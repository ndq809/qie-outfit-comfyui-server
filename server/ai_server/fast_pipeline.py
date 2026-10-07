"""ai-server worker, in-process and pipelined (settings.d1_engine == "fast").

Same per-photo work as the serial worker (D0b -> D1 -> D3 -> D2, uploads, one result per
ticket), with every model resident in this process and the stages overlapped across
three threads:

    prep  : pop ticket, download, decode, D0b (detector/SAM/ArcFace), prompt, Kontext resize
    gen   : D1 - d1_engine.D1Engine (text encoder + int8 transformer + VAE)
    post  : D3a crop, D3b classify, same-image D2, upload crops, report, push result

D1 is the GPU-bound stage (~2.1s a photo on an RTX 4090); the other two are mostly CPU
(JPEG decode, PIL resizes, mask post-processing, PNG encodes, HTTP to MinIO) and run while
the next/previous photo is generating, so throughput approaches D1's time alone. D0b runs
in-process instead of behind the item_detector service, which removes an HTTP round trip
and a 12MP PNG encode + decode of the isolated subject per photo.

D0b and D1 issue their GPU work on CUDA streams of their own, D3 on the default stream.
On one shared stream every .item()/.cpu() of the other stages waited for whatever D1 had
queued (a whole 4-step sampling, ~1.7s): measured, D0b's person-detector step took 1.8s
instead of 0.1s and the pipeline ran at 3.5s/photo with the GPU idle a third of the time.
D3 has to be the one on the default stream: BiRefNet's deformable convolution
(torchvision.ops.deform_conv2d) launches on the default stream whatever torch's current
stream is, and on a side stream its mask collapsed to ~0 (no crop found). SageAttention
had the same problem and is built with scripts/sageattention-current-stream.patch, which
makes it launch on the current stream. D0b on its own stream is bit-identical (all flags,
scores and isolated pixels over the test photos).

Ticket semantics are unchanged: a cancelled job's tickets are reported failed/"cancelled"
(checked when popped and again before D1), and a stage error requeues the ticket with
retryCount + 1 until max_job_retries, then dead-letters it.
"""
import gc
import logging
import queue as pyqueue
import shutil
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

import test_extract_outfit as pipeline
from server.ai_server import worker as W
from server.common import params, queue, storage
from server.common.config import get_settings

log = logging.getLogger("ai-server.pipeline")

D1_SEED = 42


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Work:
    """One ticket on its way through the stages."""

    def __init__(self, ticket):
        self.ticket = ticket
        self.job_id, self.item_id = ticket["jobId"], ticket["itemId"]
        self.object_key = ticket["objectKey"]
        self.face_ref_key = ticket.get("faceRefKey")
        self.retry_count = ticket.get("retryCount", 0)
        self.tmp = Path(tempfile.mkdtemp(prefix="wardrobe_"))
        self.raw_path = self.tmp / f"input{Path(self.object_key).suffix or '.jpg'}"
        self.isolated_path = self.tmp / "isolated.jpg"
        self.grid_path = self.tmp / "result.png"
        self.detected = {}
        self.items, self.prompt, self.stages = [], "", []
        self.used_isolated = False
        self.d1_input = None
        self.grid = None


