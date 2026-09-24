# Figures

Publication-quality figures for *Verification-first retrieval-augmented generation
for trustworthy decision support in MPCVD diamond growth* (Digital Discovery, RSC).

All figures are 300 dpi, B&W/greyscale, rendered with Matplotlib and saved via
the scripts in the repository root.

## Figure Inventory

| File | Description | Section | Script |
|:-----|:------------|:--------|:-------|
| `figure_1.png` | Six-stage verification-first pipeline architecture | §2.1 | — |
| `figure_2.png` | Verification and retry loop flowchart | §2.3 | — |
| `figure_3.png` | Stage-decomposed evaluation (primary metric per stage) | §4.1 | — |
| `figure_4.png` | System ablation: verifier + retry contributions | §4.2 | `regenerate_figure_4.py` |
| `figure_5.png` | Atomic-claim action accuracy by gold-label status | §4.2 | — |
| `figure_6.jpg` | Score distribution across three evaluation dimensions | §4.4 | `regenerate_figure_6.py` |

## SHA-256 Integrity Hashes

Generated: 2026-09-24

| File | SHA-256 |
|:-----|:--------|
| `figure_1.png` | `9c48e83f5f3c8512560fdf9446bc6fb8e34b9b98c3dcba307bf0c9254e296a36` |
| `figure_2.png` | `cb429165a40db97d4aaf81fe4e019e71f3d4e1ab1586b110d5daf43cc9a833b5` |
| `figure_3.png` | `ccb5fe573ad9d7baa1d068f2244415adba7c12a519fa5c7a2bbc5734ca29c3c6` |
| `figure_4.png` | `533bfb542db2fda0b438ee1a6651d47b24447adbbdd706f12623f1040d7fca5b` |
| `figure_5.png` | `b41dd17bacfb5062aa9768b112baf78d0144afa25cb4f2b66a588a0dfca83a07` |
| `figure_6.jpg` | `5d7fc1700e9d3b05b7c4ba0d94e3601827aa1d910876bbe7930be35f7ce7e433` |

Verify integrity with:

```bash
python -c "
import hashlib, pathlib
for f in sorted(pathlib.Path('figures').iterdir()):
    print(f.name, hashlib.sha256(f.read_bytes()).hexdigest())
"
```

## Regenerating Figures

```bash
# Fig. 4 — system ablation chart (vertical 87% reduction bracket)
python regenerate_figure_4.py

# Fig. 6 — score distribution (B&W grouped bar chart, single legend)
python regenerate_figure_6.py
```

Figs. 1, 2, 3, and 5 were produced externally and are provided as-is.
