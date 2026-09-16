# Evaluation Data

This directory should contain the anonymised scientist evaluation CSVs.

## Expected Files

| File | Description |
|:-----|:------------|
| `scientist_scores_grader_A.csv` | 50 ratings from Grader A |
| `scientist_scores_grader_B.csv` | 50 ratings from Grader B |

## CSV Schema

Each CSV should have the following columns:

| Column | Type | Description |
|:-------|:-----|:------------|
| `row_id` | string | Question identifier (prefix: B1, B2, or R for bucket) |
| `Grader` | string | Anonymised grader identifier (A or B) |
| `Grounded` | int (1-5) | Groundedness rating |
| `Correct` | int (1-5) | Correctness rating |
| `Scientific Rigor` | int (1-5) | Scientific rigor rating |

## Reproducing Statistics

After placing the CSVs here, from the parent directory run:

```bash
python compute_statistics.py
```

This reproduces all scientist evaluation statistics reported in Section 4.4 of the paper.
