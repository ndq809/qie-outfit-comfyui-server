#!/usr/bin/env python3
"""Export the parts of MS_Model_Magic_Eye this project needs into models/magic_eye/.

test_extract_outfit.py classifies each cropped item with the Magic Eye wardrobe
model, which lives in a separate project (D:\\Ndq809\\Projects\\MS_Model_Magic_Eye).
Depending on it by absolute path means the classification step only runs on the one
machine that has that folder, so the pieces are copied in here instead. They are not
copied verbatim, because upstream's are far too big to commit:

  upstream                          size      exported                     size
  checkpoints/phase2/best.pt        709 MB -> models/magic_eye/phase2.pt   195 MB
  checkpoints/phase3/best.pt         45 MB -> models/magic_eye/phase3.pt     8 MB
  anchor_items.json + MS_Model.xlsx 394 MB -> models/magic_eye/taxonomy.json  40 KB

Three reductions get it there, none of which change what the model predicts:

1. Optimizer/scheduler/GradScaler state is dropped. Adam keeps two extra fp32
   tensors per parameter, which is ~2/3 of the phase 2 file and is only needed to
   *resume training* - inference reads model_state_dict alone.
2. Floating-point weights are stored as fp16. The classifier runs its matmuls in
   fp32 either way (load_state_dict casts back on load); this only halves what sits
   on disk. Integer buffers are left alone. Measured against the fp32 originals on
   the result_v4.png crops: every predicted label identical, colour confidences
   within 0.1 percentage points (brown 67.6% -> 67.7%). Pass --fp32 to skip this,
   at the cost of a 390 MB file.
3. The taxonomy is precomputed. Upstream rebuilds its label maps at every startup
   by streaming a 394 MB anchor_items.json (257k items, ~30s) and reading auxiliary
   scores out of an Excel file - but the result is just eight label->index maps, two
   small hierarchy matrices and four score lookups, which serialise to 40 KB of
   JSON. Exporting it also drops openpyxl and the anchor corpus from this project's
   runtime requirements, and ~30s off every classification run.

Re-run this after retraining the classifier upstream:

    python scripts/export_magic_eye_bundle.py [--source D:\\...\\MS_Model_Magic_Eye]
"""
import argparse
import json
import sys
from pathlib import Path

import torch

DEFAULT_SOURCE = Path(r"D:\Ndq809\Projects\MS_Model_Magic_Eye")
DEST = Path(__file__).resolve().parent.parent / "models" / "magic_eye"
# Provenance worth keeping (which epoch these weights are, and its validation
# metrics); everything else in the upstream file is training state.
KEEP_KEYS = ("epoch", "best_val_raw", "best_val_loss", "metrics")


def export_checkpoint(src, dest, half=True):
    ckpt = torch.load(src, map_location="cpu", weights_only=False)
    state = ckpt["model_state_dict"]
    if half:
        state = {k: (v.half() if v.is_floating_point() else v) for k, v in state.items()}
    out = {"model_state_dict": state}
    out.update({k: ckpt[k] for k in KEEP_KEYS if k in ckpt})
    dest.parent.mkdir(parents=True, exist_ok=True)
    torch.save(out, dest)
    print(f"  {src.name}: {src.stat().st_size / 1e6:.0f} MB -> {dest.name}: "
          f"{dest.stat().st_size / 1e6:.0f} MB")


def export_taxonomy(source, dest):
    """Dump TaxonomyInfo to JSON so it never has to be rebuilt from the anchor corpus."""
    # build_taxonomy() prints "cat→sub"; a cp1252 Windows console can't encode that
    # and would take the export down on a print statement.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    sys.path.insert(0, str(source))
    try:
        from magic_eye.taxonomy import build_taxonomy
    finally:
        sys.path.remove(str(source))

    taxonomy, _ = build_taxonomy(source / "anchor_items.json", source / "MS_Model.xlsx")
    data = {
        "gender_map": taxonomy.gender_map,
        "category_map": taxonomy.category_map,
        "sub_category_map": taxonomy.sub_category_map,
        "type_map": taxonomy.type_map,
        "color_map": taxonomy.color_map,
        "neck_map": taxonomy.neck_map,
        "sleeve_map": taxonomy.sleeve_map,
        "pattern_map": taxonomy.pattern_map,
        # Hierarchy masks: [num_cat, num_sub] and [num_sub, num_type] 0/1 matrices.
        "cat_to_sub_matrix": taxonomy.cat_to_sub_matrix.tolist(),
        "sub_to_type_matrix": taxonomy.sub_to_type_matrix.tolist(),
        "type_scores": {k: list(v) for k, v in taxonomy.type_scores.items()},
        "neck_scores": taxonomy.neck_scores,
        "sleeve_scores": taxonomy.sleeve_scores,
        "color_scores": taxonomy.color_scores,
    }
    dest.write_text(json.dumps(data), encoding="utf-8")
    print(f"  anchor_items.json + MS_Model.xlsx -> {dest.name}: "
          f"{dest.stat().st_size / 1e6:.1f} MB")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help="MS_Model_Magic_Eye project root")
    ap.add_argument("--dest", type=Path, default=DEST)
    ap.add_argument("--fp32", action="store_true",
                    help="Keep weights in fp32 (doubles the exported size)")
    args = ap.parse_args()

    source, dest = args.source, args.dest
    if not source.is_dir():
        sys.exit(f"Magic Eye project not found: {source}")
    dest.mkdir(parents=True, exist_ok=True)
    print(f"Exporting {source} -> {dest}")

    export_checkpoint(source / "checkpoints" / "phase2" / "best.pt",
                      dest / "phase2.pt", half=not args.fp32)
    export_checkpoint(source / "checkpoints" / "phase3" / "best.pt",
                      dest / "phase3.pt", half=not args.fp32)

    thresholds = dest / "multi_label_thresholds.json"
    thresholds.write_bytes((source / "multi_label_thresholds.json").read_bytes())
    print(f"  multi_label_thresholds.json -> {thresholds.name}")

    export_taxonomy(source, dest / "taxonomy.json")
    print("Done.")


if __name__ == "__main__":
    main()
