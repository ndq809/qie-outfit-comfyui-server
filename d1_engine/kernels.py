"""Triton kernels for the D1 engine's W8A8 path.

int8 GEMM with the whole post-matmul chain fused into its epilogue, so a linear layer
costs one read of its int8 input and one write of its bf16 output:

    y = acc_int32 * sx[m] * sw[n] + bias[n]                  (always)
    y = gelu_tanh(y)                                         (EPI_GELU)
    y = res[m, n] + gate[m >= tz][n] * y                     (EPI_GATE: adaLN-gated residual,
                                                              two gate rows: regular / t=0 tokens)

and per-token activation quantization fused with whatever precedes it (LayerNorm +
adaLN modulate, or nothing), producing int8 + one fp32 scale per row.
"""
import torch
import triton
import triton.language as tl

# --------------------------------------------------------------------------- GEMM

_GEMM_CONFIGS = [
    triton.Config({"BM": 128, "BN": 128, "BK": 64, "GM": 8}, num_stages=4, num_warps=4),
    triton.Config({"BM": 128, "BN": 128, "BK": 128, "GM": 8}, num_stages=3, num_warps=8),
    triton.Config({"BM": 128, "BN": 256, "BK": 64, "GM": 8}, num_stages=3, num_warps=8),
    triton.Config({"BM": 256, "BN": 128, "BK": 64, "GM": 8}, num_stages=3, num_warps=8),
    triton.Config({"BM": 128, "BN": 128, "BK": 64, "GM": 8}, num_stages=5, num_warps=8),
    triton.Config({"BM": 64, "BN": 128, "BK": 128, "GM": 8}, num_stages=4, num_warps=4),
    triton.Config({"BM": 128, "BN": 64, "BK": 128, "GM": 8}, num_stages=4, num_warps=4),
]


@triton.jit
def _gelu_tanh(x):
    # 0.5 * x * (1 + tanh(sqrt(2/pi) * (x + 0.044715 x^3)))
    inner = 0.7978845608028654 * (x + 0.044715 * x * x * x)
    # tanh via exp for portability: tanh(u) = 1 - 2 / (exp(2u) + 1)
    t = 1.0 - 2.0 / (tl.exp(2.0 * inner) + 1.0)
    return 0.5 * x * (1.0 + t)


# cache_results: the tuned tile per shape is kept in the Triton cache, so a restart
# does not re-benchmark every config (~30s on the first image otherwise).
@triton.autotune(configs=_GEMM_CONFIGS, key=["N", "K", "EPI", "M_BUCKET"], cache_results=True)
@triton.jit
def _int8_gemm_kernel(a_ptr, b_ptr, c_ptr, sa_ptr, sb_ptr, bias_ptr, res_ptr, gate_ptr,
                      M, N, K, tz, M_BUCKET,
                      stride_am, stride_bn, stride_cm, stride_rm,
                      HAS_BIAS: tl.constexpr, EPI: tl.constexpr,
                      BM: tl.constexpr, BN: tl.constexpr, BK: tl.constexpr, GM: tl.constexpr):
    pid = tl.program_id(0)
    num_pid_m = tl.cdiv(M, BM)
    num_pid_n = tl.cdiv(N, BN)
    num_pid_in_group = GM * num_pid_n
    group_id = pid // num_pid_in_group
    first_pid_m = group_id * GM
    group_size_m = min(num_pid_m - first_pid_m, GM)
    pid_m = first_pid_m + ((pid % num_pid_in_group) % group_size_m)
    pid_n = (pid % num_pid_in_group) // group_size_m

    rm = pid_m * BM + tl.arange(0, BM)
    rn = pid_n * BN + tl.arange(0, BN)
    rk = tl.arange(0, BK)
    rm_c = tl.minimum(rm, M - 1)
    rn_c = tl.minimum(rn, N - 1)
    a_ptrs = a_ptr + rm_c[:, None] * stride_am + rk[None, :]
    b_ptrs = b_ptr + rn_c[None, :] * stride_bn + rk[:, None]
    acc = tl.zeros((BM, BN), dtype=tl.int32)
    for k in range(0, K, BK):
        a = tl.load(a_ptrs)
        b = tl.load(b_ptrs)
        acc = tl.dot(a, b, acc, out_dtype=tl.int32)
        a_ptrs += BK
        b_ptrs += BK

    sa = tl.load(sa_ptr + rm_c)
    sb = tl.load(sb_ptr + rn_c)
    y = acc.to(tl.float32) * sa[:, None] * sb[None, :]
    if HAS_BIAS:
        y += tl.load(bias_ptr + rn_c).to(tl.float32)[None, :]
    if EPI == 1:
        y = _gelu_tanh(y)
    mask = (rm[:, None] < M) & (rn[None, :] < N)
    if EPI == 2:
        # bf16 residual + bf16 gate, rounded like the eager path: y is first rounded to bf16
        g0 = tl.load(gate_ptr + rn_c).to(tl.float32)
        g1 = tl.load(gate_ptr + N + rn_c).to(tl.float32)
        g = tl.where(rm[:, None] < tz, g0[None, :], g1[None, :])
        yb = y.to(tl.bfloat16).to(tl.float32)
        r = tl.load(res_ptr + rm[:, None] * stride_rm + rn[None, :], mask=mask, other=0.0).to(tl.float32)
        y = r + (yb * g).to(tl.bfloat16).to(tl.float32)
    tl.store(c_ptr + rm[:, None] * stride_cm + rn[None, :], y.to(tl.bfloat16), mask=mask)


