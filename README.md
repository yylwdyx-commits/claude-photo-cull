# claude-photo-cull

A harsh photo curator. Two-stage pipeline: free local pre-filter (SigLIP-v2.5 aesthetic + Laplacian sharpness + EXIF-burst dedup) feeds a strict Claude vision pass that defaults to **reject**. Survivors land in `keep/` as symlinks (or copies, or losslessly baked crops). **Originals are never modified.**

Ships as both a standalone CLI and a [Claude Code](https://claude.com/claude-code) skill.

## Why

Most "AI photo selection" tools optimize for "looks technically OK". This one runs an explicit curator persona that asks "what does this photo SAY?" and rejects pleasant-but-empty travel records, posed cuteness, document shots, and tourist angles. Expected reject rate: 85–95%. If you want a tool that validates your shooting, look elsewhere.

## Quickstart

```bash
git clone https://github.com/<your-user>/claude-photo-cull
cd claude-photo-cull
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python cull.py /path/to/photos
open /path/to/photos/keep/report.md
```

For shareable baked JPG crops (works in Preview, Photos.app, WeChat):

```bash
brew install jpeg-turbo
python cull.py /path/to/photos --bake
```

For free local-only (no Claude calls):

```bash
python cull.py /path/to/photos --no-claude --threshold 5.5
```

## Use as a Claude Code skill

```bash
git clone https://github.com/<your-user>/claude-photo-cull ~/.claude/skills/photo-cull
pip install -r ~/.claude/skills/photo-cull/requirements.txt
```

Then in any Claude Code session: "cull `~/Pictures/Trip-2026/`" — the skill picks flags based on what you ask for (strict / loose / share-ready / local-only / which model).

## Replaceable model

`--model` accepts either an alias or a full model id:

```bash
python cull.py photos/ --model haiku                              # alias
python cull.py photos/ --model sonnet
python cull.py photos/ --model opus
python cull.py photos/ --model claude-sonnet-4-6                  # raw id
python cull.py photos/ --model claude-haiku-4-5-20251001
```

Aliases are tracked in `CLAUDE_MODEL_ALIASES` at the top of `cull.py` — bump the dated id when a new snapshot ships.

## Pipeline

| Phase | What | Cost |
|---|---|---|
| 1. Local screen | SigLIP-v2.5 aesthetic + Laplacian sharpness | free, GPU/MPS/CPU |
| 2. Burst dedup | Group by EXIF datetime within `--dedup-window`, keep highest score per burst | free |
| 3. Claude review | Vision pass on survivors above `--local-min`, returns `{keep, score, reject_reason, crop, …}` | API tokens |
| 4. Output | Symlink / copy keepers, write `.xmp` sidecar with crop, optional `jpegtran` lossless bake | free |

## Inputs

JPG / PNG / TIFF / WebP / HEIC / HEIF, plus RAW (`.dng .raf .cr3 .cr2 .arw .nef .orf .rw2 .pef`). RAW is decoded via embedded JPEG thumbnail when present, otherwise full demosaic — first run on a RAW-heavy folder is slow.

## Output

```
photos/
├── IMG_1234.jpg
├── IMG_1234.dng
└── keep/
    ├── IMG_1234.jpg          # symlink (or copy with --copy, baked with --bake)
    ├── IMG_1234.jpg.xmp      # crop sidecar (Lightroom/Bridge/Capture One)
    └── report.md             # Chinese-language summary
```

`.xmp` sidecars don't render in Preview or Photos.app — use `--bake` for crops that show everywhere.

## All flags

```
folder                   folder of photos (recursive)
-o, --output DIR         output dir (default: <folder>/keep)
--copy                   copy instead of symlink
--bake                   lossless jpegtran rotate+crop the kept JPGs (implies --copy)
--sharpness-min N        min Laplacian variance (default 80)
--local-min N            min SigLIP score to send to Claude (default 4.0)
--dedup-window S         seconds within which photos group as burst (default 3.0; 0 disables)
--no-claude              skip Claude review; keep top by SigLIP score
--threshold N            (--no-claude only) min SigLIP score to keep (default 5.5)
--model X                Claude model alias or full id (default: haiku)
--workers N              concurrent Claude calls (default 5)
--dry-run                report only, write nothing
```

## API key locations searched

In order:

1. `ANTHROPIC_API_KEY` env var
2. Path in `$PHOTO_CULL_ENV_FILE` (if set)
3. `./.env`, `./.env.local`
4. `~/.anthropic/.env`
5. `~/.config/anthropic/.env`

## Curator prompt

The full system prompt lives in `cull.py` as `CLAUDE_SYSTEM`. Read it before complaining about strictness — it is deliberately harsh and explicitly tuned for "宁可错杀十张也不放进一张废片". Edit in place if you want a softer curator.

## License

MIT — see `LICENSE`.
