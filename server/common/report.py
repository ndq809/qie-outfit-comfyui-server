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
# Written by data-server once it has run wardrobe-level D2 on this photo's garments:
# {objectKey: {"added": bool, "duplicateOf": id|null, "score": float|null}}.
WARDROBE = "wardrobe.json"


def save_stages(report_dir: str, job_id: str, item_id: str, *, original: Path,
                isolated: Path | None, grid: Path, crops: list, records: list,
                object_keys: dict, detected: dict, prompt: str) -> Path:
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
            "duplicate": name not in object_keys,
            "objectKey": object_keys.get(name),
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


def mark_wardrobe(report_dir: str, job_id: str, item_id: str, outcomes: dict):
    """Record which garments data-server actually put in the wardrobe. A garment dropped
    as a duplicate of something already owned is never stored in postgres, so this is
    the only place the page can learn why it is missing."""
    out = Path(report_dir) / job_id / _safe(item_id)
    if out.is_dir():
        (out / WARDROBE).write_text(json.dumps(outcomes, indent=1), encoding="utf-8")


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
        try:
            outcomes = json.loads((here / WARDROBE).read_text(encoding="utf-8"))
        except Exception:
            outcomes = {}
        for it in data.get("items", []):
            it["wardrobe"] = outcomes.get(it.get("objectKey") or "")
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


def _group_by_job(photos: list):
    """Newest job first, photos inside it by id. A flat list stopped working once the
    same photos had been re-run a dozen times: the question being asked of this page is
    always "what did photo N look like in run X", never "what happened at 14:52"."""
    jobs = {}
    for p in photos:
        jobs.setdefault(p.get("jobId") or p["_dir"].split("/")[0], []).append(p)
    ordered = sorted(jobs.items(),
                     key=lambda kv: max(x.get("at") or "" for x in kv[1]), reverse=True)
    return [(job, sorted(group, key=lambda x: _photo_sort_key(x.get("itemId", ""))))
            for job, group in ordered]


def _photo_sort_key(name: str):
    return (0, int(name)) if name.isdigit() else (1, name)


def _render(photos: list) -> str:
    all_items = [i for p in photos for i in p.get("items", [])]
    total_items = len(all_items)
    total_added = sum(1 for i in all_items if (i.get("wardrobe") or {}).get("added"))
    total_dupes = sum(1 for i in all_items if i.get("duplicate") or
                      (i.get("wardrobe") or {}).get("added") is False)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    groups = _group_by_job(photos)

    sections = []
    for job, group in groups:
        when = max(x.get("at") or "" for x in group)[:19].replace("T", " ")
        crops = [i for x in group for i in x.get("items", [])]
        kept = sum(1 for i in crops if (i.get("wardrobe") or {}).get("added"))
        dupes = sum(1 for i in crops if i.get("duplicate") or (i.get("wardrobe") or {}).get("added") is False)
        empty = sum(1 for x in group if not x.get("items"))
        chips = " ".join(
            f'<a class="jump" href="#p-{html.escape(job[:8])}-{html.escape(str(x.get("itemId")))}">'
            f'{html.escape(str(x.get("itemId")))}</a>' for x in group)
        cards = "\n".join(_photo_card(x, job) for x in group)
        sections.append(f"""<section class="job">
<h2>Job {html.escape(job)}</h2>
<p class="meta">cập nhật {html.escape(when)} UTC &middot; {len(group)} ảnh gốc
({empty} ảnh không tách được trang phục) &rarr; {len(crops)} trang phục tách ra
&rarr; <b>{kept} vào tủ đồ</b> ({dupes} bị loại vì trùng)</p>
<p class="jump-row">Ảnh trong job: {chips}</p>
{cards}
</section>""")

    body = "\n".join(sections) or (
        '<p class="empty">Chưa có ảnh nào. Chạy một job với WARDROBE_REPORT_DIR đã cấu hình rồi tải lại trang.</p>')
    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wardrobe Pipeline Report</title>
