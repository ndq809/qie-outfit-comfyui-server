"""D0b's photo, decoded once and kept on the GPU as uint8, with the preprocessing of every
D0b model reproduced there bit for bit.

Before this, every model call re-did its own 12MP pixel work on the CPU: PIL -> tensor
copies (~25ms each), the HF processors' antialiased resizes (~38ms per DETR call, 8 calls a
photo), the person detector's /255 conversion, the background paint, the BGR copy for
InsightFace - about 0.6s of a photo's ~1.1s standalone, and more under the pipeline's load.

Exact, not merely close: the detector is chaotic at this level (a one-pixel-different
person crop once moved a score from 0.4746 to 0.5336, IMG_9822), so a "1 LSB here and
there" GPU resize would change flags on its own. Hence:
- resize_aa(): a port of the uint8 antialiased bilinear kernel torch runs on AVX2 CPUs
  (UpSampleKernelAVXAntialias.h): the same double-precision filter weights, rounded to
  int16 at the same precision, an exact int32 accumulation, a horizontal pass to a uint8
  intermediate, then the vertical pass. Checked equal to the CPU output for every resize
  D0b does on the test photos (scripts/check_gpu_image.py).
- normalisation through 256-entry lookup tables computed by the very CPU code the
  processors run, so every (channel, value) maps to the identical float.
"""
import math
from functools import lru_cache

import cv2
import numpy as np
import torch
from PIL import Image

# --------------------------------------------------------------------------- decoding

_JPEG_MAGIC = b"\xff\xd8"


