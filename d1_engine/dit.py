"""Qwen-Image-Edit-2511 transformer for D1, rebuilt for inference speed.

Computes what ComfyUI's QwenImageTransformer2DModel computes for this workflow
(UNETLoader fp8mixed -> ModelSamplingAuraFlow(3.1) -> CFGNorm(1.0) -> Extract-Outfit LoRA
-> Lightning-4step LoRA -> KSampler euler/simple, cfg 1, 4 steps, reference latents
"index_timestep_zero"), restructured:

* Both LoRAs are merged into the weights once (W += strength * scale * up @ down, in fp32,
  as comfy.lora does). ComfyUI applies them on every forward instead, and with a patch
  attached it never takes its quantized matmul path: each layer was dequantized to bf16.
* Linear layers run W8A8: int8 weights with a per-output-channel scale, activations
  quantized per token on the fly, int32 accumulation (kernels.py), with bias / GELU /
  the adaLN-gated residual fused into the GEMM epilogue. ~3x the bf16 matmul rate on Ada.
* Every adaLN modulation (img_mod, txt_mod, norm_out) is a function of the timestep only,
  and the schedule is fixed, so the modulations are precomputed per step. That also
  removes 6.8B of the model's 20.4B parameters from VRAM.
* The text-side input projection and the RoPE table are computed once per image.

Numerics: this model's own bf16 rounding already moves an output ~20% (relative L2 on the
final latent) between two runs of ComfyUI on identical inputs - a 4-step sampler turns
tiny differences into different-but-equivalent details. Measured over the eval set
(10 photos x 2 seeds), the int8 + SageAttention path sits inside that spread (0.21 vs
0.20 for a bf16 re-implementation), with the same garments, layout and detail.
"""
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import torch
import torch.nn.functional as F
from safetensors import safe_open
from safetensors.torch import load_file, save_file

from . import comfy_lib  # noqa: F401  (puts ComfyUI on sys.path)
from . import kernels as K

log = logging.getLogger("d1_engine")

BF16 = torch.bfloat16
DIM, HEADS, HEAD_DIM, LAYERS = 3072, 24, 128, 60
FUSED = {  # packed GEMM -> the checkpoint linears it concatenates (output dim)
    "qkv": ["attn.to_q", "attn.to_k", "attn.to_v"],
    "tqkv": ["attn.add_q_proj", "attn.add_k_proj", "attn.add_v_proj"],
    "o": ["attn.to_out.0"], "to": ["attn.to_add_out"],
    "fc1": ["img_mlp.net.0.proj"], "fc2": ["img_mlp.net.2"],
    "tfc1": ["txt_mlp.net.0.proj"], "tfc2": ["txt_mlp.net.2"],
}
CACHE_FORMAT = 1


# ----------------------------------------------------------------------------- weights

class _LoraSet:
    """Both LoRA formats in use: PEFT (lora_A/lora_B, no alpha -> scale 1) and kohya-style
    (lora_down/lora_up + alpha -> scale alpha/rank), as comfy/weight_adapter/lora.py."""

    def __init__(self, loras):
        self.files = []
        for path, strength in loras:
            f = safe_open(path, "pt", device="cpu")
            self.files.append((f, set(f.keys()), strength))

    def delta(self, module, device):
        total = None
        for f, keys, strength in self.files:
            if module + ".lora_A.default.weight" in keys:
                down = f.get_tensor(module + ".lora_A.default.weight")
                up = f.get_tensor(module + ".lora_B.default.weight")
                scale = 1.0
            elif module + ".lora_down.weight" in keys:
                down = f.get_tensor(module + ".lora_down.weight")
                up = f.get_tensor(module + ".lora_up.weight")
                alpha_key = module + ".alpha"
                scale = (f.get_tensor(alpha_key).item() / down.shape[0]) if alpha_key in keys else 1.0
            else:
                continue
            d = torch.mm(up.to(device, torch.float32), down.to(device, torch.float32)) * (strength * scale)
            total = d if total is None else total + d
        return total


class _Checkpoint:
    def __init__(self, path, loras, device):
        self.f = safe_open(path, "pt", device="cpu")
        self.keys = set(self.f.keys())
        self.lora = _LoraSet(loras)
        self.device = device

    def plain(self, key):
        return self.f.get_tensor(key).to(self.device, BF16)

    def merged(self, module):
        """bf16(W + LoRA deltas); W dequantized as ComfyUI sees it, bf16(fp8 * scale)."""
        w = self.f.get_tensor(module + ".weight").to(self.device)
        if w.dtype in (torch.float8_e4m3fn, torch.float8_e5m2):
            w = (w.to(torch.float32) * self.f.get_tensor(module + ".weight_scale").to(self.device)).to(BF16)
        w = w.to(BF16)
        d = self.lora.delta(module, self.device)
        return w if d is None else (w.to(torch.float32) + d).to(BF16)


