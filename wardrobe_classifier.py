#!/usr/bin/env python3
"""Classify each cropped garment with the Magic Eye wardrobe model.

Given the square item crops test_extract_outfit.py cuts out of a generated outfit
grid, this predicts the eight attributes the wardrobe index is built on - gender,
category, sub_category, type, color, neck, sleeve, pattern - plus the two 768-d
embeddings used for retrieval, and writes them to wardrobe_index.json.

Two trained models run per image, both committed under models/magic_eye/:

  phase2.pt  SigLIP-base vision backbone + the 8 attribute heads. This is the
             classifier: image -> visual embedding -> attribute logits.
  phase3.pt  Text-embedding generator. Takes phase 2's visual embedding *and* its
             predicted attributes and produces the 768-d text embedding that
             wardrobe search matches against. It is a head on top of phase 2, not
             a standalone model, which is why both are loaded here.

Only the SigLIP architecture comes from Hugging Face (google/siglip-base-patch16-224,
downloaded and cached on first use); its weights are immediately overwritten by
phase2.pt, which was fine-tuned from it.

This mirrors MS_Model_Magic_Eye's inference.py - the decode and description logic
below is that pipeline's, kept behaviour-identical so results match the upstream
project - but reads the committed bundle instead of that project's 1.1 GB of
training checkpoints and anchor corpus. See scripts/export_magic_eye_bundle.py for
how the bundle is produced and models/magic_eye/README.md for what is in it.

Usage:
    python3 wardrobe_classifier.py <folder_of_images> [-o wardrobe_index.json]
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List

import torch
import torch.nn.functional as F
from PIL import Image
from torchvision import transforms as T

from magic_eye.config import Phase2Config, Phase3Config
from magic_eye.model_phase2 import MagicEyePhase2
from magic_eye.model_phase3 import TextEmbeddingGenerator
from magic_eye.taxonomy import load_taxonomy

BUNDLE_DIR = Path(__file__).resolve().parent / "models" / "magic_eye"
BACKBONE_NAME = "google/siglip-base-patch16-224"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
# Background colour for alpha-compositing transparent images. White, because a plain
# .convert("RGB") turns transparent pixels black, which makes black clothing
# indistinguishable from its own background.
ALPHA_BACKGROUND_COLOR = (255, 255, 255)


def scan_images(folder: Path) -> List[Path]:
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)


def load_image_rgb(path: Path) -> Image.Image:
    img = Image.open(path)
    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, ALPHA_BACKGROUND_COLOR)
        background.paste(img, mask=img.split()[3])
        return background
    return img.convert("RGB")


def invert_map(m: Dict[str, int]) -> Dict[int, str]:
    return {v: k for k, v in m.items()}


def decode_single_label(logits, idx2label):
    return [idx2label[i] for i in logits.argmax(dim=-1).cpu().tolist()]


def decode_multi_label(logits, idx2label, threshold=0.5, fallback_min=0.15,
                       per_class_thresholds=None, return_confidence=False):
    """Sigmoid + threshold per sample. Falls back to the top-1 class when nothing
    clears its threshold but that class is still at least fallback_min, so an item
    is never left with no colour at all."""
    probs = torch.sigmoid(logits).cpu()
    results = []
    for row in probs:
        if per_class_thresholds is not None:
            active = (row > per_class_thresholds).nonzero(as_tuple=True)[0].tolist()
        else:
            active = (row > threshold).nonzero(as_tuple=True)[0].tolist()
        if not active:
            top_val, top_idx = row.max(dim=0)
            if top_val.item() >= fallback_min:
                active = [top_idx.item()]
        active.sort(key=lambda i: row[i].item(), reverse=True)
        if return_confidence:
            results.append([{"color": idx2label[i],
                             "confidence": round(row[i].item() * 100, 1)} for i in active])
        else:
            results.append([idx2label[i] for i in active])
    return results


def load_per_class_thresholds(path: Path, head_name: str, label_map: Dict[str, int]):
    """Per-class thresholds tuned upstream (tune_thresholds.py). A single global 0.5
    over-predicts common colours and drops rare ones."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if head_name not in data:
        return None
    t = torch.full((len(label_map),), 0.5)
    for label, idx in label_map.items():
        if label in data[head_name]:
            t[idx] = data[head_name][label]
    return t


