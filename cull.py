#!/usr/bin/env python3
"""Photo culling: hybrid local + Claude pipeline.

Phase 1 (local, free):  SigLIP aesthetic score + Laplacian sharpness + EXIF datetime.
Phase 2 (free):         Burst dedup — group photos shot within --dedup-window seconds,
                        keep highest local-score per group.
Phase 3 (Claude):       Vision review on survivors with score >= --local-min.
                        Claude returns {keep, score, issues, crop, note}.
Phase 4 (output):       Copy / symlink kept originals + .xmp sidecar with Claude's crop.

Originals are never modified. Run with --no-claude to do local-only.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

import piexif

import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

import numpy as np
import torch
from PIL import Image, ImageOps

import pillow_heif
pillow_heif.register_heif_opener()

import rawpy
import cv2
import exifread

from aesthetic_predictor_v2_5 import convert_v2_5_from_siglip


SUPPORTED_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp",
    ".heic", ".heif",
    ".dng", ".raf", ".cr3", ".cr2", ".arw", ".nef", ".orf", ".rw2", ".pef",
}
RAW_EXTS = {".dng", ".raf", ".cr3", ".cr2", ".arw", ".nef", ".orf", ".rw2", ".pef"}

CLAUDE_MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}

CLAUDE_SYSTEM = """You are a STRICT photo curator. Your default verdict is REJECT. The user has explicitly asked you: "宁可错杀十张也不放进一张废片." Expect to reject 85-95% of photos shown to you.

Before deciding, answer in one concrete sentence: "What does this photo SAY?" — meaning the actual content/idea/event, not a description of pixels. If you cannot articulate a real reason for the photo to exist beyond "I was there," REJECT.

KEEP only if AT LEAST ONE is genuinely true (not "kind of"):
- Decisive moment: action/expression captured at peak (gesture, glance, collision of elements)
- Emotional density: a face/body language that carries weight beyond "looks happy / looks pleasant"
- Visual tension: light, shadow, geometry, color, or juxtaposition exerting real pressure on the eye
- Narrative pull: a viewer immediately wants to ask "who? where? why?"
- Distinct seeing: not the obvious tourist angle of this place

REJECT (default) for ANY of:
- Pleasant-but-empty travel record: skylines with figure, harbor scenes, sunsets, "we were here" panoramas
- Document shots: hotel meals, menu signage, museum displays, neon storefronts shot head-on
- Posed cuteness with no moment: kid smiling at camera, family photo, "look at me" portrait
- Technically clean but visually generic
- Pretty light wasted on a subject the light isn't actually doing anything to
- Anything you'd swipe past in a photo book without pausing

Score scale (1-10):
- 1-3: trash (broken OR utterly empty)
- 4-5: technically OK but says nothing — REJECT
- 6: has something but borderline — usually REJECT
- 7: real moment / emotion / tension — KEEP threshold
- 8-9: strong, portfolio-worthy
- 10: rare, masterpiece-class

If keeping, suggest a crop ONLY if it materially intensifies the photo. For already-strong compositions, crop=null.

Crop rules:
- Coordinates normalized 0-1: left, top, right, bottom (0,0 is top-left)
- left < right and top < bottom
- Must retain at least 50% of original area
- Prefer common ratios: 4:5, 5:4, 1:1, 3:2, 2:3, 16:9

Output JSON only, no preamble, no markdown fence:
{"what_it_says": "one concrete sentence — the photo's actual idea, not pixel description", "keep": bool, "score": int 1-10, "reject_reason": "string or null — be specific and honest, not polite", "issues": [str], "crop": null | {"left": float, "top": float, "right": float, "bottom": float, "ratio": str, "why": str}, "note": "brief, blunt allowed"}

Issue tags: eyes_closed, out_of_focus, motion_blur, cut_off, distracting, tilted, exposure, generic, posed, empty, cliche, no_moment.

