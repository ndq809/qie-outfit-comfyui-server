#!/usr/bin/env python3
"""Terminal test client for the QIE-2511-Extract-Outfit ComfyUI server.

Usage:
    python3 test_extract_outfit.py <input_image> [prompt]

Uploads the image to ComfyUI, runs it through the Qwen-Image-Edit-2511 (GGUF Q5_K_S)
+ QIE-2511-Extract-Outfit LoRA pipeline, and saves the result next to the input.
"""
import json
import sys
import time
import urllib.request
import uuid

COMFY_URL = "http://127.0.0.1:18188"
UNET_NAME = "qwen-image-edit-2511-Q4_K_M.gguf"
UNET_NAME_FP8 = "qwen_image_edit_2511_fp8mixed.safetensors"
CLIP_NAME = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
VAE_NAME = "qwen_image_vae.safetensors"
LORA_NAME = "QIE-2511-Extract-Outfit-4200.safetensors"
LIGHTNING_LORAS = {
    4: "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
    8: "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors",
}
CLIP_CLASSIFIER_MODEL = "openai/clip-vit-base-patch32"
CLIP_SERVICE_URL = "http://127.0.0.1:18189"


def _detect_worn_items_inprocess(image_path):
    """Fallback path: loads CLIP fresh in this process (~8s). Only used if the
    clip_classifier supervisor service isn't reachable."""
    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor

    model = CLIPModel.from_pretrained(CLIP_CLASSIFIER_MODEL)
    processor = CLIPProcessor.from_pretrained(CLIP_CLASSIFIER_MODEL)
    model.eval()

    def classify(img, pos, neg):
        inputs = processor(text=[pos, neg], images=img, return_tensors="pt", padding=True)
        with torch.no_grad():
            out = model(**inputs)
        probs = out.logits_per_image.softmax(dim=1)[0]
        return probs[0].item() > probs[1].item()

    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    bottom_crop = img.crop((0, int(h * 0.75), w, h))

    headwear = classify(img, "a person with a hat on their head", "a person with no hat, bare head")
    footwear = classify(
        bottom_crop,
        "a close-up of feet wearing shoes or sandals on the ground",
        "a close-up of the ground, ocean, or fabric with no feet or shoes",
    )
    one_piece = classify(
        img,
        "a one-piece dress with no separation at the waist",
        "a separate top and bottom, two different garments worn together",
    )
    bag = classify(
        img,
        "a person with a shoulder bag or handbag",
        "a person with nothing extra, just clothing",
    )
    return {"headwear": headwear, "footwear": footwear, "one_piece": one_piece, "bag": bag}


def detect_worn_items(image_path):
    """Lightweight zero-shot presence check (CLIP ViT-B/32, ~350MB, CPU-only — never
    competes with the GPU/VRAM the main fp8 pipeline needs) for every item category the
    outfit-extraction LoRA is unreliable at inferring from pixels alone: headwear,
    footwear, bag, and whether the worn outfit is a single one-piece dress vs a
    separate top+bottom (the LoRA defaults to always splitting into "top" + "skirt"
    even for a genuine one-piece dress unless told otherwise). Nothing in the item
    list build_prompt() constructs is hardcoded/assumed present — every category is
    classified up front so the prompt can state a fact ("this person is/isn't wearing
    X") instead of asking the diffusion model to guess presence from the image, which
    it does inconsistently (see project history). Whole-image classification works
    well for headwear, bag, and one-piece-vs-separates; footwear needs the bottom crop
    of the frame or beach/background content confuses it.

    Talks to the clip_classifier supervisor service (model loaded once, kept warm —
    see clip_classifier_service.py) so repeated runs pay only the ~0.5-1s inference
    cost instead of the ~8s process-startup + model-load cost every time. Falls back
    to loading CLIP in this process if that service isn't running."""
    try:
        payload = json.dumps({"image_path": image_path}).encode()
        req = urllib.request.Request(
            f"{CLIP_SERVICE_URL}/detect", data=payload, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, ConnectionError, OSError):
        print("  (clip_classifier service unreachable, loading CLIP in-process instead)")
        return _detect_worn_items_inprocess(image_path)


def _grid_positions(n):
    rows = (n + 1) // 2
    labels = []
    for row in range(1, rows + 1):
        labels.append(f"row {row} left")
        labels.append(f"row {row} right")
    return labels


def build_prompt(detected):
    body = ["dress"] if detected["one_piece"] else ["top/shirt", "bottom (skirt/pants)"]
    items = (["hat"] if detected["headwear"] else []) + body + \
            (["bag"] if detected["bag"] else []) + (["shoes"] if detected["footwear"] else [])
    placements = ", ".join(f"{item} in {pos}" for item, pos in zip(items, _grid_positions(len(items))))
    return (
        f"Arrange in a grid, one item per cell: {placements}. Plain white background, each "
        "item placed separately with a wide gap of clear white space between them, no "
        "touching, no overlapping. The bag lies flat on its own with its strap coiled "
        "neatly beside it, not worn or draped over any other item. Professional flat "
        "mockup photography."
    )


def upload_image(path):
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        data = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{path.split("/")[-1]}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return json.loads(urllib.request.urlopen(req).read())["name"]


