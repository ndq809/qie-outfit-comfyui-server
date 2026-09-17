"""What each model in the pipeline was actually run with, for the test report.

The report page has to answer "why did this photo come out like this", and the four
stage images alone cannot: the same grid from the same prompt looks different at 4
steps vs 40, and a garment goes missing because a threshold sat above its score, not
because the picture was wrong. So every stage hands back the settings it ran with.

Read, never assumed: the detector reports its own thresholds (they take a per-request
override and OUTFIT_ITEMS_DEVICE), and D1's settings are read out of the workflow
ComfyUI actually executed. Restating the module defaults here would go stale the first
time a caller passed something else.

Each builder returns one stage dict: {key, title, seconds, models, params, note}.
`models` is what was loaded, `params` the knobs. Both render as plain label/value rows,
so adding a knob here needs no change to the page.
"""
from pathlib import Path

from PIL import Image

import test_extract_outfit as pipeline
import wardrobe_classifier


def detect_stage(detected: dict, original: Path, seconds: float) -> dict:
    """D0b — which garment categories the subject is wearing."""
    p = dict(detected.get("params") or {})
    device = p.pop("device", detected.get("device"))
    models = {
        "detector": p.pop("detector_model", "?"),
        "person detector": p.pop("person_model", "?"),
        "SAM (cô lập chủ thể)": p.pop("sam_model", "?"),
        "face match": p.pop("face_model", "?"),
    }
    params = {
        "device": device,
        "ảnh vào": _dimensions(original),
        "score threshold": p.pop("threshold", None),
        "threshold riêng theo lớp": p.pop("class_thresholds", None),
        "person score threshold": p.pop("person_score_threshold", None),
        "face match threshold": p.pop("face_match_threshold", None),
        "multi-scale TTA": p.pop("multi_scale_tta", None),
        "person-crop TTA": p.pop("person_crop_tta", None),
        "item phải nằm trong box người ≥": p.pop("subject_item_min_frac", None),
        "item vào mask SAM khi nằm trong ≥": p.pop("item_inside_person_frac", None),
        "số người trong ảnh": detected.get("persons"),
        "SAM đã chạy": detected.get("isolated_by_sam"),
        "độ giống khuôn mặt": detected.get("face_similarity"),
    }
    params.update(p)  # anything the detector added since this was written
    note = None
    if not detected.get("isolated_by_sam"):
        note = ("Chỉ 1 người trong ảnh nên không cô lập bằng SAM — lọc theo box người "
                "(rẻ hơn và không đụng vào pixel nào).")
    labels = {"decode_image": "giải mã ảnh gốc", "save_isolated": "ghi ảnh isolate (PNG)",
              "person_detect": "dò người (Faster R-CNN)",
              "face_load": "nạp ArcFace", "face_ref_embed": "embed ảnh mặt tham chiếu",
              "face_match_group": "so khớp khuôn mặt",
              "sam_load": "nạp SAM", "sam_segment": "SAM tách nền",
              "fashion_detect_locate": "dò trang phục (vòng định vị)",
              "fashion_detect_final": "dò trang phục (vòng chốt)"}
    named = {labels.get(k, k): v for k, v in (detected.get("timing") or {}).items()}
    return _stage("d0b", "D0b · Phát hiện trang phục đang mặc", seconds, models, params,
                  note, timing=_with_overhead(named, seconds, "tải ảnh + HTTP tới detector"))


def generate_stage(workflow: dict, used_isolated: bool, grid: Path, seconds: float,
                   timing: dict | None = None) -> dict:
    """D1 — the Qwen-Image-Edit generation that lays the outfit out as a flat grid."""
    def node(name):
        return (workflow.get(name) or {}).get("inputs", {})

    ks, loaders = node("ksampler"), {n: node(n) for n in ("unet_loader", "clip_loader", "vae_loader")}
    models = {
        "diffusion model": loaders["unet_loader"].get("unet_name"),
        "text encoder": loaders["clip_loader"].get("clip_name"),
        "VAE": loaders["vae_loader"].get("vae_name"),
    }
    for key, label in (("lora_outfit", "LoRA tách trang phục"), ("lora_lightning", "LoRA tăng tốc")):
        lora = node(key)
        if lora:
            models[label] = f'{lora.get("lora_name")} · strength {lora.get("strength_model")}'
    params = {
        "steps": ks.get("steps"),
        "cfg": ks.get("cfg"),
        "sampler": ks.get("sampler_name"),
        "scheduler": ks.get("scheduler"),
        "denoise": ks.get("denoise"),
        "seed": ks.get("seed"),
        "shift (ModelSamplingAuraFlow)": node("model_sampling").get("shift"),
        "CFGNorm strength": node("cfg_norm").get("strength"),
        "reference latents": node("ref_pos").get("reference_latents_method"),
        "negative": ("ConditioningZeroOut (cfg=1.0 nên KSampler bỏ qua nhánh uncond)"
                     if "text_neg" not in workflow else "TextEncodeQwenImageEditPlus với prompt rỗng"),
        "ảnh đưa vào": ("ảnh isolate (SAM)" if used_isolated else "ảnh gốc") + " → FluxKontextImageScale",
        "ảnh grid sinh ra": _dimensions(grid),
    }
    return _stage("d1", "D1 · Sinh ảnh grid trang phục (ComfyUI)", seconds, models, params,
                  timing=_with_overhead(timing, seconds, "chờ hàng đợi + poll"))