def build_description(type_, gender, category, sub_category, color, neck, sleeve, pattern) -> str:
    """Concise natural-language description in the style of the wardrobe corpus,
    e.g. "short sleeve printed t-shirts in black and white. crewneck collar."."""
    parts = []
    if sleeve:
        parts.append(" ".join(sleeve))
    if pattern:
        parts.append(" ".join(pattern))
    parts.append(type_)
    color_names = [c["color"] if isinstance(c, dict) else c for c in color]
    if len(color_names) == 1:
        parts.append(f"in {color_names[0]}")
    elif len(color_names) == 2:
        parts.append(f"in {color_names[0]} and {color_names[1]}")
    elif color_names:
        parts.append("in " + ", ".join(color_names[:-1]) + f", and {color_names[-1]}")

    sentences = [" ".join(parts) + "."]
    if neck:
        sentences.append(" ".join(neck) + ".")
    sentences.append(f"{gender}'s {category}.")
    return " ".join(sentences)


def predictions_to_phase3_inputs(preds):
    """Phase 3 consumes phase 2's *decisions*, not its logits: argmax for the
    single-label heads, thresholded sigmoid for the multi-label ones."""
    return dict(
        gender=preds["gender"].argmax(dim=-1),
        category=preds["category"].argmax(dim=-1),
        sub_category=preds["sub_category"].argmax(dim=-1),
        type_idx=preds["type"].argmax(dim=-1),
        color=(torch.sigmoid(preds["color"]) > 0.5).float(),
        neck=(torch.sigmoid(preds["neck"]) > 0.5).float(),
        sleeve=(torch.sigmoid(preds["sleeve"]) > 0.5).float(),
        pattern=(torch.sigmoid(preds["pattern"]) > 0.5).float(),
    )


def _check_bundle(bundle_dir: Path):
    """Fail with an explanation rather than a torch unpickling error.

    The two .pt files are Git LFS objects. A clone made without LFS still *has* them -
    as ~130-byte text pointers - so an existence check alone passes and torch.load()
    then dies on what looks like a corrupt checkpoint. Check the LFS magic instead."""
    missing = [f for f in ("phase2.pt", "phase3.pt", "taxonomy.json")
               if not (bundle_dir / f).exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing {', '.join(missing)} in {bundle_dir}. Regenerate the bundle with "
            f"scripts/export_magic_eye_bundle.py, or `git lfs pull` if this is a clone.")

    pointers = [f for f in ("phase2.pt", "phase3.pt")
                if (bundle_dir / f).stat().st_size < 1024
                and (bundle_dir / f).read_bytes().startswith(b"version https://git-lfs")]
    if pointers:
        raise RuntimeError(
            f"{', '.join(pointers)} in {bundle_dir} are Git LFS pointer files, not the "
            f"actual weights. Run `git lfs install` (once per machine) and `git lfs pull`.")


def load_models(bundle_dir=BUNDLE_DIR, device=None, backbone_name=BACKBONE_NAME):
    bundle_dir = Path(bundle_dir)
    _check_bundle(bundle_dir)
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    taxonomy = load_taxonomy(bundle_dir / "taxonomy.json")

    cfg2 = Phase2Config()
    model_p2 = MagicEyePhase2(
        taxonomy=taxonomy, backbone_name=backbone_name,
        embedding_dim=cfg2.embedding_dim, hidden_dim=cfg2.hidden_dim,
        hard_head_hidden_dim=cfg2.hard_head_hidden_dim, dropout=cfg2.dropout,
        unfreeze_layers=cfg2.unfreeze_layers,
    )
    ckpt2 = torch.load(bundle_dir / "phase2.pt", map_location="cpu", weights_only=False)
    # The bundle stores fp16 weights; load_state_dict casts them into this fp32 model.
    model_p2.load_state_dict(ckpt2["model_state_dict"])
    model_p2.to(device).eval()

    cfg3 = Phase3Config()
    model_p3 = TextEmbeddingGenerator(
        taxonomy=taxonomy, embedding_dim=cfg3.embedding_dim,
        attr_dim=cfg3.attr_embedding_dim, hidden_dim=cfg3.hidden_dim,
        num_hidden_layers=cfg3.num_hidden_layers, dropout=cfg3.dropout,
    )
    ckpt3 = torch.load(bundle_dir / "phase3.pt", map_location="cpu", weights_only=False)
    model_p3.load_state_dict(ckpt3["model_state_dict"])
    model_p3.to(device).eval()

    from transformers import AutoImageProcessor
    proc = AutoImageProcessor.from_pretrained(backbone_name)
    transform = T.Compose([
        T.Resize((224, 224), interpolation=T.InterpolationMode.BICUBIC),
        T.ToTensor(),
        T.Normalize(proc.image_mean, proc.image_std),
    ])
    return model_p2, model_p3, taxonomy, transform, device