def build_workflow(image_name, prompt, seed=42, lightning_steps=None, fp8=False):
    """lightning_steps: None for full quality (40 steps, cfg 4.0), or 4/8 to chain the
    Lightning distilled-speed LoRA on top (steps=lightning_steps, cfg=1.0).
    fp8: use the native fp8mixed checkpoint (fast fp8 tensor-core matmul) instead of
    the GGUF Q5_K_S quant (which dequantizes on the fly and can't use fp8 tensor cores)."""
    steps = lightning_steps or 40
    cfg = 1.0 if lightning_steps else 4.0

    model_chain = "lora_outfit"
    if fp8:
        unet_node = {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_NAME_FP8, "weight_dtype": "default"}}
    else:
        unet_node = {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": UNET_NAME}}
    nodes = {
        "unet_loader": unet_node,
        "clip_loader": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_NAME, "type": "qwen_image", "device": "default"}},
        "vae_loader": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        "model_sampling": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet_loader", 0], "shift": 3.1}},
        "cfg_norm": {"class_type": "CFGNorm", "inputs": {"model": ["model_sampling", 0], "strength": 1.0, "pre_cfg": False}},
        "lora_outfit": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["cfg_norm", 0], "lora_name": LORA_NAME, "strength_model": 1.0}},
        "load_image": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "kontext_scale": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["load_image", 0]}},
        "text_pos": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip_loader", 0], "vae": ["vae_loader", 0], "image1": ["kontext_scale", 0], "prompt": prompt}},
        "ref_pos": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["text_pos", 0], "reference_latents_method": "index_timestep_zero"}},
        "vae_encode": {"class_type": "VAEEncode", "inputs": {"pixels": ["kontext_scale", 0], "vae": ["vae_loader", 0]}},
        "vae_decode": {"class_type": "VAEDecode", "inputs": {"samples": ["ksampler", 0], "vae": ["vae_loader", 0]}},
        "save_image": {"class_type": "SaveImage", "inputs": {"images": ["vae_decode", 0], "filename_prefix": "extract_outfit_test"}},
    }

    if cfg == 1.0:
        # KSampler already skips the uncond forward pass when cfg==1.0 (comfy/samplers.py
        # math.isclose(cond_scale, 1.0) fast path), so the negative branch's *value* never
        # reaches the model. Only its tensor shape needs to be valid -> zero out the already-
        # computed positive conditioning instead of re-running the ~7B VLM text encoder a
        # second time on the same reference image. Saves ~1-3s per request.
        nodes["negative_cond"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["ref_pos", 0]}}
    else:
        nodes["text_neg"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip_loader", 0], "vae": ["vae_loader", 0], "image1": ["kontext_scale", 0], "prompt": ""}}
        nodes["negative_cond"] = {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {"conditioning": ["text_neg", 0], "reference_latents_method": "index_timestep_zero"}}

    if lightning_steps:
        nodes["lora_lightning"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": ["lora_outfit", 0], "lora_name": LIGHTNING_LORAS[lightning_steps], "strength_model": 1.0},
        }
        model_chain = "lora_lightning"

    nodes["ksampler"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": [model_chain, 0], "positive": ["ref_pos", 0], "negative": ["negative_cond", 0],
            "latent_image": ["vae_encode", 0], "seed": seed, "steps": steps, "cfg": cfg,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
        },
    }
    return nodes


def main():
    args = sys.argv[1:]
    lightning_steps = None
    if "--lightning4" in args:
        lightning_steps = 4
        args.remove("--lightning4")
    elif "--lightning8" in args:
        lightning_steps = 8
        args.remove("--lightning8")
    fp8 = "--fp8" in args
    if fp8:
        args.remove("--fp8")
    seed = 42
    if "--seed" in args:
        i = args.index("--seed")
        seed = int(args[i + 1])
        del args[i:i + 2]

    if len(args) < 1:
        print(f"Usage: {sys.argv[0]} [--lightning4|--lightning8] [--fp8] [--seed N] <input_image> [prompt]")
        sys.exit(1)
    image_path = args[0]
    if len(args) > 1:
        prompt = args[1]
    else:
        print("Detecting headwear/footwear presence (CLIP ViT-B/32, CPU)...")
        t_det = time.time()
        detected = detect_worn_items(image_path)
        print(f"  headwear={detected['headwear']}, footwear={detected['footwear']}, one_piece={detected['one_piece']}, bag={detected['bag']} ({time.time() - t_det:.1f}s)")
        prompt = build_prompt(detected)

    print(f"Uploading {image_path} ...")
    image_name = upload_image(image_path)
    print(f"Uploaded as {image_name}")

    client_id = uuid.uuid4().hex
    workflow = build_workflow(image_name, prompt, seed=seed, lightning_steps=lightning_steps, fp8=fp8)
    print(f"Using seed={seed} (pass --seed N to retry with a different layout/result)")
    if lightning_steps:
        print(f"Using Lightning {lightning_steps}-step LoRA (cfg=1.0)")
    if fp8:
        print("Using native fp8mixed checkpoint (fp8 tensor-core matmul, no GGUF dequant)")
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(f"{COMFY_URL}/prompt", data=payload, headers={"Content-Type": "application/json"})

    t0 = time.time()
    try:
        resp = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        print("ComfyUI rejected the workflow:")
        print(e.read().decode())
        sys.exit(1)
    prompt_id = resp["prompt_id"]
    print(f"Queued prompt_id={prompt_id}, waiting for completion...")

    while True:
        with urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}") as r:
            hist = json.loads(r.read())
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            if status.get("completed"):
                break
            if status.get("status_str") == "error":
                print("Generation failed:", json.dumps(status, indent=2))
                sys.exit(1)
        time.sleep(2)
        print(f"  ...still running ({time.time() - t0:.0f}s elapsed)")

    elapsed = time.time() - t0
    outputs = entry["outputs"]
    saved = []
    for node_id, out in outputs.items():
        for img in out.get("images", []):
            saved.append(img["filename"])
    print(f"Done in {elapsed:.1f}s. Output image(s): {saved}")
    print(f"Fetch with: curl -s '{COMFY_URL}/view?filename={saved[0]}&type=output' -o result.png" if saved else "No image output produced.")


if __name__ == "__main__":
    main()