def _q8(w):
    w = w.to(torch.float32)
    sw = (w.abs().amax(dim=1).clamp(min=1e-8) / 127.0).contiguous()
    return torch.round(w / sw[:, None]).clamp_(-127, 127).to(torch.int8).contiguous(), sw


def flow_sigmas(steps, shift):
    """ModelSamplingAuraFlow(shift) = ModelSamplingDiscreteFlow(shift, multiplier=1.0) with the
    'simple' scheduler - ComfyUI's own classes, so the schedule is bit-identical."""
    import comfy.model_sampling as ms
    import comfy.samplers as smp

    class _MS(ms.ModelSamplingDiscreteFlow, ms.CONST):
        pass
    s = _MS()
    s.set_parameters(shift=shift, multiplier=1.0)
    return smp.calculate_sigmas(s, "simple", steps).to(torch.float32)


def _timestep_proj(t):
    """QwenTimestepProjEmbeddings.time_proj: Timesteps(256, flip_sin_to_cos, shift 0, scale 1000)."""
    from comfy.ldm.lightricks.model import get_timestep_embedding
    return get_timestep_embedding(t, 256, flip_sin_to_cos=True, downscale_freq_shift=0, scale=1000)


def convert(dit_path, loras, steps, shift, device):
    """Checkpoint + LoRAs -> the flat tensor dict this engine runs from."""
    ck = _Checkpoint(dit_path, loras, device)
    sig = flow_sigmas(steps, shift)
    out = {"sigmas": sig}
    tw = [ck.plain(f"time_text_embed.timestep_embedder.linear_{i}.{p}") for i in (1, 2) for p in ("weight", "bias")]
    temb = []
    for i in range(steps):
        t = sig[i:i + 1].to(device)                      # model_sampling.timestep(sigma) = sigma
        p = _timestep_proj(torch.cat([t, t * 0])).to(BF16)
        temb.append(F.linear(F.silu(F.linear(p, tw[0], tw[1])), tw[2], tw[3]))   # [2, 3072]: t, t=0
    for name in ("img_in", "txt_in", "proj_out"):
        out[f"{name}.w"], out[f"{name}.b"] = ck.plain(f"{name}.weight"), ck.plain(f"{name}.bias")
    out["txt_norm.w"] = ck.plain("txt_norm.weight")
    nw, nb = ck.plain("norm_out.linear.weight"), ck.plain("norm_out.linear.bias")
    for i in range(steps):
        out[f"final_mod.{i}"] = F.linear(F.silu(temb[i][:1]), nw, nb)          # [1, 6144]
    for li in range(LAYERS):
        p = f"transformer_blocks.{li}."
        for fused, parts in FUSED.items():
            qs, ss, bs = [], [], []
            for part in parts:
                q, s = _q8(ck.merged(p + part))
                qs.append(q), ss.append(s), bs.append(ck.plain(p + part + ".bias"))
            out[f"b{li}.{fused}.q"] = torch.cat(qs).contiguous()
            out[f"b{li}.{fused}.s"] = torch.cat(ss).contiguous()
            out[f"b{li}.{fused}.b"] = torch.cat(bs).contiguous()
        for n, k in (("nq", "attn.norm_q"), ("nk", "attn.norm_k"), ("naq", "attn.norm_added_q"), ("nak", "attn.norm_added_k")):
            out[f"b{li}.{n}"] = ck.plain(p + k + ".weight")
        imw, tmw = ck.merged(p + "img_mod.1"), ck.merged(p + "txt_mod.1")
        imb, tmb = ck.plain(p + "img_mod.1.bias"), ck.plain(p + "txt_mod.1.bias")
        for i in range(steps):
            out[f"b{li}.img_mod.{i}"] = F.linear(F.silu(temb[i]), imw, imb)        # [2, 18432]
            out[f"b{li}.txt_mod.{i}"] = F.linear(F.silu(temb[i][:1]), tmw, tmb)    # [1, 18432]
        del imw, tmw
    return out


def _cache_key(dit_path, loras, steps, shift):
    ident = {"format": CACHE_FORMAT, "steps": steps, "shift": shift,
             "files": [(str(p), os.path.getsize(p), int(os.path.getmtime(p)), s)
                       for p, s in [(dit_path, 1.0)] + list(loras)]}
    return hashlib.sha1(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:16]