@torch.no_grad()
def classify_images(images, bundle_dir=BUNDLE_DIR, device=None, batch_size=16,
                    multi_label_threshold=0.5, verbose=True):
    """Classify a list of image paths (or every image in a folder).

    Returns one record per image, in the wardrobe_index.json shape: the eight
    attributes, a generated description, and the visual/text embeddings."""
    bundle_dir = Path(bundle_dir)
    if isinstance(images, (str, Path)):
        images = scan_images(Path(images))
    images = [Path(p) for p in images]
    if not images:
        return []

    t = time.time()
    model_p2, model_p3, taxonomy, transform, device = load_models(bundle_dir, device)
    load_time = time.time() - t
    if verbose:
        print(f"  models loaded on {device} ({taxonomy.summary()})")

    phase2_time = phase3_time = 0.0

    maps = {name: invert_map(getattr(taxonomy, f"{name}_map")) for name in
            ("gender", "category", "sub_category", "type", "color", "neck", "sleeve", "pattern")}
    thresholds_path = bundle_dir / "multi_label_thresholds.json"
    pct = {name: load_per_class_thresholds(thresholds_path, name, getattr(taxonomy, f"{name}_map"))
           for name in ("color", "neck", "sleeve", "pattern")}

    results = []
    for start in range(0, len(images), batch_size):
        batch_paths, pixel_batch, valid = images[start:start + batch_size], [], []
        for p in batch_paths:
            try:
                pixel_batch.append(transform(load_image_rgb(p)))
                valid.append(p)
            except Exception as e:
                print(f"  skipping {p.name}: {e}")
        if not pixel_batch:
            continue

        pixel_values = torch.stack(pixel_batch).to(device)
        t = time.time()
        visual_emb = F.normalize(model_p2.backbone(pixel_values=pixel_values).pooler_output,
                                 p=2, dim=-1)
        preds = model_p2.classifier(visual_emb)
        phase2_time += time.time() - t

        genders = decode_single_label(preds["gender"], maps["gender"])
        categories = decode_single_label(preds["category"], maps["category"])
        sub_categories = decode_single_label(preds["sub_category"], maps["sub_category"])
        types = decode_single_label(preds["type"], maps["type"])
        colors = decode_multi_label(preds["color"], maps["color"], multi_label_threshold,
                                    per_class_thresholds=pct["color"], return_confidence=True)
        necks = decode_multi_label(preds["neck"], maps["neck"], multi_label_threshold,
                                   per_class_thresholds=pct["neck"])
        sleeves = decode_multi_label(preds["sleeve"], maps["sleeve"], multi_label_threshold,
                                     per_class_thresholds=pct["sleeve"])
        patterns = decode_multi_label(preds["pattern"], maps["pattern"], multi_label_threshold,
                                      per_class_thresholds=pct["pattern"])

        t = time.time()
        text_emb = model_p3(visual_emb=visual_emb, **predictions_to_phase3_inputs(preds))
        phase3_time += time.time() - t
        visual_cpu, text_cpu = visual_emb.cpu(), text_emb.cpu()

        for i, path in enumerate(valid):
            results.append({
                "image_id": len(results) + 1,
                "image_name": path.name,
                "original_text": build_description(
                    types[i], genders[i], categories[i], sub_categories[i],
                    colors[i], necks[i], sleeves[i], patterns[i]),
                "type": types[i], "gender": genders[i],
                "category": categories[i], "sub_category": sub_categories[i],
                "color": colors[i], "neck": necks[i],
                "sleeve": sleeves[i], "pattern": patterns[i],
                "visual_embedding": visual_cpu[i].tolist(),
                "text_embedding": text_cpu[i].tolist(),
            })
    if verbose:
        print(f"  [D3 model timing] load={load_time:.3f}s phase2={phase2_time:.3f}s "
              f"phase3={phase3_time:.3f}s (n={len(results)} crops)")
    return results


def main():
    ap = argparse.ArgumentParser(description="Magic Eye wardrobe classifier")
    ap.add_argument("image_folder", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="default: <image_folder>/wardrobe_index.json")
    ap.add_argument("--bundle", type=Path, default=BUNDLE_DIR)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()

    if not args.image_folder.is_dir():
        sys.exit(f"Not a directory: {args.image_folder}")
    records = classify_images(args.image_folder, bundle_dir=args.bundle,
                              device=torch.device("cpu") if args.cpu else None,
                              batch_size=args.batch_size)
    out = args.output or args.image_folder / "wardrobe_index.json"
    out.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    for r in records:
        print(f"{r['image_name']}: {r['original_text']}")
    print(f"Wrote {len(records)} items to {out}")


if __name__ == "__main__":
    main()