<style>
:root{{--bg:#f6f6f8;--card:#fff;--ink:#222;--muted:#666;--soft:#888;--line:#eee;--chip:#f3f3f6;
  --accent:#4f46e5;--ok:#1a7f37;--warn:#b4541f}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#16161a;--card:#1f1f24;
  --ink:#ececf0;--muted:#a3a3ad;--soft:#8b8b95;--line:#2f2f37;--chip:#27272e;--accent:#a5a0ff;--ok:#5cc27a;--warn:#e0895a}}}}
:root[data-theme="dark"]{{--bg:#16161a;--card:#1f1f24;--ink:#ececf0;--muted:#a3a3ad;--soft:#8b8b95;
  --line:#2f2f37;--chip:#27272e;--accent:#a5a0ff;--ok:#5cc27a;--warn:#e0895a}}
*{{box-sizing:border-box}}
body{{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0 auto;padding:24px 16px;
  max-width:1500px;background:var(--bg);color:var(--ink)}}
h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:var(--muted);margin:0 0 16px}}
section.job{{background:var(--card);border-radius:12px;padding:16px 20px;margin-bottom:24px}}
section.job>h2{{font-size:15px;margin:0 0 4px;font-family:ui-monospace,monospace;word-break:break-all}}
.meta{{color:var(--muted);margin:0 0 8px}}
.jump-row{{margin:0 0 12px;color:var(--muted);font-size:12px}}
.jump{{font-family:ui-monospace,monospace;background:var(--chip);border:1px solid var(--line);
  border-radius:6px;padding:1px 7px;color:var(--ink);text-decoration:none}}
.jump:hover{{border-color:var(--accent)}}
.photo{{border:1px solid var(--line);border-radius:12px;padding:14px;margin-top:14px;scroll-margin-top:70px}}
.photo:target{{outline:2px solid var(--accent);outline-offset:2px}}
.photo>header{{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:baseline;margin-bottom:10px}}
.photo h3{{margin:0;font-size:16px;font-family:ui-monospace,monospace}}
.flow{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr)) minmax(0,2fr);gap:10px;align-items:start}}
.step h4{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 6px;font-weight:600}}
.step h4 .arrow{{color:var(--accent)}}
.step img{{width:100%;aspect-ratio:1;object-fit:contain;background:var(--chip);border:1px solid var(--line);
  border-radius:10px;display:block}}