def int8_gemm(a, sa, w, sw, bias=None, epi=0, res=None, gate=None, tz=0, out=None):
    """a: int8 [M,K] (row-major), sa: fp32 [M]; w: int8 [N,K] (row-major), sw: fp32 [N].
    epi: 0 plain, 1 gelu-tanh, 2 gated residual (res [M,N] bf16, gate [2,N] bf16, rows < tz use gate[0])."""
    M, K = a.shape
    N = w.shape[0]
    assert K % 128 == 0 and a.stride(1) == 1 and w.stride(1) == 1
    c = out if out is not None else torch.empty((M, N), device=a.device, dtype=torch.bfloat16)
    grid = lambda meta: (triton.cdiv(M, meta["BM"]) * triton.cdiv(N, meta["BN"]),)
    _int8_gemm_kernel[grid](
        a, w, c, sa, sw, bias if bias is not None else sw, res if res is not None else c,
        gate if gate is not None else sw,
        # text rows (~300) and image rows (~8000) want different tiles; tune them apart
        M, N, K, tz, 0 if M < 2048 else 1, a.stride(0), w.stride(0), c.stride(0),
        res.stride(0) if res is not None else 0,
        HAS_BIAS=bias is not None, EPI=epi)
    return c


# --------------------------------------------------------------------------- quantization

