---
name: photo-cull
description: Cull a folder of photos with a hybrid local + Claude vision pipeline. Use when the user asks to "select photos", "pick keepers", "cull", "filter out bad shots", "rate this folder", or otherwise filter/curate a directory of images. Local SigLIP aesthetic + sharpness pre-filters; Claude vision applies a strict "what does this photo SAY" curator prompt and returns keep/reject + crop suggestions. Originals are never modified.
---

# photo-cull

A photo-curation pipeline. Phase 1 SigLIP aesthetic + Laplacian sharpness, Phase 2 burst dedup by EXIF datetime, Phase 3 Claude vision review with a strict "reject by default" prompt, Phase 4 copy/symlink + `.xmp` sidecars (or lossless `jpegtran` baked crops).

The script lives next to this file as `cull.py`. **Originals are never modified.** Output goes to `<folder>/keep` by default.

## When to invoke

- User points at a folder and asks to cull / select / pick / filter / curate / rate / 选片 / 严选 / 挑片.
- User asks about photo quality across many shots ("which of these are worth keeping").
- User wants Claude to recommend crops on photos.

Do **not** invoke for editing a single image, generating images, or any task that isn't "filter a folder".

## Required environment

- Python 3.10+ with `pip install -r {SKILL_DIR}/requirements.txt` (one-time).
- `ANTHROPIC_API_KEY` set in env, or in `./.env` / `~/.anthropic/.env` / `~/.config/anthropic/.env`.
- For `--bake`: `brew install jpeg-turbo` (macOS) or distro equivalent.

If deps look missing, run `python -c "import torch, anthropic, rawpy, cv2"` to check before starting a long run.

## How to invoke

Resolve `{SKILL_DIR}` to the directory this `SKILL.md` lives in (e.g. `~/.claude/skills/photo-cull`), then run:

```bash
python {SKILL_DIR}/cull.py <folder> [options]
```

Pass arguments based on what the user asked for. Defaults already encode the "strict curator" intent — only deviate when the user signals otherwise.

### Key flags

| Flag | Default | When to use |
|---|---|---|
| `<folder>` | required | The folder to cull (recursive). |
| `-o, --output DIR` | `<folder>/keep` | Override output location. |
| `--copy` | symlink | Use when results will leave the source machine, or when `--bake`. |
| `--bake` | off | Lossless `jpegtran` rotate+crop baked into JPGs in the output folder. Implies `--copy`. Use when the user wants ready-to-share JPGs (Preview / WeChat / Photos.app show the crop directly). |
| `--sharpness-min N` | 80 | Lower if the user shoots intentional motion blur / soft focus. |
| `--local-min N` | 4.0 | Lower to send more candidates to Claude (more $$ but fewer false rejects). |
| `--dedup-window S` | 3.0 | Set `0` if user wants every burst frame considered. |
| `--no-claude` | off | Local-only run (free). Combine with `--threshold` (default 5.5). |
| `--threshold N` | 5.5 | `--no-claude` only: SigLIP keep threshold. |
| `--model X` | `haiku` | Alias `haiku`/`sonnet`/`opus` OR a full model id like `claude-sonnet-4-6`. Use sonnet/opus when user asks for higher quality judgment. |
| `--workers N` | 5 | Concurrent Claude calls. Bump to 10–20 for >500 photos. |
| `--dry-run` | off | Preview only. Use first when the folder is huge or the user is unsure. |

### Common modes

- **First-time / unsure folder**: `--dry-run` first to see counts, then real run.
- **"严格选片" / harsh curation** (default): `python … <folder>` — Haiku, dedup on, output as symlinks.
- **"挑出来直接发朋友圈"**: add `--copy --bake` so the crops are baked into shareable JPGs.
- **"先免费过一遍"**: `--no-claude --threshold 6.0` for a SigLIP-only pass.
- **Higher-stakes selection** (wedding, portfolio): `--model sonnet --workers 10`.
- **Burst-heavy event** (sports, kids): keep dedup window default; bump `--workers`.
- **Pixel-peeping** (the user wants every survivor reviewed): `--local-min 3.0`.

## After the run

The script writes `<output>/report.md` with a Chinese-language Markdown report (keep / reject / dedup / blur / low-score sections). Surface the headline numbers to the user and offer to open the report or the keep folder.

## Things to watch

- The Claude prompt is intentionally harsh — expect 85–95% reject. If the user complains "too strict", suggest `--local-min 5.0` (skip more low-score candidates so the kept set looks higher quality) or `--model sonnet` (Sonnet is less trigger-happy than Haiku).
- RAW formats are decoded via `rawpy`; first run on a RAW-heavy folder is slow.
- On Apple Silicon, SigLIP runs on MPS; first-load downloads ~1GB of weights via HuggingFace.
- The `.xmp` sidecars only render in Lightroom / Bridge / Capture One. Plain Preview / Photos.app users want `--bake`.