def load_weights(dit_path, loras, steps, shift, device, cache_dir=None):
    """Converted weights, from the on-disk cache when one matches (~13GB, a few seconds to
    read) or built from the checkpoint (~25s) and cached for next time."""
    path = None
    if cache_dir:
        path = Path(cache_dir) / f"qie_d1_{_cache_key(dit_path, loras, steps, shift)}.safetensors"
        if path.exists():
            t = time.time()
            w = load_file(str(path), device=str(device))
            log.info("D1 weights loaded from cache %s in %.1fs", path.name, time.time() - t)
            return w
    t = time.time()
    w = convert(dit_path, loras, steps, shift, device)
    log.info("D1 weights converted (LoRA merge + int8) in %.1fs", time.time() - t)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            save_file({k: v.contiguous() for k, v in w.items()}, str(tmp))
            os.replace(tmp, path)
        except Exception as exc:  # a full disk must not stop generation
            log.warning("could not write D1 weight cache %s: %s", path, exc)
    return w


# ----------------------------------------------------------------------------- model

class QwenEditDiT:
    def __init__(self, dit_path, loras, steps=4, shift=3.1, device="cuda", attention=None,
                 cache_dir=None):
        self.device = torch.device(device)
        self.steps, self.shift = steps, shift
        self.attention = attention
        w = load_weights(dit_path, loras, steps, shift, self.device, cache_dir)
        self.sigmas = w["sigmas"].cpu()
        self.img_in = (w["img_in.w"], w["img_in.b"])
        self.txt_in = (w["txt_in.w"], w["txt_in.b"])
        self.proj_out = (w["proj_out.w"], w["proj_out.b"])
        self.txt_norm_w = w["txt_norm.w"]
        self.final_mod = [w[f"final_mod.{i}"] for i in range(steps)]
        self.blocks = []
        for li in range(LAYERS):
            b = {name: (w[f"b{li}.{name}.q"], w[f"b{li}.{name}.s"], w[f"b{li}.{name}.b"]) for name in FUSED}
            b["norm"] = tuple(w[f"b{li}.{n}"].contiguous() for n in ("naq", "nak", "nq", "nk"))
            b["mod"] = []
            for i in range(steps):
                im1, im2 = w[f"b{li}.img_mod.{i}"].chunk(2, dim=-1)
                tm1, tm2 = w[f"b{li}.txt_mod.{i}"].chunk(2, dim=-1)
                i1 = [t.contiguous() for t in im1.chunk(3, dim=-1)]       # shift, scale, gate: [2, 3072]
                i2 = [t.contiguous() for t in im2.chunk(3, dim=-1)]
                t1 = [t.contiguous() for t in tm1.chunk(3, dim=-1)]       # [1, 3072]
                t2 = [t.contiguous() for t in tm2.chunk(3, dim=-1)]
                t1[2] = t1[2].expand(2, -1).contiguous()                  # gate kernel takes 2 rows
                t2[2] = t2[2].expand(2, -1).contiguous()
                b["mod"].append((i1, i2, t1, t2))
            self.blocks.append(b)
        del w
        torch.cuda.empty_cache()
        self._pe = None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _patchify(x):
        """[1,16,1,h,w] -> [1, (h/2)(w/2), 64]: channel-major, then the 2x2 patch (process_img)."""
        b, c, t, h, w = x.shape
        x = x.view(b, c, t, h // 2, 2, w // 2, 2).permute(0, 2, 3, 5, 1, 4, 6)
        return x.reshape(b, t * (h // 2) * (w // 2), c * 4)

    def _ids(self, h, w, index):
        hl, wl = (h + 1) // 2, (w + 1) // 2
        ids = torch.zeros((1, hl, wl, 3), device=self.device)
        ids[..., 0] += index
        ids[..., 1] += torch.linspace(0, hl - 1, steps=hl, device=self.device).unsqueeze(1).unsqueeze(0) - (hl // 2)
        ids[..., 2] += torch.linspace(0, wl - 1, steps=wl, device=self.device).unsqueeze(0).unsqueeze(0) - (wl // 2)
        return ids.reshape(1, hl * wl, 3)

    def _rope(self, h, w, hr, wr, n_txt):
        """RoPE rows for [text, image, reference] (index 0 / 1, text after the larger half-axis)."""
        key = (h, w, hr, wr, n_txt)
        if self._pe is None or self._pe[0] != key:
            from comfy.ldm.flux.layers import EmbedND
            img_ids = torch.cat([self._ids(h, w, 0), self._ids(hr, wr, 1)], dim=1)
            txt_start = round(max(((w + 1) // 2) // 2, ((h + 1) // 2) // 2))
            txt_ids = torch.arange(txt_start, txt_start + n_txt, device=self.device).reshape(1, -1, 1).repeat(1, 1, 3)
            pe = EmbedND(dim=HEAD_DIM, theta=10000, axes_dim=[16, 56, 56])(torch.cat((txt_ids, img_ids), dim=1))
            self._pe = (key, pe.to(BF16).reshape(-1, HEAD_DIM // 2, 2, 2).contiguous())
        return self._pe[1]

    # ------------------------------------------------------------------ forward
    @torch.no_grad()
    def forward(self, x, step, txt, ref_tokens, ref_hw):
        """x: [1,16,1,h,w] process_in'ed latent. Returns the flow prediction, same shape."""
        _, _, _, h, w = x.shape
        img = F.linear(self._patchify(x.to(BF16)), *self.img_in)
        tz = img.shape[1]                                   # first reference-token row
        hs = torch.cat([img, ref_tokens], dim=1)[0].contiguous()
        enc = txt[0].contiguous()
        S, T = hs.shape[0], enc.shape[0]
        L = T + S
        pe = self._rope(h, w, ref_hw[0], ref_hw[1], T)
        qkv = torch.empty((L, 3 * DIM), device=hs.device, dtype=BF16)
        attn = self.attention
        for b in self.blocks:
            i1, i2, t1, t2 = b["mod"][step]
            # joint attention: text rows first, then image + reference rows
            xq, sx = K.ln_mod_quant(hs, i1[0], i1[1], tz)
            K.int8_gemm(xq, sx, *b["qkv"], out=qkv[T:])
            tq, ts = K.ln_mod_quant(enc, t1[0], t1[1])
            K.int8_gemm(tq, ts, *b["tqkv"], out=qkv[:T])
            K.qk_norm_rope_(qkv, pe, T, *b["norm"])
            v3 = qkv.view(1, L, 3, HEADS, HEAD_DIM)
            o = attn(v3[:, :, 0], v3[:, :, 1], v3[:, :, 2]).reshape(L, DIM)
            oq, os_ = K.quant_rows(o)
            hs = K.int8_gemm(oq[T:], os_[T:], *b["o"], epi=2, res=hs, gate=i1[2], tz=tz)
            enc = K.int8_gemm(oq[:T], os_[:T], *b["to"], epi=2, res=enc, gate=t1[2], tz=T)
            # MLPs
            xq, sx = K.ln_mod_quant(hs, i2[0], i2[1], tz)
            mid = K.int8_gemm(xq, sx, *b["fc1"], epi=1)
            mq, ms_ = K.quant_rows(mid)
            hs = K.int8_gemm(mq, ms_, *b["fc2"], epi=2, res=hs, gate=i2[2], tz=tz)
            tq, ts = K.ln_mod_quant(enc, t2[0], t2[1])
            mid = K.int8_gemm(tq, ts, *b["tfc1"], epi=1)
            mq, ms_ = K.quant_rows(mid)
            enc = K.int8_gemm(mq, ms_, *b["tfc2"], epi=2, res=enc, gate=t2[2], tz=T)
        scale, shift = self.final_mod[step].chunk(2, dim=1)
        out = torch.addcmul(shift, F.layer_norm(hs[:tz], (DIM,), eps=1e-6), 1 + scale)
        out = F.linear(out, *self.proj_out)
        out = out.view(1, 1, h // 2, w // 2, 16, 2, 2).permute(0, 4, 1, 2, 5, 3, 6)
        return out.reshape(1, 16, 1, h, w)

    @torch.no_grad()
    def sample(self, noise, context, ref_latent, latent_format):
        """Euler over the flow schedule at cfg 1 (KSampler + CFGNorm, which is a no-op scale
        of ~1 there but is kept so the math matches). noise: comfy.sample.prepare_noise
        output; ref_latent: raw VAE latent. Returns the raw latent, ready for VAE decode."""
        ref = latent_format.process_in(ref_latent.to(self.device, torch.float32))
        ctx = context.to(self.device, BF16)
        txt = F.linear(F.rms_norm(ctx, (ctx.shape[-1],), weight=self.txt_norm_w, eps=1e-6), *self.txt_in)
        ref_tokens = F.linear(self._patchify(ref.to(BF16)), *self.img_in)
        x = noise.to(self.device, torch.float32) * self.sigmas[0].item()    # (1 - sigma) * latent == 0
        for i in range(self.steps):
            s, s_next = self.sigmas[i].item(), self.sigmas[i + 1].item()
            v = self.forward(x, i, txt, ref_tokens, ref.shape[-2:]).float()
            denoised = x - v * s
            n = torch.norm(denoised, dim=1, keepdim=True)
            denoised = denoised * (n / (n + 1e-8)).clamp(0.0, 1.0)
            x = x + (x - denoised) / s * (s_next - s)
        return latent_format.process_out(x)
