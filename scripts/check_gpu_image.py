#!/usr/bin/env python3
"""Check that gpu_image reproduces D0b's CPU preprocessing bit for bit.

For every photo given (default: test-images/), compares against the original CPU code:
decode, the DETR processor at every scale on the photo and on a person-sized crop, the SAM
processor (pixels, sizes, boxes) and the person detector's float conversion. Exits 1 on
the first mismatch.

    python scripts/check_gpu_image.py [photos...]
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
import torch  # noqa: E402
from PIL import Image  # noqa: E402

import gpu_image as gi  # noqa: E402


def same(name, gpu, cpu):
    gpu = gpu.cpu() if isinstance(gpu, torch.Tensor) else gpu
    ok = (gpu.dtype == cpu.dtype and gpu.shape == cpu.shape and torch.equal(gpu, cpu))
    if not ok:
        diff = (gpu.double() - cpu.double()).abs().max().item() if gpu.shape == cpu.shape else None
        print(f"MISMATCH {name}: gpu {gpu.dtype} {tuple(gpu.shape)} cpu {cpu.dtype} "
              f"{tuple(cpu.shape)} max|diff| {diff}")
        sys.exit(1)


def main():
    from torchvision.models.detection import FasterRCNN_MobileNet_V3_Large_320_FPN_Weights
    from transformers import AutoImageProcessor, SamProcessor

    import detect_clothing_yolo as clothing
    photos = [Path(p) for p in sys.argv[1:]] or sorted((REPO / "test-images").iterdir())

    rng = np.random.default_rng(0)
    pairs = [(int(a), int(b)) for a, b in zip(rng.integers(200, 6000, 300), rng.integers(200, 3700, 300))]
    for in_size, out_size in pairs + [(4032, 1066), (3024, 800), (4032, 1600), (3024, 1200), (777, 1200)]:
        ref, vec = gi._int16_weights_reference(in_size, out_size), gi._int16_weights(in_size, out_size)
        if not (np.array_equal(ref[0], vec[0]) and np.array_equal(ref[1], vec[1]) and ref[2] == vec[2]):
            print(f"MISMATCH resize weights {in_size}->{out_size}")
            sys.exit(1)
    # and the resize itself against torch's CPU kernel, down- and upscaling
    src = torch.from_numpy(rng.integers(0, 256, (3, 1531, 1187), dtype=np.uint8))
    for oh, ow in [(800, 621), (1200, 931), (2600, 2016), (517, 1187), (1531, 400), (3000, 2400)]:
        ref = torch.nn.functional.interpolate(src[None], size=(oh, ow), mode="bilinear",
                                              align_corners=False, antialias=True)[0]
        same(f"resize random {oh}x{ow}", gi.resize_aa(src.cuda(), oh, ow), ref)
    print("resize weights + kernel: identical")
    procs = [AutoImageProcessor.from_pretrained(clothing.MODEL_NAME, size=s)
             for s in clothing.SCALES_FULL + clothing.SCALES_CROP]
    sam = SamProcessor.from_pretrained("facebook/sam-vit-base")
    person_pre = FasterRCNN_MobileNet_V3_Large_320_FPN_Weights.DEFAULT.transforms()

    for path in photos:
        pil = Image.open(path).convert("RGB")
        img = gi.GpuImage.open(path, "cuda")
        same(f"{path.name} decode", img.t.permute(1, 2, 0), torch.from_numpy(np.asarray(pil).copy()))
        w, h = pil.size
        # a tall person-like crop and a wide one, both partly at the frame edge
        crops = [(int(w * 0.18), int(h * 0.07), int(w * 0.61), h), (0, int(h * 0.3), w, int(h * 0.71))]
        for box in [None] + crops:
            src_pil = pil if box is None else pil.crop(box)
            src = img if box is None else img.crop(box)
            for p in procs:
                ref = p(images=src_pil, return_tensors="pt")
                got = gi.detr_inputs(p, src)
                tag = f"{path.name} detr {dict(p.size)} crop={box}"
                same(tag + " pixel_values", got["pixel_values"], ref["pixel_values"])
                same(tag + " pixel_mask", got["pixel_mask"], ref["pixel_mask"])
        boxes = [[w * 0.1, h * 0.05, w * 0.55, h * 0.98], [w * 0.4, h * 0.5, w * 0.7, h * 0.8]]
        ref = sam(pil, input_boxes=[boxes], return_tensors="pt")
        got = gi.sam_inputs(sam, img, boxes)
        for k in ("pixel_values", "original_sizes", "reshaped_input_sizes", "input_boxes"):
            same(f"{path.name} sam {k}", got[k], ref[k])
        same(f"{path.name} person float", gi.to_float(img), person_pre(pil))
        # PIL-semantics crop, including a box hanging off the frame (filled black)
        for box in [(10, 20, 300, 400), (w - 100, h - 50, w + 40, h + 30), (-5, -5, 60, 70)]:
            same(f"{path.name} crop {box}", torch.from_numpy(np.asarray(img.crop(box).convert("RGB")).copy()),
                 torch.from_numpy(np.asarray(pil.crop(box)).copy()))
        # the D1 input of a single-person photo: the stored pixels turned upright by EXIF
        from PIL import ImageOps
        same(f"{path.name} exif transpose",
             torch.from_numpy(np.asarray(img.to_pil_exif_transposed(path)).copy()),
             torch.from_numpy(np.asarray(ImageOps.exif_transpose(Image.open(path).convert("RGB"))).copy()))
        print(f"{path.name}: identical")
    print("ALL IDENTICAL")


if __name__ == "__main__":
    main()
