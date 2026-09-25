# Figures

Publication-quality figures for *Verification-first retrieval-augmented generation
for trustworthy decision support in MPCVD diamond growth* (Digital Discovery, RSC).

All figures are 600 DPI TIFF, B&W/greyscale.

## Figure Inventory

| File | Description | Section |
|:-----|:------------|:--------|
| `figure_1.tiff` | Six-stage verification-first pipeline architecture | §2.1 |
| `figure_2.tiff` | Verification and retry loop flowchart | §2.3 |
| `figure_3.tiff` | Stage-decomposed evaluation (primary metric per stage) | §4.1 |
| `figure_4.tiff` | System ablation: verifier + retry contributions | §4.2 |
| `figure_5.tiff` | Atomic-claim action accuracy by gold-label status | §4.2 |
| `figure_6.tiff` | Score distribution across three evaluation dimensions | §4.4 |

## SHA-256 Integrity Hashes

Generated: 2026-09-25 (TIFF 600 DPI versions)

| File | SHA-256 |
|:-----|:--------|
| `figure_1.tiff` | `9388190977f50c8757f139198afb8494d7ef3b0f385f2133ae4f05abe887999a` |
| `figure_2.tiff` | `b0b03d4639844009a2f43d06eaeb55d9c2df0e2f78666921d167adcf1bf12910` |
| `figure_3.tiff` | `bfec98d80331e5aff0a95ce47cc613d8fa81462150e477f2606b3272dbde1cdc` |
| `figure_4.tiff` | `21a314fb0569a395f57e40ceb02e1ba037f760791b54a3fd25c1bb7ee2ec996f` |
| `figure_5.tiff` | `ccbe50c4bfcaf63c24c831f8e52daf1ceb5ce81fe149c374b53c4473494a471e` |
| `figure_6.tiff` | `a0b45ef8655520c344b96a7f23a4fb2421ed0dc647035bc2c8594f52542e468d` |

Verify integrity with:

```bash
python -c "
import hashlib, pathlib
for f in sorted(pathlib.Path('figures').glob('*.tiff')):
    print(f.name, hashlib.sha256(f.read_bytes()).hexdigest())
"
```