.na{{aspect-ratio:1;display:flex;align-items:center;justify-content:center;text-align:center;
  color:var(--soft);font-size:12px;border:1px dashed var(--line);border-radius:10px;padding:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
figure{{margin:0;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--card)}}
figure img{{border:0;border-radius:0;background:#fff}}
figcaption{{padding:6px 8px;font-size:12px;line-height:1.4}}
figcaption span{{color:var(--soft)}}
figcaption small{{color:var(--muted)}}
figcaption .desc{{color:var(--soft);font-size:11px;margin-top:3px}}
figure.dupe{{outline:2px solid var(--warn);outline-offset:-2px;opacity:.75}}
.d2{{margin-top:4px;font-size:11px}} .ok{{color:var(--ok);font-weight:600}} .pending{{color:var(--soft)}}
.badge{{display:inline-block;background:var(--warn);color:#fff;font-size:10px;padding:1px 5px;border-radius:4px}}
.chips{{display:flex;flex-wrap:wrap;gap:5px;margin:12px 0 0}}
.chip{{background:var(--chip);border:1px solid var(--line);border-radius:999px;padding:2px 9px;font-size:12px;color:var(--muted)}}
.chip b{{color:var(--ink);font-weight:600}}
.prompt{{margin-top:10px}}
.prompt h4{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 4px}}
pre{{background:var(--chip);border:1px solid var(--line);border-radius:8px;padding:10px;margin:0;
  font-size:12px;white-space:pre-wrap;word-break:break-word}}
.toolbar{{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0;margin-bottom:10px;
  display:flex;gap:10px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line)}}
.toolbar input{{font:inherit;padding:7px 11px;border-radius:8px;border:1px solid var(--line);
  background:var(--card);color:var(--ink);min-width:240px}}
.toolbar .hint{{color:var(--muted);font-size:12px}}
.empty{{color:var(--muted)}}
@media (max-width:900px){{.flow{{grid-template-columns:repeat(3,minmax(0,1fr))}} .flow .items{{grid-column:1/-1}}}}
@media (max-width:520px){{.flow{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>Wardrobe — luồng xử lý ảnh</h1>
<p class="sub">Mỗi ảnh theo đúng thứ tự pipeline: ảnh gốc &rarr; ảnh isolate (SAM) &rarr; ảnh grid trang phục (D1)
&rarr; từng trang phục tách ra + phân loại (D3). &nbsp;<b>{len(photos)}</b> ảnh &middot; <b>{len(groups)}</b> job &middot;
<b>{total_items}</b> trang phục tách ra &middot; <b>{total_added}</b> vào tủ đồ &middot;
<b>{total_dupes}</b> bị loại vì trùng (D2) &middot; tạo lúc {generated}</p>
<div class="toolbar">
  <input id="q" type="search" placeholder="Lọc theo id ảnh, ví dụ 9652" autocomplete="off">
  <span class="hint" id="qhint">Nhập id để chỉ hiện ảnh đó (ở mọi job).</span>
</div>
{body}
<script>
const q=document.getElementById('q'),hint=document.getElementById('qhint');
function apply(){{
  const term=q.value.trim().toLowerCase();let total=0;
  document.querySelectorAll('section.job').forEach(job=>{{
    let shown=0;
    job.querySelectorAll('.photo').forEach(c=>{{
      const hit=!term||(c.dataset.photo||'').toLowerCase().includes(term);
      c.hidden=!hit;if(hit)shown++;
    }});
    job.hidden=!!term&&shown===0;total+=shown;
  }});
  hint.textContent=term?`${{total}} ảnh khớp "${{q.value.trim()}}"`:'Nhập id để chỉ hiện ảnh đó (ở mọi job).';
  try{{history.replaceState(null,'',term?'#id='+encodeURIComponent(q.value.trim()):location.pathname+location.search)}}catch(e){{}}
}}
q.addEventListener('input',apply);
function fromHash(){{const m=location.hash.match(/^#id=(.+)$/);if(m){{q.value=decodeURIComponent(m[1]);apply();}}}}
fromHash();window.addEventListener('hashchange',fromHash);
</script>
</body></html>
"""


def _photo_card(p: dict, job: str = "") -> str:
    d = p["_dir"]
    photo_id = str(p.get("itemId", "?"))
    anchor = f"p-{job[:8]}-{photo_id}" if job else f"p-{photo_id}"
    det = p.get("detected") or {}
    flags = [k for k in ("headwear", "outer", "one_piece", "top", "bottom", "bag", "footwear") if det.get(k)]
    chips = [_chip("detector nhận", ", ".join(flags) or "không có")]
    scores = det.get("scores") or {}
    if scores:
        chips.append(_chip("điểm", ", ".join(f"{k} {v:.2f}" for k, v in scores.items())))
    if det.get("persons") is not None:
        chips.append(_chip("số người", det["persons"]))
    if det.get("face_similarity") is not None:
        chips.append(_chip("khớp khuôn mặt", f'{det["face_similarity"]:.3f}'))
    chips.append(_chip("SAM", "đã cô lập" if det.get("isolated_by_sam") else "không"))
    asked = p.get("askedItems") or []
    items = p.get("items", [])
    chips.append(_chip("prompt xin / tách được", f"{len(asked)} / {len(items)}"))

    original = _stage_img(d, p, "_original", "ảnh gốc") or '<div class="na">không có ảnh gốc</div>'
    isolated = _stage_img(d, p, "_isolated", "ảnh isolate") or \
        '<div class="na">không cần isolate<br>(chỉ 1 người, lọc theo box người)</div>'
    grid = _stage_img(d, p, "_grid", "ảnh grid") or \
        '<div class="na">không sinh grid<br>(không phát hiện trang phục)</div>'
    tiles = "".join(_item_tile(d, it) for it in items) or \
        '<div class="na">không có trang phục</div>'
    prompt = p.get("prompt") or ""

    return f"""<article class="photo" id="{html.escape(anchor)}" data-photo="{html.escape(photo_id)}">
<header><h3>ID {html.escape(photo_id)}</h3>
<span class="meta">job {html.escape(str(p.get("jobId", "?"))[:8])} &middot; {html.escape(str(p.get("at", "")))}</span></header>
<div class="flow">
  <div class="step"><h4>1 &middot; Ảnh gốc</h4>{original}</div>
  <div class="step"><h4><span class="arrow">&rarr;</span> 2 &middot; Ảnh isolate</h4>{isolated}</div>
  <div class="step"><h4><span class="arrow">&rarr;</span> 3 &middot; Grid trang phục</h4>{grid}</div>
  <div class="step items"><h4><span class="arrow">&rarr;</span> 4 &middot; Trang phục tách ra ({len(items)})</h4>
    <div class="grid">{tiles}</div></div>
</div>
<div class="chips">{"".join(chips)}</div>
<div class="prompt"><h4>Prompt đã dùng để tách trang phục</h4>
<pre>{html.escape(prompt) if prompt else "(không gọi D1 — không có trang phục để tách)"}</pre></div>
</article>"""


def _stage_img(d: str, p: dict, key: str, alt: str) -> str:
    full = p.get(key)
    if not full:
        return ""
    shown = p.get(key + "_preview") or full
    return (f'<a href="{d}/{full}" target="_blank" rel="noopener">'
            f'<img src="{d}/{shown}" alt="{alt}" loading="lazy"></a>')


def _item_tile(d: str, it: dict) -> str:
    colors = ", ".join(f'{c.get("color", "")} {c.get("confidence", 0):.0f}%' for c in (it.get("color") or []))
    extra = " · ".join((it.get("neck") or []) + (it.get("sleeve") or []) + (it.get("pattern") or []))
    cat = " / ".join(x for x in (it.get("category"), it.get("sub_category")) if x)
    w = it.get("wardrobe") or {}
    if it.get("duplicate"):
        badge, dupe = '<span class="badge">trùng món khác trong cùng ảnh, đã loại</span>', True
    elif w.get("added") is False:
        badge = (f'<span class="badge">trùng đồ đã có trong tủ (sim {w.get("score", 0):.3f}), '
                 'không thêm</span>')
        dupe = True
    elif w.get("added"):
        badge, dupe = '<span class="ok">&#10003; đã vào tủ đồ</span>', False
    else:
        badge, dupe = '<span class="pending">chưa có kết quả D2</span>', False
    return f"""<figure{' class="dupe"' if dupe else ''}>
<a href="{d}/{it['file']}" target="_blank" rel="noopener"><img src="{d}/{it['file']}" alt="{html.escape(str(it.get('label') or ''))}" loading="lazy"></a>
<figcaption><b>{html.escape(str(it.get('type') or '?'))}</b> <span>{html.escape(str(it.get('gender') or ''))}</span><br>
{html.escape(colors)}<br><small>{html.escape(extra)}</small>
<div class="desc">{html.escape(cat)}<br>ô prompt: {html.escape(str(it.get('label') or '?'))}</div>
<div class="d2">{badge}</div></figcaption></figure>"""


def _chip(k, v) -> str:
    return f'<span class="chip">{html.escape(str(k))} <b>{html.escape(str(v))}</b></span>'