@triton.jit
def _quant_rows_kernel(x_ptr, q_ptr, s_ptr, K, stride_x, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    cols = tl.arange(0, BLOCK)
    m = cols < K
    x = tl.load(x_ptr + row * stride_x + cols, mask=m, other=0.0).to(tl.float32)
    amax = tl.max(tl.abs(x), axis=0)
    s = tl.maximum(amax, 1e-8) / 127.0
    q = x / s
    q = tl.extra.cuda.libdevice.rint(q)
    q = tl.minimum(tl.maximum(q, -127.0), 127.0)
    tl.store(q_ptr + row * K + cols, q.to(tl.int8), mask=m)
    tl.store(s_ptr + row, s)


def quant_rows(x):
    """bf16 [M,K] -> (int8 [M,K], fp32 [M]) symmetric per-row."""
    x2 = x.reshape(-1, x.shape[-1])
    M, K = x2.shape
    q = torch.empty((M, K), device=x.device, dtype=torch.int8)
    s = torch.empty((M,), device=x.device, dtype=torch.float32)
    BLOCK = triton.next_power_of_2(K)
    _quant_rows_kernel[(M,)](x2, q, s, K, x2.stride(0), BLOCK=BLOCK, num_warps=8 if K > 4096 else 4)
    return q, s


@triton.jit
def _ln_mod_quant_kernel(x_ptr, shift_ptr, scale_ptr, q_ptr, s_ptr, K, tz, stride_x, eps,
                         BLOCK: tl.constexpr, TWO: tl.constexpr):
    """LayerNorm (no affine) -> x*(1+scale)+shift (per row group) -> rounded to bf16 (as the
    eager path materialises it) -> per-row int8."""
    row = tl.program_id(0)
    cols = tl.arange(0, BLOCK)
    m = cols < K
    x = tl.load(x_ptr + row * stride_x + cols, mask=m, other=0.0).to(tl.float32)
    mean = tl.sum(x, axis=0) / K
    xc = tl.where(m, x - mean, 0.0)
    var = tl.sum(xc * xc, axis=0) / K
    xn = (xc * tl.rsqrt(var + eps)).to(tl.bfloat16).to(tl.float32)
    off = 0
    if TWO:
        off = tl.where(row >= tz, K, 0)
    sh = tl.load(shift_ptr + off + cols, mask=m, other=0.0).to(tl.float32)
    sc = tl.load(scale_ptr + off + cols, mask=m, other=0.0).to(tl.float32)
    y = (xn * (1.0 + sc) + sh).to(tl.bfloat16).to(tl.float32)
    amax = tl.max(tl.abs(y), axis=0)
    s = tl.maximum(amax, 1e-8) / 127.0
    q = tl.extra.cuda.libdevice.rint(y / s)
    q = tl.minimum(tl.maximum(q, -127.0), 127.0)
    tl.store(q_ptr + row * K + cols, q.to(tl.int8), mask=m)
    tl.store(s_ptr + row, s)


def ln_mod_quant(x, shift, scale, tz=None, eps=1e-6):
    """x bf16 [M,K]; shift/scale bf16 [R,K] with R=2 (rows >= tz use row 1) or R=1."""
    x2 = x.reshape(-1, x.shape[-1])
    M, K = x2.shape
    q = torch.empty((M, K), device=x.device, dtype=torch.int8)
    s = torch.empty((M,), device=x.device, dtype=torch.float32)
    two = shift.shape[0] == 2
    _ln_mod_quant_kernel[(M,)](x2, shift.contiguous(), scale.contiguous(), q, s, K, tz if tz is not None else M,
                               x2.stride(0), eps, BLOCK=triton.next_power_of_2(K), TWO=two, num_warps=4)
    return q, s


# --------------------------------------------------------------------------- qk norm + rope

@triton.jit
def _qk_norm_rope_kernel(buf_ptr, stride_row, pe_ptr, wq_t_ptr, wk_t_ptr, wq_i_ptr, wk_i_ptr,
                         T, L, eps, H: tl.constexpr, HP: tl.constexpr, D: tl.constexpr, R: tl.constexpr):
    """In place on R token rows of the joint qkv buffer [L, 3*H*D]: per-head RMSNorm of q
    and k (text rows use the norm_added_* weights), then interleaved RoPE with each row's
    bf16 cos/sin table pe[row] = [D/2, 2, 2]. Contiguous loads; pairs split in registers."""
    pid = tl.program_id(0)
    h = tl.arange(0, HP)
    d = tl.arange(0, D)
    j = tl.arange(0, D // 2)
    hm = (h < H)[:, None]
    off = h[:, None] * D + d[None, :]
    for rr in tl.static_range(R):
        row = pid * R + rr
        if row < L:
            is_txt = row < T
            base = buf_ptr + row * stride_row
            pe_row = pe_ptr + row * (D // 2) * 4
            f00 = tl.load(pe_row + j * 4 + 0).to(tl.float32)
            f01 = tl.load(pe_row + j * 4 + 1).to(tl.float32)
            f10 = tl.load(pe_row + j * 4 + 2).to(tl.float32)
            f11 = tl.load(pe_row + j * 4 + 3).to(tl.float32)
            for part in tl.static_range(2):
                p = base + part * H * D
                x = tl.load(p + off, mask=hm, other=0.0).to(tl.float32)
                r = tl.rsqrt(tl.sum(x * x, axis=1) / D + eps)
                if part == 0:
                    w = tl.where(is_txt, tl.load(wq_t_ptr + d), tl.load(wq_i_ptr + d)).to(tl.float32)
                else:
                    w = tl.where(is_txt, tl.load(wk_t_ptr + d), tl.load(wk_i_ptr + d)).to(tl.float32)
                n = (x * r[:, None] * w[None, :]).to(tl.bfloat16).to(tl.float32)
                n0, n1 = tl.split(tl.reshape(n, (HP, D // 2, 2)))
                o0 = f00[None, :] * n0 + f01[None, :] * n1
                o1 = f10[None, :] * n0 + f11[None, :] * n1
                o = tl.reshape(tl.join(o0, o1), (HP, D))
                tl.store(p + off, o.to(tl.bfloat16), mask=hm)


def qk_norm_rope_(buf, pe, T, wq_t, wk_t, wq_i, wk_i, heads=24, head_dim=128, eps=1e-6):
    """buf: bf16 [L, 3*H*D] (q | k | v per row, text rows first); pe: bf16 [L, D/2, 2, 2]."""
    L = buf.shape[0]
    R = 2
    _qk_norm_rope_kernel[(triton.cdiv(L, R),)](buf, buf.stride(0), pe, wq_t, wk_t, wq_i, wk_i, T, L, eps,
                                               H=heads, HP=triton.next_power_of_2(heads), D=head_dim, R=R,
                                               num_warps=8)
    return buf
