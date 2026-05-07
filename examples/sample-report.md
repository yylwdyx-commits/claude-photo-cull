# Photo cull report

- **Source**: `/Users/jane/Pictures/Tokyo-2026`
- **Output**: `/Users/jane/Pictures/Tokyo-2026/keep`
- **Mode**: copy  + jpegtran lossless baked crops
- **Local thresholds**: sharpness ≥ 80.0 · SigLIP ≥ 4.0 (sent to Claude)
- **Claude model**: sonnet
- **Burst-dedup window**: 3.0s

## Summary

| Category | Count |
|---|---|
| Total | 247 |
| **Kept** | **18** (11 suggest crop) |
| Burst duplicates dropped | 34 |
| Blurry rejected | 9 |
| Below local-min (skipped Claude) | 41 |
| Rejected by Claude | 145 |

## Kept · 18 (sorted by score desc)

| # | File | Claude | What it says | Crop? | Crop reason |
|---|---|---|---|---|---|
| 1 | `DSCF1284.RAF` | 9 | An old man feeding pigeons, mid-throw, the birds caught in a frozen burst around his outstretched arm | yes · 4:5 | Tighter crop on the man and the densest pigeon cluster removes the empty street on the right that doesn't add anything |
| 2 | `DSCF1342.RAF` | 8 | Two schoolgirls laughing at a phone screen while a third leans in surprised | no |  |
| 3 | `IMG_4471.heic` | 8 | Wet umbrella in foreground frames a salaryman hailing a cab in the rain | yes · 3:2 | Crop bottom 15% — the puddle is too prominent and pulls the eye away from the figure |
| 4 | `DSCF1399.RAF` | 8 | A monk's hand visible only by its shadow, gesturing across an offering bowl | no |  |
| 5 | `IMG_4502.heic` | 7 | Tired-looking chef closing his stall, the half-down shutter cropping his torso | yes · 1:1 | Square crop centers the diagonal of the shutter |
| 6 | `DSCF1421.RAF` | 7 | Woman alone on a moving Yamanote car at night, reflection doubling her | no |  |
| 7 | `DSCF1455.RAF` | 7 | Two old friends bowing simultaneously at exactly the same depth, mirror gestures | yes · 16:9 | Wide crop emphasizes the symmetry — the original 3:2 has too much wall |
| 8 | `IMG_4533.heic` | 7 | A pigeon walking deliberately across the chalk outline of a child's drawing | no |  |

(remaining 10 omitted from this preview)

## Rejected by Claude · 145

_Strict-curator mode rejects by default; the reason column says why._

| File | Claude | Reason | What it says | Tags |
|---|---|---|---|---|
| `DSCF1281.RAF` | 5 | Pleasant Shibuya scramble shot but says nothing a thousand others don't say better | Crowd crossing Shibuya intersection at dusk | generic,cliche |
| `IMG_4488.heic` | 5 | Hotel breakfast — document shot, no idea here | A bowl of miso and grilled fish on a tray | empty,no_moment |
| `DSCF1300.RAF` | 4 | "Look at me" portrait, the smile is performed not felt | A friend posing in front of the Tokyo Tower | posed,no_moment |
| `DSCF1370.RAF` | 6 | Genuinely pretty light but the subject (an empty bench) doesn't earn it | Late afternoon sun across an empty park bench | empty |
| `IMG_4510.heic` | 4 | Tilted skyline, no recovery | Skyline from a bar at night | tilted,generic |

(remaining 140 omitted)

## Burst duplicates · 34

_Same-second bursts; highest-score frame kept. Pass `--dedup-window 0` to see every frame._

| File | Local score | Lost to |
|---|---|---|
| `DSCF1283.RAF` | 6.41 | `DSCF1284.RAF` |
| `DSCF1285.RAF` | 6.20 | `DSCF1284.RAF` |
| `DSCF1341.RAF` | 5.88 | `DSCF1342.RAF` |
| `DSCF1343.RAF` | 6.10 | `DSCF1342.RAF` |

(remaining 30 omitted)

## Blurry · 9

_Laplacian variance < 80.0. Lower `--sharpness-min` if you shoot intentional motion blur._

| File | Sharpness |
|---|---|
| `IMG_4475.heic` | 64 |
| `DSCF1297.RAF` | 47 |
| `IMG_4519.heic` | 38 |

(remaining 6 omitted)

## Below local-min · 41

_SigLIP < 4.0; skipped Claude to save tokens. Lower `--local-min` to surface them._

| File | SigLIP |
|---|---|
| `IMG_4480.heic` | 3.92 |
| `DSCF1305.RAF` | 3.71 |
| `IMG_4495.heic` | 3.40 |

(remaining 38 omitted)

---
## How to read this report

- **Local score = SigLIP-v2.5 aesthetic score** (1-10). Runs locally in seconds. Coarse pre-filter.
- **Claude score** (1-10). Vision-model judgment; weights moments, eye contact, focus, framing.
- **Crops are baked**: kept JPGs were losslessly cropped via `jpegtran` — every viewer (Preview, Photos.app, WhatsApp) shows the crop. Pixel-exact, zero re-encoding. Originals untouched.
- **To rescue a rejected photo**: find the filename in the relevant section above and grab it from the source folder.