class Pipeline:
    def __init__(self, idle_unload_seconds):
        self.idle_unload_seconds = idle_unload_seconds
        self.q_gen = pyqueue.Queue(maxsize=1)
        self.q_post = pyqueue.Queue(maxsize=2)
        self._lock = threading.Lock()
        self._in_flight = 0
        self._last_work = time.monotonic()
        self._engine = None
        self._engine_lock = threading.Lock()
        self._gpu_dirty = False
        self._streams = {}

    def _stream(self, name):
        """This stage's own CUDA stream (created on first use, after CUDA is up)."""
        import torch
        if not torch.cuda.is_available():
            return _NullCtx()
        if name not in self._streams:
            self._streams[name] = torch.cuda.Stream()
        return torch.cuda.stream(self._streams[name])

    # ------------------------------------------------------------------ bookkeeping
    def _begin(self):
        with self._lock:
            self._in_flight += 1
            self._gpu_dirty = True

    def _end(self, w):
        shutil.rmtree(w.tmp, ignore_errors=True)
        with self._lock:
            self._in_flight -= 1
            self._last_work = time.monotonic()

    def _fail(self, w, exc):
        log.error("processing failed for job %s item %s: %s", w.job_id, w.item_id, exc, exc_info=exc)
        settings = get_settings()
        try:
            if w.retry_count < settings.max_job_retries:
                log.info("requeueing job %s item %s (retry %d)", w.job_id, w.item_id, w.retry_count + 1)
                queue.push_job(w.job_id, w.item_id, w.object_key, face_ref_key=w.face_ref_key,
                               retry_count=w.retry_count + 1)
            else:
                queue.push_dead(w.ticket)
                queue.push_result(w.job_id, w.item_id, "failed", error_reason=str(exc)[:500])
        finally:
            self._end(w)

    def _cancelled(self, w):
        log.info("job %s cancelled, skipping ticket %s", w.job_id, w.item_id)
        try:
            queue.push_result(w.job_id, w.item_id, "failed", error_reason="cancelled")
        finally:
            self._end(w)

    def _maybe_unload(self):
        if self.idle_unload_seconds <= 0:
            return
        with self._lock:
            idle = (self._gpu_dirty and self._in_flight == 0
                    and time.monotonic() - self._last_work >= self.idle_unload_seconds)
            if idle:
                self._gpu_dirty = False
        if idle:
            self.release_gpu()

    def release_gpu(self):
        """Hand all VRAM back while idle; everything reloads lazily on the next ticket."""
        import sys
        import torch
        with self._engine_lock:
            self._engine = None
        import outfit_items
        outfit_items.unload_models()
        pipeline._segmenter_cache.clear()
        classifier = sys.modules.get("wardrobe_classifier")
        if classifier is not None:
            classifier._models_cache.clear()
        try:
            import comfy.model_management as mm
            mm.unload_all_models()
        except Exception:
            pass
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        log.info("idle: D0b/D1/D3 models dropped from VRAM")

    def engine(self):
        with self._engine_lock:
            if self._engine is None:
                from d1_engine import D1Engine
                t = time.time()
                self._engine = D1Engine(cache_dir=get_settings().d1_cache_dir or None)
                self._engine.warm_up()
                log.info("D1 engine loaded in %.1fs", time.time() - t)
            return self._engine

    # ------------------------------------------------------------------ stages
    def run(self, stop_event=None):
        threading.Thread(target=self._loop, args=(self._gen_one, self.q_gen, stop_event),
                         name="d1-gen", daemon=True).start()
        threading.Thread(target=self._loop, args=(self._post_one, self.q_post, stop_event),
                         name="d3-post", daemon=True).start()
        log.info("pipelined worker starting (unload models after %.0fs idle)", self.idle_unload_seconds)
        while stop_event is None or not stop_event.is_set():
            try:
                ticket = queue.pop_job(timeout=5)
            except Exception:
                log.exception("could not read the job queue")
                time.sleep(1)
                continue
            if not ticket:
                self._maybe_unload()
                continue
            self._begin()
            w = _Work(ticket)
            try:
                if queue.is_cancelled(w.job_id):
                    self._cancelled(w)
                    continue
                with self._stream("prep"):
                    self._prepare(w)
            except Exception as exc:
                self._fail(w, exc)
                continue
            self.q_gen.put(w)

    def _loop(self, fn, q, stop_event):
        while stop_event is None or not stop_event.is_set():
            try:
                w = q.get(timeout=5)
            except pyqueue.Empty:
                continue
            try:
                fn(w)
            except Exception as exc:
                self._fail(w, exc)

    def _prepare(self, w):
        """prep: download, decode once, D0b, prompt, D1 input resize."""
        settings = get_settings()
        storage.download_to(settings.minio_raw_bucket, w.object_key, w.raw_path)
        t0 = time.perf_counter()
        image = Image.open(w.raw_path).convert("RGB")       # what D0b has always read
        detected = self._detect(image, w.face_ref_key, settings)
        isolated = detected.pop("isolated_image", None)
        w.detected = detected
        w.stages = [params.detect_stage(detected, w.raw_path, time.perf_counter() - t0)]
        w.items = pipeline.prompt_items(detected)
        if not w.items:
            return
        w.prompt = pipeline.build_prompt(detected)
        w.used_isolated = bool(detected.get("isolated_by_sam")) and isolated is not None
        # The SAM-isolated subject when there is one; otherwise the photo as ComfyUI's
        # LoadImage read it (EXIF orientation applied).
        gen = isolated if w.used_isolated else ImageOps.exif_transpose(image)
        from d1_engine import kontext_scale
        w.d1_input = kontext_scale(gen)
        if settings.wardrobe_report_dir and w.used_isolated:
            isolated.save(w.isolated_path, quality=90)

    def _detect(self, image, face_ref_key, settings):
        """D0b in-process; same fallback as before: a reference that matches nobody (or
        cannot be fetched) means "largest person in frame"."""
        import outfit_items
        selfie = None
        if face_ref_key:
            try:
                selfie = str(W._cached_face_ref(face_ref_key, settings))
            except Exception:
                log.warning("face reference %s could not be fetched; using largest person in frame",
                            face_ref_key)
        if selfie:
            try:
                return outfit_items.detect_worn_items(image, selfie_path=selfie, return_isolated=True)
            except Exception as exc:
                log.info("no face matched the reference (%s); using largest person in frame", exc)
        return outfit_items.detect_worn_items(image, return_isolated=True)

    def _gen_one(self, w):
        """gen: D1."""
        if w.items:
            if queue.is_cancelled(w.job_id):
                self._cancelled(w)
                return
            with self._stream("gen"):
                eng = self.engine()
            timing = {}
            t0 = time.perf_counter()
            with self._stream("gen"):
                w.grid = eng.generate(w.d1_input, w.prompt, seed=D1_SEED, timing=timing, prescaled=True)
            w.stages.append(params.generate_stage_engine(eng.settings(D1_SEED), w.used_isolated,
                                                         w.grid.size, time.perf_counter() - t0, timing))
            w.d1_input = None
        self.q_post.put(w)

    def _post_one(self, w):
        """post: D3a/D3b, same-image D2, uploads, report, result."""
        settings = get_settings()
        garments = self._finish(w, settings)     # default stream - see the module docstring
        queue.push_result(w.job_id, w.item_id, "success", garments=garments)
        log.info("job %s item %s done: %d garment(s)", w.job_id, w.item_id, len(garments))
        self._end(w)

    def _finish(self, w, settings):
        if not w.items:
            log.info("job %s item %s: no garment detected, nothing to extract", w.job_id, w.item_id)
            W._write_report(w.job_id, w.item_id, w.raw_path, w.isolated_path, w.grid_path,
                            [], [], [], w.detected, w.items, "", w.stages, settings, w.object_key)
            return []
        if settings.wardrobe_report_dir:
            w.grid.save(w.grid_path, compress_level=1)
        crop_dir = w.tmp / "items"
        t0 = time.perf_counter()
        crop_timing = {}
        crops = pipeline.crop_items(w.grid, crop_dir, items=w.items, device=None, timing=crop_timing)
        w.stages.append(params.crop_stage(w.items, crops, time.perf_counter() - t0, crop_timing))
        t0 = time.perf_counter()
        classify_timing = {}
        records = (pipeline.classify_crops(crop_dir, device=None, timing=classify_timing)
                   if crops else [])
        w.stages.append(params.classify_stage(records, time.perf_counter() - t0, classify_timing))
        if settings.wardrobe_dedup_enabled:
            t0 = time.perf_counter()
            kept = W._drop_same_image_duplicates(records) if records else []
            w.stages.append(params.dedup_stage(records, kept, W.SAME_IMAGE_DEDUP_THRESHOLD,
                                               settings.wardrobe_dedup_threshold,
                                               time.perf_counter() - t0))
        else:
            kept = list(records)
        W._write_report(w.job_id, w.item_id, w.raw_path, w.isolated_path, w.grid_path,
                        crops, records, kept, w.detected, w.items, w.prompt, w.stages, settings,
                        w.object_key)
        garments = []
        for idx, record in enumerate(kept, start=1):
            crop = next((c for c in crops if c["path"].name == record["image_name"]), None)
            if crop is None:
                continue
            dest_key = storage.item_object_key(w.job_id, w.item_id, idx)
            storage.upload_file(settings.minio_items_bucket, dest_key, crop["path"])
            garments.append({
                "objectKey": dest_key,
                "tags": {
                    "type": record["type"], "category": record["category"],
                    "sub_category": record["sub_category"], "gender": record["gender"],
                    "color": record["color"], "neck": record["neck"],
                    "sleeve": record["sleeve"], "pattern": record["pattern"],
                },
                "description": record["original_text"],
                "visualEmbedding": record["visual_embedding"],
                "textEmbedding": record["text_embedding"],
            })
        return garments


def run(stop_event=None):
    Pipeline(W.IDLE_UNLOAD_SECONDS).run(stop_event)