Be honest. The user is a serious photographer asking for harsh selection, not validation."""


def load_anthropic_key():
    """Find ANTHROPIC_API_KEY in env, or .env files at common locations."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    candidates = [
        Path.cwd() / ".env",
        Path.cwd() / ".env.local",
        Path.home() / ".anthropic" / ".env",
        Path.home() / ".config" / "anthropic" / ".env",
    ]
    extra = os.environ.get("PHOTO_CULL_ENV_FILE")
    if extra:
        candidates.insert(0, Path(extra).expanduser())
    for envfile in candidates:
        if not envfile.is_file():
            continue
        for line in envfile.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v.startswith("sk-"):
                    os.environ["ANTHROPIC_API_KEY"] = v
                    return True
    return False


def pick_device():
    if torch.cuda.is_available():
        return "cuda", torch.bfloat16
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps", torch.float32
    return "cpu", torch.float32


def load_image(path: Path) -> Image.Image | None:
    """Load to RGB PIL image with EXIF orientation honored (so Claude sees upright)."""
    ext = path.suffix.lower()
    try:
        if ext in RAW_EXTS:
            with rawpy.imread(str(path)) as raw:
                try:
                    thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        img = Image.open(BytesIO(thumb.data))
                        return ImageOps.exif_transpose(img).convert("RGB")
                except Exception:
                    pass
                rgb = raw.postprocess(use_camera_wb=True, output_bps=8, no_auto_bright=False)
                return Image.fromarray(rgb)
        img = Image.open(path)
        return ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:
        print(f"  ! load failed: {path.name}: {e}", file=sys.stderr)
        return None


def get_exif_orientation(path: Path) -> int:
    """Return EXIF Orientation tag (1=normal, 3=180, 6=90CW, 8=90CCW). Default 1."""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            return int(exif.get(274, 1))
    except Exception:
        return 1


