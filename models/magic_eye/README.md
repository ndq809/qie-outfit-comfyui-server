# Magic Eye wardrobe classifier — committed bundle

Everything `wardrobe_classifier.py` needs to label a cropped garment. Exported from
the separate **MS_Model_Magic_Eye** training project by
[`scripts/export_magic_eye_bundle.py`](../../scripts/export_magic_eye_bundle.py);
nothing here is edited by hand.

| file | size | what it is |
| --- | --- | --- |
| `phase2.pt` | 195 MB (LFS) | SigLIP-base vision backbone + the 8 attribute heads. The classifier: image → visual embedding → attribute logits. Epoch 15. |
| `phase3.pt` | 7.5 MB (LFS) | Text-embedding generator. Takes phase 2's visual embedding *and* its predicted attributes → the 768-d text embedding used for wardrobe retrieval. A head on top of phase 2, not a standalone model. Epoch 49. |
| `taxonomy.json` | 40 KB | The 8 label→index maps (gender 5, category 4, sub_category 49, type 122, color 23, neck 12, sleeve 5, pattern 11), the two hierarchy matrices, and the auxiliary score look-ups. Every head is sized off these, so they must match the checkpoints. |
| `multi_label_thresholds.json` | 2 KB | Per-class sigmoid thresholds for the four multi-label heads, tuned upstream. A flat 0.5 over-predicts common colours and drops rare ones. |

The `.pt` files are **Git LFS** objects — GitHub rejects blobs over 100 MB. A clone
needs `git lfs install` once per machine, then `git lfs pull`. Without it these are
one-line pointer files and `wardrobe_classifier.py` fails with a clear message.

Not stored here, downloaded from Hugging Face on first use (~1 GB, cached):
`google/siglip-base-patch16-224`. Only its *architecture* and image-preprocessing
constants are used — `phase2.pt` overwrites every vision weight it defines.

## Regenerating

After retraining upstream:

```bash
python scripts/export_magic_eye_bundle.py --source D:\Ndq809\Projects\MS_Model_Magic_Eye
```

The export drops optimizer state, stores weights as fp16, and precomputes the
taxonomy that upstream otherwise rebuilds from a 394 MB anchor corpus at every
startup — 1.15 GB down to 203 MB, with identical predicted labels. The script's
docstring has the details and the measurements.