def crop_stage(asked_items: list, crops: list, seconds: float,
               timing: dict | None = None) -> dict:
    """D3a — cutting the generated grid back into one image per garment."""
    models = {"segmenter": pipeline.SEGMENTER_MODEL_NAME}
    params = {
        "device": _torch_device(),
        "input size": "1024×1024 (resize, chuẩn hoá ImageNet)",
        "min_area_frac": 0.002,
        "merge_gap_frac": 0.02,
        "merge_cap_frac": 0.03,
        "số ô prompt xin": len(asked_items or []),
        "số ô tách được": len(crops),
        "kích thước crop": ", ".join(f'{c["size"][0]}×{c["size"][1]}' for c in crops) or "—",
    }
    note = None
    if asked_items and len(asked_items) != len(crops):
        note = (f"Tách được {len(crops)} ô nhưng prompt xin {len(asked_items)} — crop được "
                "đặt tên theo vị trí thay vì theo nhãn, để không gán nhầm loại trang phục.")
    return _stage("d3a", "D3a · Cắt từng trang phục ra khỏi grid", seconds, models, params, note,
                  timing=_with_overhead(timing, seconds, "cắt + ghép nền"))


def classify_stage(records: list, seconds: float, timing: dict | None = None) -> dict:
    """D3b — Magic Eye attributes + the embeddings D2 later dedups on."""
    bundle = Path(pipeline.MAGIC_EYE_BUNDLE)
    models = {
        "backbone": wardrobe_classifier.BACKBONE_NAME,
        "phase 2 (thuộc tính)": f"{bundle.name}/phase2.pt",
        "phase 3 (text embedding)": f"{bundle.name}/phase3.pt",
    }
    params = {
        "device": _torch_device(),
        "batch size": 16,
        "ngưỡng multi-label": "0.5 (fallback 0.15), có threshold riêng từng lớp trong "
                              "multi_label_thresholds.json",
        "nền khi ghép alpha": "trắng (#ffffff)",
        "số crop phân loại": len(records),
        "visual embedding": _embedding_dim(records, "visual_embedding"),
        "text embedding": _embedding_dim(records, "text_embedding"),
    }
    # load is ~0 after the first photo of a run: the models stay warm in this process.
    labels = {"load": "nạp model", "phase2": "phase 2 (thuộc tính)", "phase3": "phase 3 (text emb.)"}
    named = {labels.get(k, k): v for k, v in (timing or {}).items()}
    return _stage("d3b", "D3b · Phân loại thuộc tính từng trang phục", seconds, models, params,
                  timing=_with_overhead(named, seconds, "đọc ảnh + tiền xử lý"))


def dedup_stage(records: list, kept: list, same_image_threshold: float,
                wardrobe_threshold: float, seconds: float | None = None) -> dict:
    """D2 — the two dedup passes. `seconds` covers only the same-image one; the
    wardrobe-level pass runs in data-server, and its outcome per crop shows on the
    crop itself."""
    params = {
        "so khớp bằng": "cosine similarity trên visual embedding của D3b",
        "ngưỡng trùng trong cùng ảnh": same_image_threshold,
        "ngưỡng trùng với đồ đã có trong tủ": wardrobe_threshold,
        "loại vì trùng trong cùng ảnh": len(records) - len(kept),
        "gửi sang data-server": len(kept),
    }
    return _stage("d2", "D2 · Loại trang phục trùng", seconds, {}, params,
                  "Thời gian trên chỉ tính vòng so trong cùng ảnh. Vòng so với tủ đồ chạy ở "
                  "data-server sau khi nhận kết quả — kết quả từng món hiện ngay dưới ảnh crop.")


def _stage(key, title, seconds, models, params, note=None, timing=None) -> dict:
    return {
        "key": key, "title": title,
        # 3 decimals under 0.1s: D2's same-image pass is sub-millisecond, and rounding
        # it to 0.0 reads as "not measured" rather than "too fast to matter".
        "seconds": None if seconds is None else round(seconds, 2 if seconds >= 0.1 else 3),
        "models": {k: v for k, v in models.items() if v not in (None, "?")},
        "params": {k: v for k, v in params.items() if v is not None},
        "timing": {k: v for k, v in (timing or {}).items()},
        "note": note,
    }


def _with_overhead(timing: dict | None, seconds: float, label: str) -> dict:
    """The per-model times plus whatever of the stage's wall time they don't account
    for, under `label`. Without it the panel silently loses seconds - D3b's models can
    add up to 1.7s of a 2.4s stage - and the reader cannot tell whether the gap is a
    slow model or the I/O around it."""
    timing = {k: v for k, v in (timing or {}).items() if v is not None}
    if not timing:
        return {}
    rest = round(seconds - sum(timing.values()), 3)
    return {**timing, label: rest} if rest >= 0.05 else timing


def _dimensions(path) -> str | None:
    try:
        with Image.open(path) as im:
            return f"{im.width}×{im.height}"
    except Exception:
        return None


def _embedding_dim(records: list, key: str) -> str | None:
    vec = next((r.get(key) for r in records if r.get(key)), None)
    return f"{len(vec)}-d" if vec else None


def _torch_device() -> str:
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"