def decode_bgr(path) -> np.ndarray:
    """HxWx3 uint8 BGR, the pixels Image.open(path).convert("RGB") gives (reversed).

    JPEGs go through OpenCV's libjpeg-turbo, ~1.5x faster than PIL's and bit-identical to
    it (same ISLOW IDCT and fancy upsampling; checked on every test photo). EXIF orientation
    is ignored on purpose, like PIL's decoder - D0b has always seen the stored orientation.
    Anything else (PNG, CMYK or 16-bit oddities...) takes PIL's own path."""
    data = np.fromfile(str(path), dtype=np.uint8)
    if data[:2].tobytes() == _JPEG_MAGIC:
        bgr = cv2.imdecode(data, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if bgr is not None:
            return bgr
    rgb = np.asarray(Image.open(path).convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


class GpuImage:
    """An RGB photo as uint8 [3, H, W] on the GPU, with the slice of PIL's Image API that
    D0b's helpers use (size/width/height/mode, crop, convert), so the same code accepts
    either. `bgr` is the CPU copy InsightFace reads, when the photo came from the CPU."""

    mode = "RGB"

    def __init__(self, t: torch.Tensor, bgr: np.ndarray | None = None):
        self.t = t
        self._bgr = bgr

    @classmethod
    def from_bgr(cls, bgr: np.ndarray, device) -> "GpuImage":
        t = torch.from_numpy(bgr).to(device).permute(2, 0, 1).flip(0).contiguous()
        return cls(t, bgr)

    @classmethod
    def from_pil(cls, image: Image.Image, device) -> "GpuImage":
        rgb = np.asarray(image.convert("RGB"))
        return cls.from_bgr(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), device)

    @classmethod
    def open(cls, path, device) -> "GpuImage":
        return cls.from_bgr(decode_bgr(path), device)

    @property
    def size(self):
        return (self.t.shape[2], self.t.shape[1])

    @property
    def width(self):
        return self.t.shape[2]

    @property
    def height(self):
        return self.t.shape[1]

    def bgr(self) -> np.ndarray:
        if self._bgr is None:
            self._bgr = np.ascontiguousarray(self.t.flip(0).permute(1, 2, 0).cpu().numpy())
        return self._bgr

    def to_pil(self) -> Image.Image:
        if self._bgr is not None:
            return Image.fromarray(cv2.cvtColor(self._bgr, cv2.COLOR_BGR2RGB))
        return Image.fromarray(self.t.permute(1, 2, 0).cpu().numpy())

    def crop(self, box) -> "GpuImage":
        """PIL's crop semantics: box rounded like PIL does, area outside the image black."""
        x0, y0, x1, y1 = (int(round(v)) for v in box)
        if x1 < x0 or y1 < y0:
            raise ValueError(f"invalid crop box {box}")
        h, w = self.height, self.width
        if x0 >= 0 and y0 >= 0 and x1 <= w and y1 <= h:
            return GpuImage(self.t[:, y0:y1, x0:x1])
        out = torch.zeros((3, y1 - y0, x1 - x0), dtype=torch.uint8, device=self.t.device)
        sx0, sy0, sx1, sy1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
        if sx1 > sx0 and sy1 > sy0:
            out[:, sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = self.t[:, sy0:sy1, sx0:sx1]
        return GpuImage(out)

    def convert(self, mode) -> Image.Image:
        return self.to_pil().convert(mode)

    def to_pil_exif_transposed(self, path) -> Image.Image:
        """ImageOps.exif_transpose(Image.open(path).convert("RGB")) for the photo this was
        decoded from: the stored pixels turned upright by the file's EXIF orientation."""
        image = self.to_pil()
        try:
            orientation = Image.open(path).getexif().get(0x0112)
        except Exception:
            orientation = None
        method = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
                  4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
                  6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
                  8: Image.Transpose.ROTATE_90}.get(orientation)
        return image.transpose(method) if method is not None else image


# --------------------------------------------------------------------------- exact resize

def _aa_filter(x):
    x = abs(x)
    return 1.0 - x if x < 1.0 else 0.0


def _int16_weights_reference(in_size: int, out_size: int):
    """The scalar transcription of torch's weight computation; _int16_weights is the
    vectorised form (checked equal by scripts/check_gpu_image.py)."""
    scale = in_size / out_size
    support = scale if scale >= 1.0 else 1.0          # (interp_size * 0.5) * scale, interp_size 2
    ksize = math.ceil(support) * 2 + 1
    invscale = 1.0 / scale if scale >= 1.0 else 1.0
    xmins = np.zeros(out_size, np.int64)
    weights = np.zeros((out_size, ksize), np.float64)
    wt_max = 0.0
    for i in range(out_size):
        center = scale * (i + 0.5)
        xmin = max(int(center - support + 0.5), 0)
        xsize = min(int(center + support + 0.5), in_size) - xmin
        xsize = min(max(xsize, 0), ksize)
        ws = [_aa_filter((j + xmin - center + 0.5) * invscale) for j in range(xsize)]
        total = 0.0
        for w in ws:
            total += w
        if total != 0.0:
            ws = [w / total for w in ws]
        for j, w in enumerate(ws):
            weights[i, j] = w
            wt_max = max(wt_max, w)
        xmins[i] = xmin
    return _quantize(xmins, weights, wt_max)


@lru_cache(maxsize=256)
def _int16_weights(in_size: int, out_size: int):
    """(xmin[out], weights int32 [out, K], precision) exactly as torch's
    _compute_index_ranges_int16_weights computes them for antialiased bilinear.

    Vectorised over output positions with the scalar code's operation order per element
    (the tap sum is a running sum over j, not numpy's pairwise sum): crop sizes change with
    every photo, and the per-pixel Python loop cost 10-20ms per new size."""
    scale = in_size / out_size
    support = scale if scale >= 1.0 else 1.0          # (interp_size * 0.5) * scale, interp_size 2
    ksize = math.ceil(support) * 2 + 1
    invscale = 1.0 / scale if scale >= 1.0 else 1.0
    center = scale * (np.arange(out_size, dtype=np.int64) + 0.5)
    xmins = np.maximum(np.trunc(center - support + 0.5).astype(np.int64), 0)
    xsize = np.minimum(np.trunc(center + support + 0.5).astype(np.int64), in_size) - xmins
    xsize = np.clip(xsize, 0, ksize)
    j = np.arange(ksize, dtype=np.int64)
    x = np.abs(((j[None, :] + xmins[:, None]) - center[:, None] + 0.5) * invscale)
    weights = np.where((x < 1.0) & (j[None, :] < xsize[:, None]), 1.0 - x, 0.0)
    total = np.zeros(out_size, np.float64)
    for k in range(ksize):
        total = total + weights[:, k]
    weights = np.where(total[:, None] != 0.0, weights / np.where(total == 0.0, 1.0, total)[:, None],
                       weights)
    return _quantize(xmins, weights, float(weights.max(initial=0.0)))


def _quantize(xmins, weights, wt_max):
    precision = 0
    while precision < 22:
        if int(0.5 + wt_max * (1 << (precision + 1))) >= (1 << 15):
            break
        precision += 1
    v = weights * (1 << precision)
    w_int = np.where(v < 0, np.trunc(v - 0.5), np.trunc(v + 0.5)).astype(np.int32)
    return xmins, w_int, precision


@lru_cache(maxsize=256)
def _pass_tensors(in_size, out_size, device):
    xmins, w_int, precision = _int16_weights(in_size, out_size)
    ksize = w_int.shape[1]
    idx = np.minimum(xmins[None, :] + np.arange(ksize)[:, None], in_size - 1)   # [K, out]
    return (torch.from_numpy(idx).to(device), torch.from_numpy(w_int.T.copy()).to(device),
            precision)


def _resize_pass(x: torch.Tensor, dim: int, out_size: int) -> torch.Tensor:
    """One separable pass over `dim` (1 = rows, 2 = columns) of a uint8 [C, H, W] tensor."""
    idx, w, precision = _pass_tensors(x.shape[dim], out_size, x.device)
    shape = [1, 1, 1]
    shape[dim] = out_size
    acc = None
    for k in range(idx.shape[0]):
        term = x.index_select(dim, idx[k]).to(torch.int32) * w[k].view(shape)
        acc = term if acc is None else acc.add_(term)
    acc.add_(1 << (precision - 1)).bitwise_right_shift_(precision)
    return acc.clamp_(0, 255).to(torch.uint8)


def resize_aa(t: torch.Tensor, out_h: int, out_w: int) -> torch.Tensor:
    """uint8 [3, H, W] -> [3, out_h, out_w], equal to torch's CPU (AVX2) uint8
    antialiased bilinear interpolate - what torchvision's resize runs for the HF processors."""
    if t.shape[2] != out_w:
        t = _resize_pass(t, 2, out_w)
    if t.shape[1] != out_h:
        t = _resize_pass(t, 1, out_h)
    return t


# --------------------------------------------------------------------------- normalisation

def lut_from_cpu(fn, device) -> torch.Tensor:
    """[3, 256] float table: fn applied on the CPU to a [1, 3, 1, 256] uint8 ramp."""
    ramp = torch.arange(256, dtype=torch.uint8).view(1, 1, 1, 256).expand(1, 3, 1, 256).contiguous()
    return fn(ramp).reshape(3, 256).to(torch.float32).to(device)


def apply_lut(t: torch.Tensor, lut: torch.Tensor) -> torch.Tensor:
    c, h, w = t.shape
    return lut.gather(1, t.reshape(c, h * w).long()).view(c, h, w)


_luts = {}


def _processor_lut(processor, device):
    key = (type(processor).__name__, processor.do_rescale, processor.rescale_factor,
           processor.do_normalize, tuple(processor.image_mean), tuple(processor.image_std),
           str(device))
    if key not in _luts:
        _luts[key] = lut_from_cpu(lambda x: processor.rescale_and_normalize(
            x, processor.do_rescale, processor.rescale_factor, processor.do_normalize,
            processor.image_mean, processor.image_std), device)
    return _luts[key]


def detr_inputs(processor, image: GpuImage) -> dict:
    """What `processor(images=pil, return_tensors="pt")` returns for one image (a
    ConditionalDetrImageProcessor with a shortest/longest-edge size), on the GPU."""
    from transformers.models.conditional_detr.image_processing_conditional_detr import (
        get_size_with_aspect_ratio)
    size = processor.size
    h, w = get_size_with_aspect_ratio((image.height, image.width), size["shortest_edge"],
                                      size["longest_edge"])
    pixels = apply_lut(resize_aa(image.t, h, w), _processor_lut(processor, image.t.device))
    mask = torch.ones((1, h, w), dtype=torch.int64, device=image.t.device)
    return {"pixel_values": pixels[None], "pixel_mask": mask}


def sam_inputs(sam_processor, image: GpuImage, boxes, pixels=True) -> dict:
    """What `sam_processor(pil, input_boxes=[boxes], return_tensors="pt")` returns
    (without pixel_values when pixels=False - the encoder already ran)."""
    ip = sam_processor.image_processor
    H, W = image.height, image.width
    longest = ip.size["longest_edge"]
    h, w = ip._get_preprocess_shape((H, W), longest)
    norm = sam_processor._normalize_coordinates(longest, np.array(list(boxes)).astype(np.float32),
                                                (H, W), is_bounding_box=True)
    out = {"original_sizes": torch.tensor([[H, W]], dtype=torch.int64),
           "reshaped_input_sizes": torch.tensor([[h, w]], dtype=torch.int64),
           "input_boxes": torch.from_numpy(np.array([norm])).to(image.t.device)}
    if pixels:
        out["pixel_values"] = sam_pixel_values(sam_processor, image)
    return out


def sam_pixel_values(sam_processor, image: GpuImage) -> torch.Tensor:
    """The pixel_values of sam_inputs() alone (no host->device copy, so nothing waits)."""
    ip = sam_processor.image_processor
    h, w = ip._get_preprocess_shape((image.height, image.width), ip.size["longest_edge"])
    resized = apply_lut(resize_aa(image.t, h, w), _processor_lut(ip, image.t.device))
    ph, pw = ip.pad_size["height"], ip.pad_size["width"]
    padded = torch.zeros((1, 3, ph, pw), dtype=resized.dtype, device=resized.device)
    padded[0, :, :h, :w] = resized
    return padded


@lru_cache(maxsize=4)
def _to_float_lut(device):
    from torchvision.transforms import functional as F
    return lut_from_cpu(lambda x: F.convert_image_dtype(x, torch.float), device)


def to_float(image: GpuImage) -> torch.Tensor:
    """torchvision's convert_image_dtype(uint8 -> float) as the CPU computes it (the GPU's
    own division differs in the last bit, enough to move the person boxes by 0.3px)."""
    return apply_lut(image.t, _to_float_lut(image.t.device))
