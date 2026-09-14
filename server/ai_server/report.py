"""Test-only visual record of what D0b/D1/D3 did to each photo.

The worker normally keeps nothing: it works in a temp dir and the original is deleted
from object-storage as soon as extraction succeeds (wardrobe-system-spec.md, "Bảo mật
và vòng đời dữ liệu"). That makes it impossible to see *why* a wardrobe item came out
wrong - whether the subject was isolated correctly, what the generator drew, or how the
grid was split. With WARDROBE_REPORT_DIR set, every stage is kept side by side instead:

    original photo -> SAM-isolated subject -> generated garment grid -> per-item crops

Keep it empty in production: it retains the user's original photo on disk.
"""
import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

# The page shows a downscaled copy and links to the file itself. A phone original is
# ~7MB, so a dozen photos would otherwise be a ~100MB page for images displayed a few
# hundred pixels wide.
PREVIEW_MAX_PX = 1000
THUMB_SUFFIX = ".preview.jpg"

STAGE_ORIGINAL = "01_original"
STAGE_ISOLATED = "02_isolated.png"
STAGE_GRID = "03_grid.png"
ITEMS_DIR = "items"
RECORD = "record.json"


def save_stages(report_dir: str, job_id: str, item_id: str, *, original: Path,
                isolated: Path | None, grid: Path, crops: list, records: list,
                kept_names: set, detected: dict, prompt: str) -> Path:
    """Copy one photo's four stages into <report_dir>/<job_id>/<item_id>/ and write the
    metadata the page needs beside them. Never raises into the worker: a broken report
    must not fail a job that otherwise succeeded."""
    out = Path(report_dir) / job_id / _safe(item_id)
    (out / ITEMS_DIR).mkdir(parents=True, exist_ok=True)

    _keep(original, out / f"{STAGE_ORIGINAL}{original.suffix.lower() or '.jpg'}")
    if isolated is not None and isolated.exists():
        _keep(isolated, out / STAGE_ISOLATED)
    if grid.exists():
        _keep(grid, out / STAGE_GRID)

    by_name = {r["image_name"]: r for r in records}
    items = []
    for crop in crops:
        name = crop["path"].name
        shutil.copyfile(crop["path"], out / ITEMS_DIR / name)
        rec = by_name.get(name, {})
        items.append({
            "file": f"{ITEMS_DIR}/{name}",
            "label": crop.get("label"),
            "box": crop.get("box"),
            # A crop the generator drew twice: classified, then dropped before the
            # wardrobe ever saw it. Worth showing - it explains a missing item.
            "duplicate": name not in kept_names,
            "type": rec.get("type"), "category": rec.get("category"),
            "sub_category": rec.get("sub_category"), "gender": rec.get("gender"),
            "color": rec.get("color"), "neck": rec.get("neck"),
            "sleeve": rec.get("sleeve"), "pattern": rec.get("pattern"),
            "description": rec.get("original_text"),
        })

    (out / RECORD).write_text(json.dumps({
        "jobId": job_id, "itemId": item_id,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "detected": {k: v for k, v in detected.items()
                     if k not in ("timing",) and not k.startswith("_")},
        "prompt": prompt,
        "askedItems": detected.get("_asked_items"),
        "items": items,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _keep(src: Path, dest: Path):
    """Store the file as-is, plus a downscaled JPEG beside it for the page to display."""
    shutil.copyfile(src, dest)
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((PREVIEW_MAX_PX, PREVIEW_MAX_PX))
            im.save(dest.with_suffix(dest.suffix + THUMB_SUFFIX), quality=82, optimize=True)
    except Exception:
        pass  # page falls back to the full file


def _preview_or_self(dir_path: Path, name: str) -> str:
    return name + THUMB_SUFFIX if (dir_path / (name + THUMB_SUFFIX)).exists() else name


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:120]


# --- the page ----------------------------------------------------------------------

def build_index(report_dir: str) -> Path:
    root = Path(report_dir)
    root.mkdir(parents=True, exist_ok=True)
    photos = []
    for rec_path in sorted(root.glob(f"*/*/{RECORD}")):
        try:
            data = json.loads(rec_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        here = rec_path.parent
        data["_dir"] = here.relative_to(root).as_posix()
        data["_original"] = next(
            (p.name for p in sorted(here.glob(f"{STAGE_ORIGINAL}.*"))
             if not p.name.endswith(THUMB_SUFFIX)), None)
        data["_isolated"] = STAGE_ISOLATED if (here / STAGE_ISOLATED).exists() else None
        data["_grid"] = STAGE_GRID if (here / STAGE_GRID).exists() else None
        for key in ("_original", "_isolated", "_grid"):
            data[key + "_preview"] = (
                _preview_or_self(here, data[key]) if data.get(key) else None)
        photos.append(data)

    photos.sort(key=lambda d: (d.get("at") or "", d["_dir"]), reverse=True)
    out = root / "index.html"
    out.write_text(_render(photos), encoding="utf-8")
    return out


def _render(photos: list) -> str:
    total_items = sum(len(p.get("items", [])) for p in photos)
    total_dupes = sum(1 for p in photos for i in p.get("items", []) if i.get("duplicate"))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = "\n".join(_photo_card(p) for p in photos) or (
        '<p class="empty">No photos recorded yet. Run a job with WARDROBE_REPORT_DIR set, '
        'then reload.</p>')
    return f"""<title>Wardrobe Extraction Report</title>
<style>
:root {{
  --bg:#f7f7f5; --card:#fff; --ink:#1a1a18; --muted:#6b6b66; --line:#e2e2dd;
  --accent:#7c5cff; --warn:#b4541f; --chip:#f0f0ec;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg:#16161a; --card:#1e1e23; --ink:#eceCf0; --muted:#9a9aa4; --line:#2e2e36;
    --accent:#a08cff; --warn:#e0895a; --chip:#26262d;
  }}
}}
:root[data-theme="dark"] {{
  --bg:#16161a; --card:#1e1e23; --ink:#ececf0; --muted:#9a9aa4; --line:#2e2e36;
  --accent:#a08cff; --warn:#e0895a; --chip:#26262d;
}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--ink);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  padding-block:28px;padding-left:20px;padding-right:20px;max-width:1500px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:var(--muted);margin:0 0 24px}}
.sub b{{color:var(--ink)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:22px}}
.card > header{{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline;margin-bottom:14px}}
.card h2{{font-size:15px;margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
.meta{{color:var(--muted);font-size:12px}}
.stages{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}}
.stage{{min-width:0}}
.stage h3{{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
  margin:0 0 6px;font-weight:600}}
.stage img{{width:100%;max-width:100%;border-radius:9px;border:1px solid var(--line);
  background:var(--chip);display:block}}
.na{{color:var(--muted);font-size:12px;border:1px dashed var(--line);border-radius:9px;
  padding:20px 10px;text-align:center}}
.items{{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:10px}}
.item{{border:1px solid var(--line);border-radius:9px;overflow:hidden;background:var(--chip)}}
.item img{{width:100%;display:block;border:0;border-radius:0}}
.item .cap{{padding:6px 7px;font-size:11px;line-height:1.35}}
.item .t{{font-weight:600;word-break:break-word}}
.item .c{{color:var(--muted)}}
.dupe{{outline:2px solid var(--warn);outline-offset:-2px}}
.badge{{display:inline-block;background:var(--warn);color:#fff;font-size:10px;
  padding:1px 5px;border-radius:4px;margin-top:3px}}
.chips{{display:flex;flex-wrap:wrap;gap:5px;margin-top:12px}}
.chip{{background:var(--chip);border:1px solid var(--line);border-radius:999px;
  padding:2px 9px;font-size:11px;color:var(--muted)}}
.chip b{{color:var(--ink);font-weight:600}}
details{{margin-top:10px}}
summary{{cursor:pointer;font-size:12px;color:var(--accent)}}
pre{{background:var(--chip);border:1px solid var(--line);border-radius:8px;padding:10px;
  font-size:11.5px;white-space:pre-wrap;word-break:break-word;margin:8px 0 0}}
.empty{{color:var(--muted)}}
@media (max-width:560px){{ .stages{{grid-template-columns:1fr}} }}
</style>
<h1>Wardrobe Extraction Report</h1>
<p class="sub">Every stage of each photo, in pipeline order: original &rarr; isolated subject
&rarr; generated grid &rarr; per-item crops. &nbsp;<b>{len(photos)}</b> photos &middot;
<b>{total_items}</b> crops &middot; <b>{total_dupes}</b> dropped as duplicates &middot;
built {generated}</p>
{body}
"""


def _photo_card(p: dict) -> str:
    d = p["_dir"]
    det = p.get("detected") or {}
    flags = [k for k in ("headwear", "outer", "one_piece", "top", "bottom", "bag", "footwear")
             if det.get(k)]
    chips = [_chip("detected", ", ".join(flags) or "none")]
    if det.get("gender"):
        chips.append(_chip("gender", det["gender"]))
    if det.get("persons") is not None:
        chips.append(_chip("persons", det["persons"]))
    if det.get("face_similarity") is not None:
        chips.append(_chip("face sim", det["face_similarity"]))
    if det.get("isolated_by_sam"):
        chips.append(_chip("SAM", "isolated"))
    asked = p.get("askedItems")
    if asked:
        chips.append(_chip("asked", f'{len(asked)} &rarr; got {len(p.get("items", []))}'))

    isolated = _stage_img(d, p, "_isolated", "isolated subject") or \
        '<div class="na">not isolated<br>(single person in frame)</div>'
    grid = _stage_img(d, p, "_grid", "generated grid") or '<div class="na">no grid</div>'
    original = _stage_img(d, p, "_original", "original photo") or \
        '<div class="na">no original</div>'

    items = "".join(_item_tile(d, it) for it in p.get("items", [])) or \
        '<div class="na">no items extracted</div>'

    return f"""<section class="card">
<header>
  <h2>{html.escape(str(p.get("itemId", "?")))}</h2>
  <span class="meta">job {html.escape(str(p.get("jobId", "?"))[:8])} &middot; {html.escape(str(p.get("at", "")))}</span>
</header>
<div class="stages">
  <div class="stage"><h3>1 &middot; Original</h3>{original}</div>
  <div class="stage"><h3>2 &middot; Isolated subject</h3>{isolated}</div>
  <div class="stage"><h3>3 &middot; Generated grid</h3>{grid}</div>
  <div class="stage"><h3>4 &middot; Items ({len(p.get("items", []))})</h3>
    <div class="items">{items}</div></div>
</div>
<div class="chips">{"".join(chips)}</div>
<details><summary>D1 prompt</summary><pre>{html.escape(p.get("prompt") or "")}</pre></details>
</section>"""


def _stage_img(d: str, p: dict, key: str, alt: str) -> str:
    """Displayed small, linked to the file itself for a full-size look."""
    full = p.get(key)
    if not full:
        return ""
    shown = p.get(key + "_preview") or full
    return (f'<a href="{d}/{full}" target="_blank" rel="noopener">'
            f'<img src="{d}/{shown}" alt="{alt}" loading="lazy"></a>')


def _item_tile(d: str, it: dict) -> str:
    colors = ", ".join(c.get("color", "") for c in (it.get("color") or [])[:2])
    extra = ", ".join(x for x in ((it.get("sleeve") or []) + (it.get("neck") or [])
                                  + (it.get("pattern") or [])))
    badge = '<div class="badge">duplicate, dropped</div>' if it.get("duplicate") else ""
    return f"""<div class="item{' dupe' if it.get('duplicate') else ''}">
<a href="{d}/{it['file']}" target="_blank" rel="noopener">
  <img src="{d}/{it['file']}" alt="{html.escape(str(it.get('label') or ''))}" loading="lazy"></a>
<div class="cap"><div class="t">{html.escape(str(it.get('type') or '?'))}</div>
<div class="c">{html.escape(colors)}</div>
{f'<div class="c">{html.escape(extra)}</div>' if extra else ''}
{badge}</div></div>"""


def _chip(k, v) -> str:
    return f'<span class="chip">{html.escape(str(k))} <b>{v}</b></span>'
