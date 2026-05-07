# Examples

- [`sample-report.md`](./sample-report.md) — what the auto-generated `report.md` looks like after a real run. This one is hand-edited from a Tokyo trip cull (247 photos in, 18 out, ~7% keep rate). Run shape and column structure match exactly what `cull.py` produces; filenames and "what it says" descriptions are illustrative rather than from one specific run.

To generate your own report on real photos:

```bash
python ../cull.py /path/to/photos --copy --bake --model sonnet
open /path/to/photos/keep/report.md
```