def get_exif_datetime(path: Path) -> datetime | None:
    """Return datetime of exposure or None. Uses PIL for JPG/HEIC, exifread for RAW."""
    try:
        ext = path.suffix.lower()
        if ext in RAW_EXTS:
            with open(path, "rb") as f:
                tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
            tag = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
            if tag:
                return datetime.strptime(str(tag), "%Y:%m:%d %H:%M:%S")
        else:
            img = Image.open(path)
            exif = img.getexif()
            dt = exif.get(36867) or exif.get(36868) or exif.get(306)
            if dt:
                return datetime.strptime(dt, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return None


def laplacian_var(img: Image.Image) -> float:
    side = 800
    w, h = img.size
    if max(w, h) > side:
        if w >= h:
            img = img.resize((side, int(h * side / w)), Image.LANCZOS)
        else:
            img = img.resize((int(w * side / h), side), Image.LANCZOS)
    gray = np.asarray(img.convert("L"), dtype=np.uint8)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def write_xmp_sidecar(target_path: Path, crop_box: tuple, score: float, note: str = ""):
    left, top, right, bottom = crop_box
    rating = max(1, min(5, int(round(score / 2))))
    safe_note = (note or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xmp = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="photo-cull">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"\n'
        '    xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
        '    xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '   <crs:HasCrop>True</crs:HasCrop>\n'
        f'   <crs:CropTop>{top:.4f}</crs:CropTop>\n'
        f'   <crs:CropLeft>{left:.4f}</crs:CropLeft>\n'
        f'   <crs:CropBottom>{bottom:.4f}</crs:CropBottom>\n'
        f'   <crs:CropRight>{right:.4f}</crs:CropRight>\n'
        '   <crs:CropAngle>0</crs:CropAngle>\n'
        '   <crs:CropConstrainToWarp>0</crs:CropConstrainToWarp>\n'
        f'   <xmp:Rating>{rating}</xmp:Rating>\n'
        f'   <dc:description>{safe_note}</dc:description>\n'
        '  </rdf:Description>\n'
        ' </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>\n'
    )
    sidecar = target_path.with_suffix(target_path.suffix + ".xmp")
    sidecar.write_text(xmp, encoding="utf-8")


JPEGTRAN_PATHS = ("/opt/homebrew/bin/jpegtran",
                  "/opt/homebrew/opt/jpeg-turbo/bin/jpegtran",
                  "/usr/local/bin/jpegtran",
                  "/usr/local/opt/jpeg-turbo/bin/jpegtran",
                  "jpegtran")


def find_jpegtran() -> str | None:
    for p in JPEGTRAN_PATHS:
        if Path(p).is_file() or shutil.which(p):
            return p
    return None


def bake_lossless(jpegtran: str, path: Path, crop_norm: tuple, orient: int) -> tuple[bool, str]:
    """In-place lossless rotate (per EXIF) + crop. Strips orientation tag.
    crop_norm = (left, top, right, bottom) 0-1 in upright coords.
    Returns (success, error_msg)."""
    rotate_map = {1: 0, 3: 180, 6: 90, 8: 270}
    rot = rotate_map.get(orient, 0)

    src = path
    tmp1 = path.with_suffix(path.suffix + ".rot.tmp")
    tmp2 = path.with_suffix(path.suffix + ".crop.tmp")

    def run(*args):
        r = subprocess.run([jpegtran, *args], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", errors="replace").strip())

    try:
        if rot:
            run("-copy", "all", "-trim", "-rotate", str(rot), "-outfile", str(tmp1), str(src))
            src = tmp1

        with Image.open(src) as im:
            w, h = im.size

        l, t, r, b = crop_norm
        px_l = max(0, int(l * w))
        px_t = max(0, int(t * h))
        px_r = min(w, int(r * w))
        px_b = min(h, int(b * h))
        cw, ch = px_r - px_l, px_b - px_t
        if cw < 32 or ch < 32:
            return False, f"crop too small: {cw}x{ch}"

        run("-copy", "all", "-trim", "-crop",
            f"{cw}x{ch}+{px_l}+{px_t}", "-outfile", str(tmp2), str(src))

        os.replace(tmp2, path)

        if rot:
            try:
                d = piexif.load(str(path))
                if "0th" in d and piexif.ImageIFD.Orientation in d["0th"]:
                    d["0th"][piexif.ImageIFD.Orientation] = 1
                    piexif.insert(piexif.dump(d), str(path))
            except Exception:
                pass

        return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        for t in (tmp1, tmp2):
            if t.exists():
                try:
                    t.unlink()
                except Exception:
                    pass


class Scorer:
    def __init__(self):
        self.device, self.dtype = pick_device()
        print(f"device: {self.device} ({self.dtype})", file=sys.stderr)
        t0 = time.time()
        self.model, self.preprocessor = convert_v2_5_from_siglip(
            low_cpu_mem_usage=True, trust_remote_code=True,
        )
        self.model = self.model.to(self.dtype).to(self.device).eval()
        print(f"siglip loaded in {time.time() - t0:.1f}s", file=sys.stderr)

    @torch.inference_mode()
    def score(self, images: list[Image.Image]) -> np.ndarray:
        px = self.preprocessor(images=images, return_tensors="pt").pixel_values
        px = px.to(self.dtype).to(self.device)
        out = self.model(px).logits.squeeze(-1).float().cpu().numpy()
        return np.atleast_1d(out)


def img_to_jpeg_b64(img: Image.Image, max_side: int = 1568, quality: int = 88) -> str:
    w, h = img.size
    if max(w, h) > max_side:
        if w >= h:
            img = img.resize((max_side, int(h * max_side / w)), Image.LANCZOS)
        else:
            img = img.resize((int(w * max_side / h), max_side), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def parse_claude_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def crop_sane(crop) -> bool:
    if crop is None:
        return True
    try:
        l, t, r, b = float(crop["left"]), float(crop["top"]), float(crop["right"]), float(crop["bottom"])
    except (KeyError, TypeError, ValueError):
        return False
    if not (0 <= l < r <= 1 and 0 <= t < b <= 1):
        return False
    if (r - l) * (b - t) < 0.45:
        return False
    return True


def claude_review(client, model_id: str, img: Image.Image) -> dict:
    """Returns parsed JSON from Claude, or {"error": str} on failure."""
    b64 = img_to_jpeg_b64(img)
    try:
        msg = client.messages.create(
            model=model_id,
            max_tokens=600,
            system=[{"type": "text", "text": CLAUDE_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": "Review this photo."},
                ],
            }],
        )
        text = "".join(b.text for b in msg.content if hasattr(b, "text"))
        parsed = parse_claude_json(text)
        if parsed is None:
            return {"error": f"unparseable: {text[:200]}"}
        return parsed
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="folder of photos (recursive)")
    ap.add_argument("-o", "--output", default=None, help="output dir (default: <folder>/keep)")
    ap.add_argument("--copy", action="store_true", help="copy instead of symlink")
    ap.add_argument("--bake", action="store_true",
                    help="lossless jpegtran rotate+crop the kept JPGs (implies --copy; deletes .xmp). "
                         "Cropped photos become viewable in any image app.")
    ap.add_argument("--sharpness-min", type=float, default=80.0,
                    help="min Laplacian variance (default 80)")
    ap.add_argument("--local-min", type=float, default=4.0,
                    help="min SigLIP score to send to Claude (default 4.0)")
    ap.add_argument("--dedup-window", type=float, default=3.0,
                    help="seconds; photos within window grouped as burst (default 3.0; 0 disables)")
    ap.add_argument("--no-claude", action="store_true",
                    help="skip Claude review; keep top by SigLIP score")
    ap.add_argument("--threshold", type=float, default=5.5,
                    help="(--no-claude only) min SigLIP score to keep (default 5.5)")
    ap.add_argument("--model", default="haiku",
                    help="Claude model: alias 'haiku'/'sonnet'/'opus' or full model id "
                         "(e.g. claude-sonnet-4-6, claude-haiku-4-5-20251001). Default: haiku")
    ap.add_argument("--workers", type=int, default=5, help="concurrent Claude calls (default 5)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"not a directory: {folder}")

    jpegtran = None
    if args.bake:
        args.copy = True  # bake requires real files
        jpegtran = find_jpegtran()
        if not jpegtran:
            sys.exit("--bake needs jpegtran; install with `brew install jpeg-turbo`")

    out_dir = Path(args.output).expanduser().resolve() if args.output else folder / "keep"
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    files = [p for p in sorted(folder.rglob("*"))
             if p.is_file()
             and p.suffix.lower() in SUPPORTED_EXTS
             and out_dir not in p.parents]
    print(f"scanning {len(files)} files in {folder}", file=sys.stderr)
    if not files:
        return

    use_claude = not args.no_claude
    if use_claude:
        if not load_anthropic_key():
            sys.exit("ANTHROPIC_API_KEY not found. Set env var, or put it in "
                     "./.env, ~/.anthropic/.env, ~/.config/anthropic/.env, "
                     "or a path in $PHOTO_CULL_ENV_FILE.")
        import anthropic
        client = anthropic.Anthropic()
        model_id = CLAUDE_MODEL_ALIASES.get(args.model, args.model)
        label = args.model if args.model in CLAUDE_MODEL_ALIASES else "custom"
        print(f"claude: {label} ({model_id}), {args.workers} workers", file=sys.stderr)

    scorer = Scorer()

    # ----- Phase 1: local screening -----
    print(f"\n[phase 1] local screening …", file=sys.stderr)
    records = []
    t1 = time.time()
    for i, p in enumerate(files):
        rel = p.relative_to(folder)
        rec = {"name": str(rel), "path": p, "status": "error",
               "score": None, "sharp": None, "dt": None, "orient": 1,
               "claude": None, "crop": None, "issues": [], "baked": False}
        img = load_image(p)
        if img is None:
            records.append(rec); continue
        sharp = laplacian_var(img)
        rec["sharp"] = sharp
        rec["dt"] = get_exif_datetime(p)
        rec["orient"] = get_exif_orientation(p)
        if sharp < args.sharpness_min:
            rec["status"] = "blur"
            print(f"[{i+1}/{len(files)}] {rel}  sharp={sharp:5.0f}  BLUR")
            records.append(rec); continue
        score = float(scorer.score([img])[0])
        rec["score"] = score
        rec["_img"] = img  # keep PIL for Claude phase (closed at phase end)
        rec["status"] = "scored"
        print(f"[{i+1}/{len(files)}] {rel}  score={score:.2f}  sharp={sharp:5.0f}")
        records.append(rec)
    print(f"phase 1 done in {time.time() - t1:.1f}s", file=sys.stderr)

    # ----- Phase 2: burst dedup -----
    if args.dedup_window > 0:
        print(f"\n[phase 2] burst dedup (window {args.dedup_window}s) …", file=sys.stderr)
        scored = [r for r in records if r["status"] == "scored"]
        scored.sort(key=lambda r: (r["dt"] or datetime.min, r["name"]))
        groups = []
        cur = []
        for r in scored:
            if not cur:
                cur = [r]; continue
            prev_dt = cur[-1]["dt"]
            if r["dt"] and prev_dt and (r["dt"] - prev_dt).total_seconds() <= args.dedup_window:
                cur.append(r)
            else:
                groups.append(cur); cur = [r]
        if cur:
            groups.append(cur)
        n_groups = sum(1 for g in groups if len(g) > 1)
        n_dropped = 0
        for g in groups:
            if len(g) <= 1:
                continue
            best = max(g, key=lambda r: r["score"])
            best["burst_size"] = len(g)
            best["burst_peers"] = [r["name"] for r in g if r is not best]
            for r in g:
                if r is best:
                    continue
                r["status"] = "duplicate"
                r["burst_winner"] = best["name"]
                n_dropped += 1
        print(f"phase 2: {n_groups} burst groups, dropped {n_dropped} duplicates", file=sys.stderr)

    # ----- Phase 3: Claude review -----
    if use_claude:
        candidates = [r for r in records if r["status"] == "scored" and r["score"] >= args.local_min]
        too_low = [r for r in records if r["status"] == "scored" and r["score"] < args.local_min]
        for r in too_low:
            r["status"] = "low"
        print(f"\n[phase 3] claude reviewing {len(candidates)} candidates "
              f"(skipping {len(too_low)} below local-min {args.local_min}) …", file=sys.stderr)

        def review_one(rec):
            res = claude_review(client, model_id, rec["_img"])
            return rec, res

        t3 = time.time()
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(review_one, r): r for r in candidates}
            for j, fut in enumerate(cf.as_completed(futures), 1):
                rec, res = fut.result()
                rec["claude"] = res
                if "error" in res:
                    rec["status"] = "claude_err"
                    print(f"  ({j}/{len(candidates)}) {rec['name']}  ERROR  {res['error'][:100]}")
                    continue
                keep = bool(res.get("keep", False))
                rec["claude_score"] = res.get("score")
                rec["issues"] = res.get("issues") or []
                if keep:
                    crop = res.get("crop")
                    if crop_sane(crop):
                        rec["crop"] = crop
                    rec["status"] = "keep"
                else:
                    rec["status"] = "reject"
                tag = "KEEP" if keep else "reject"
                csc = res.get("score", "?")
                issues = ",".join(rec["issues"]) if rec["issues"] else "-"
                cropmark = " (+crop)" if rec["crop"] else ""
                print(f"  ({j}/{len(candidates)}) {rec['name']}  claude={csc}  {tag}{cropmark}  issues={issues}")
        print(f"phase 3 done in {time.time() - t3:.1f}s", file=sys.stderr)
    else:
        for r in records:
            if r["status"] != "scored":
                continue
            if r["score"] >= args.threshold:
                r["status"] = "keep"
            else:
                r["status"] = "low"

    # Free image memory
    for r in records:
        r.pop("_img", None)

    # ----- Phase 4: output -----
    n_keep = 0
    n_baked = 0
    if not args.dry_run:
        for r in records:
            if r["status"] != "keep":
                continue
            src = r["path"]
            dst = out_dir / Path(r["name"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            if args.copy:
                shutil.copy2(src, dst)
            else:
                os.symlink(src, dst)
            crop = r.get("crop")
            if crop:
                box = (crop["left"], crop["top"], crop["right"], crop["bottom"])
                if args.bake and dst.suffix.lower() in (".jpg", ".jpeg"):
                    ok, err = bake_lossless(jpegtran, dst, box, r["orient"])
                    if ok:
                        r["baked"] = True
                        n_baked += 1
                    else:
                        print(f"  ! bake failed for {r['name']}: {err}; falling back to xmp sidecar",
                              file=sys.stderr)
                        note = (crop.get("why") or "") + (f" [{crop.get('ratio')}]" if crop.get("ratio") else "")
                        write_xmp_sidecar(dst, box, r.get("claude_score") or r["score"] or 5, note.strip())
                else:
                    note = (crop.get("why") or "") + (f" [{crop.get('ratio')}]" if crop.get("ratio") else "")
                    write_xmp_sidecar(dst, box, r.get("claude_score") or r["score"] or 5, note.strip())
            n_keep += 1
        if args.bake:
            print(f"baked {n_baked} crops losslessly", file=sys.stderr)

    n_total = len(records)
    print(f"\nkept {n_keep}/{n_total}", file=sys.stderr)

    if not args.dry_run:
        write_report(out_dir, folder, records, args)
        print(f"report: {out_dir / 'report.md'}", file=sys.stderr)


def write_report(out_dir: Path, src_folder: Path, records: list, args):
    def cnt(s): return sum(1 for r in records if r["status"] == s)
    n_total = len(records)
    n_keep = cnt("keep")
    n_low = cnt("low")
    n_blur = cnt("blur")
    n_dup = cnt("duplicate")
    n_reject = cnt("reject")
    n_err = cnt("error") + cnt("claude_err")
    n_cropped = sum(1 for r in records if r["status"] == "keep" and r.get("crop"))

    L = []
    L.append("# 选片报告\n")
    L.append(f"- **源**：`{src_folder}`")
    L.append(f"- **目标**：`{out_dir}`")
    L.append(f"- **模式**：{'复制' if args.copy else '软链接'}{'  + jpegtran 无损烘焙裁剪' if args.bake else ''}")
    L.append(f"- **本地阈值**：sharpness ≥ {args.sharpness_min} · SigLIP ≥ {args.local_min}（送 Claude 复审）")
    if not args.no_claude:
        L.append(f"- **Claude 模型**：{args.model}")
    L.append(f"- **连拍去重窗口**：{args.dedup_window} 秒\n")

    L.append("## 总览\n")
    L.append("| 类别 | 张数 |\n|---|---|")
    L.append(f"| 总计 | {n_total} |")
    L.append(f"| **保留** | **{n_keep}**（其中 {n_cropped} 张建议裁剪）|")
    if n_dup:    L.append(f"| 连拍去重剔除 | {n_dup} |")
    if n_blur:   L.append(f"| 模糊剔除 | {n_blur} |")
    if n_low:    L.append(f"| 本地分太低（未送 Claude）| {n_low} |")
    if n_reject: L.append(f"| Claude 否决 | {n_reject} |")
    if n_err:    L.append(f"| 出错 | {n_err} |")
    L.append("")

    keep_recs = sorted([r for r in records if r["status"] == "keep"],
                       key=lambda r: -(r.get("claude_score") or r["score"] or 0))
    L.append(f"## 保留 · {n_keep} 张（分数从高到低）\n")
    if keep_recs:
        L.append("| # | 文件 | Claude分 | 这张照片说了什么 | 是否裁剪 | 裁剪理由 |")
        L.append("|---|---|---|---|---|---|")
        for i, r in enumerate(keep_recs, 1):
            crop = r.get("crop")
            if crop:
                cb = f"是 · {crop.get('ratio','?')}"
                why = crop.get("why", "")
            else:
                cb = "否"; why = ""
            says = (r.get("claude") or {}).get("what_it_says", "") or (r.get("claude") or {}).get("note", "")
            L.append(f"| {i} | `{r['name']}` | {r.get('claude_score','?')} | {says} | {cb} | {why} |")
    else:
        L.append("_无_\n")

    rej_recs = [r for r in records if r["status"] == "reject"]
    L.append(f"\n## Claude 否决 · {n_reject} 张\n")
    L.append("_严苛策展人模式默认 reject，理由列里说为啥不行。_\n")
    if rej_recs:
        L.append("| 文件 | Claude分 | 否决理由 | 这张照片说了什么 | 标签 |\n|---|---|---|---|---|")
        for r in sorted(rej_recs, key=lambda r: -(r.get("claude_score") or 0)):
            cl = r.get("claude") or {}
            reason = cl.get("reject_reason") or cl.get("note", "")
            says = cl.get("what_it_says", "")
            issues = ",".join(r.get("issues") or [])
            L.append(f"| `{r['name']}` | {r.get('claude_score','?')} | {reason} | {says} | {issues} |")

    dup_recs = [r for r in records if r["status"] == "duplicate"]
    L.append(f"\n## 连拍去重剔除 · {n_dup} 张\n")
    L.append("_同一秒/几秒内连续多张，已留分数最高那张。如果你想看到所有版本，加 `--dedup-window 0`。_\n")
    if dup_recs:
        L.append("| 文件 | 本地分 | 让位给 |\n|---|---|---|")
        for r in sorted(dup_recs, key=lambda r: r["name"]):
            L.append(f"| `{r['name']}` | {r['score']:.2f} | `{r.get('burst_winner','?')}` |")

    if n_blur:
        blur_recs = [r for r in records if r["status"] == "blur"]
        L.append(f"\n## 模糊剔除 · {n_blur} 张\n")
        L.append(f"_Laplacian 方差 < {args.sharpness_min}。有意的虚化/动感请加 `--sharpness-min` 调低。_\n")
        L.append("| 文件 | 清晰度 |\n|---|---|")
        for r in sorted(blur_recs, key=lambda r: -(r["sharp"] or 0)):
            L.append(f"| `{r['name']}` | {r['sharp']:.0f} |")

    if n_low:
        low_recs = [r for r in records if r["status"] == "low"]
        L.append(f"\n## 本地分太低（未送 Claude）· {n_low} 张\n")
        L.append(f"_SigLIP < {args.local_min}，节省 token 直接劝退。觉得有可惜的，把 `--local-min` 调低。_\n")
        L.append("| 文件 | SigLIP分 |\n|---|---|")
        for r in sorted(low_recs, key=lambda r: -(r["score"] or 0)):
            L.append(f"| `{r['name']}` | {r['score']:.2f} |")

    if n_err:
        err_recs = [r for r in records if r["status"] in ("error", "claude_err")]
        L.append(f"\n## 出错 · {n_err} 张\n")
        for r in err_recs:
            err = (r.get("claude") or {}).get("error", "load failed")
            L.append(f"- `{r['name']}` — {err}")

    L.append("\n---\n## 怎么读这份报告\n")
    L.append("- **本地分 = SigLIP-v2.5 美学分** 1-10，本地秒级跑出来，是粗筛。")
    L.append("- **Claude 分** 1-10，由 Claude vision 给出，更看重摄影常识（闭眼/失焦/穿帮/构图）。")
    if args.bake:
        L.append("- **裁剪生效**：被裁的图已经用 `jpegtran` **无损切好**——任何看图工具（Preview、Photos.app、微信）都直接显示裁剪后的画面。原始 JPEG 块零重编码，画质不损失。源文件夹的原片完全没动过。")
    else:
        L.append("- **裁剪生效**：每张被裁的图旁边有 `.xmp` sidecar。Lightroom / Bridge / Capture One 打开会自动套上裁剪框，**原片像素零修改**，不满意一键还原全帧。Preview / Photos.app 不读 sidecar，会显示全帧。要直接看裁剪结果，加 `--bake` 重跑。")
    L.append("- **想找回某张被剔除的**：在对应分组里看到文件名，去原文件夹手动捞就好。")

    (out_dir / "report.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
