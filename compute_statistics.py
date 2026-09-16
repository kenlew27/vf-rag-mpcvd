"""
Compute all reported statistics from the anonymised scientist evaluation data.

Usage:
    python compute_statistics.py

Reads scientist_scores_grader_A.csv and scientist_scores_grader_B.csv
from the evaluation/ directory and reproduces all numbers reported in the paper.
"""

import csv, math
from pathlib import Path
from collections import Counter

EVAL_DIR = Path(__file__).parent / "evaluation"


def wilson_ci(k, n, z=1.96):
    """Wilson score 95% confidence interval for a proportion."""
    if n == 0:
        return 0, 0, 0
    p = k / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    spread = z * math.sqrt((p * (1 - p) / n + z**2 / (4 * n**2))) / denom
    return round(p * 100, 1), round((centre - spread) * 100, 1), round((centre + spread) * 100, 1)


def main():
    # Load scores
    all_rows = []
    for csv_file in sorted(EVAL_DIR.glob("scientist_scores_grader_*.csv")):
        with open(csv_file, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        print(f"Loaded {csv_file.name}: {len(rows)} ratings")
        all_rows.extend(rows)

    n = len(all_rows)
    print(f"\nTotal ratings: {n}")

    # Parse scores
    grounded = [int(r["Grounded"]) for r in all_rows]
    correct = [int(r["Correct"]) for r in all_rows]
    rigor = [int(r["Scientific Rigor"]) for r in all_rows]

    # Means and SDs
    def mean_sd(vals):
        m = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
        return round(m, 2), round(sd, 2)

    g_mean, g_sd = mean_sd(grounded)
    c_mean, c_sd = mean_sd(correct)
    r_mean, r_sd = mean_sd(rigor)

    print(f"\n=== Dimension Means ===")
    print(f"  Groundedness: {g_mean} (SD {g_sd})")
    print(f"  Correctness:  {c_mean} (SD {c_sd})")
    print(f"  Rigor:        {r_mean} (SD {r_sd})")

    # Proportion >= 4
    g_ge4 = sum(1 for v in grounded if v >= 4)
    c_ge4 = sum(1 for v in correct if v >= 4)
    r_ge4 = sum(1 for v in rigor if v >= 4)

    print(f"\n=== Proportion >= 4 (Wilson 95% CI) ===")
    for label, k_val in [("Grounded", g_ge4), ("Correct", c_ge4), ("Rigor", r_ge4)]:
        pct, lo, hi = wilson_ci(k_val, n)
        print(f"  {label}: {pct}% [{lo}%, {hi}%]  ({k_val}/{n})")

    # Perfect 5s for groundedness
    g_5 = sum(1 for v in grounded if v == 5)
    print(f"\n  Groundedness perfect 5: {g_5}/{n}")

    # Below-3 exceptions
    below3 = sum(1 for i in range(n) if grounded[i] < 3 or correct[i] < 3 or rigor[i] < 3)
    print(f"  Ratings with any dimension < 3: {below3}/{n}")

    # By bucket
    print(f"\n=== By Complexity Bucket ===")
    for bucket in ["B1", "B2", "R"]:
        bucket_rows = [r for r in all_rows if r["row_id"].startswith(bucket)]
        if not bucket_rows:
            continue
        bn = len(bucket_rows)
        bg = [int(r["Grounded"]) for r in bucket_rows]
        bc = [int(r["Correct"]) for r in bucket_rows]
        br = [int(r["Scientific Rigor"]) for r in bucket_rows]

        bg4 = sum(1 for v in bg if v >= 4)
        bc4 = sum(1 for v in bc if v >= 4)
        br4 = sum(1 for v in br if v >= 4)

        bm_c, _ = mean_sd(bc) if len(bc) > 1 else (sum(bc)/len(bc), 0)
        bm_r, _ = mean_sd(br) if len(br) > 1 else (sum(br)/len(br), 0)

        print(f"  {bucket} (n={bn}): grounded>={4}: {bg4/bn*100:.1f}%, "
              f"correct>={4}: {bc4/bn*100:.1f}% (mean {bm_c}), "
              f"rigor>={4}: {br4/bn*100:.1f}% (mean {bm_r})")

    # Inter-rater agreement
    print(f"\n=== Inter-Rater Agreement ===")
    grader_a = [r for r in all_rows if "A" in r.get("Grader", "")]
    grader_b = [r for r in all_rows if "B" in r.get("Grader", "")]

    if grader_a and grader_b:
        # Match by row_id
        a_by_id = {r["row_id"]: r for r in grader_a}
        b_by_id = {r["row_id"]: r for r in grader_b}
        shared = set(a_by_id.keys()) & set(b_by_id.keys())

        diffs = [abs(int(a_by_id[q]["Correct"]) - int(b_by_id[q]["Correct"])) for q in shared]
        mad = sum(diffs) / len(diffs)
        exact = sum(1 for d in diffs if d == 0)
        max_diff = max(diffs)

        print(f"  Shared questions: {len(shared)}")
        print(f"  Correctness MAD: {mad:.2f}")
        print(f"  Exact agreement: {exact}/{len(shared)} ({exact/len(shared)*100:.0f}%)")
        print(f"  Max difference: {max_diff}")


if __name__ == "__main__":
    main()
