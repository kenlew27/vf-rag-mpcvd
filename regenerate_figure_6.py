"""
regenerate_figure_6.py

Regenerates Fig. 6 — scientist evaluation score distribution — with:
  - Single legend (not one per group)
  - Simpler, shorter title
  - Black, white, and grey palette (matching other figures)
  - Cleaner layout

Saves to both:
  - DD_submission/figures/figure_6.jpg  (for submission)
  - new_figures/figure_6.jpg
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from pathlib import Path

HERE = Path(__file__).parent
OUTS = [
    HERE / "figures" / "figure_6.jpg",
]

# ── Data ──────────────────────────────────────────────────────────────────────
# Score distributions for each dimension (counts out of 100 ratings)
# Scores 1–5 for: Grounded, Correct, Scientific Rigor
grounded   = [1, 1, 6,  5, 87]   # scores 1,2,3,4,5
correct    = [1, 2, 11, 56, 30]
rigor      = [1, 5, 5,  44, 45]

dimensions = ['Grounded', 'Correct', 'Scientific\nRigor']
data = [grounded, correct, rigor]
scores = [1, 2, 3, 4, 5]

# Greyscale palette: score 1 = black, 2 = dark grey, 3 = medium grey, 4 = light grey, 5 = white
# Actually more intuitive: 5 = darkest (best, most), 1 = lightest
# Use: white → light grey → medium grey → dark grey → black  for 1→5
grey_palette = {
    1: '#ffffff',   # white  — worst
    2: '#cccccc',   # light grey
    3: '#999999',   # medium grey
    4: '#555555',   # dark grey
    5: '#1a1a1a',   # near-black — best
}
edge_color = '#333333'

n_dims = len(dimensions)
n_scores = len(scores)
x = np.arange(n_dims)
total_width = 0.7
bar_width = total_width / n_scores
offsets = np.linspace(-total_width/2 + bar_width/2, total_width/2 - bar_width/2, n_scores)

fig, ax = plt.subplots(figsize=(8, 5))

bars_by_score = {}
for i, score in enumerate(scores):
    counts = [data[d][i] for d in range(n_dims)]
    bars = ax.bar(x + offsets[i], counts, bar_width * 0.92,
                  color=grey_palette[score],
                  edgecolor=edge_color,
                  linewidth=0.8,
                  label=f'Score {score}' + (' (best)' if score == 5 else ' (worst)' if score == 1 else ''))
    bars_by_score[score] = bars
    # Label bars that are non-zero
    for bar, count in zip(bars, counts):
        if count > 0:
            ax.text(bar.get_x() + bar.get_width()/2,
                    bar.get_height() + 0.8,
                    str(count),
                    ha='center', va='bottom',
                    fontsize=8, fontweight='bold', color='black')

ax.set_xticks(x)
ax.set_xticklabels(dimensions, fontsize=12)
ax.set_ylabel('Count (out of 100 ratings)', fontsize=11)
ax.set_title('Score distribution across three evaluation dimensions\n'
             '(n = 100 ratings, 5-point scale)', fontsize=12, fontweight='bold', pad=10)
ax.set_ylim(0, 100)
ax.yaxis.grid(True, linestyle='--', alpha=0.6, color='grey')
ax.set_axisbelow(True)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)

# Single, clean legend — outside or inside top-right
legend_patches = [
    mpatches.Patch(facecolor=grey_palette[s], edgecolor=edge_color,
                   label=f'Score {s}' + (' (best)' if s == 5 else ' (worst)' if s == 1 else ''))
    for s in scores
]
ax.legend(handles=legend_patches, title='Rating', loc='upper right',
          framealpha=0.9, fontsize=9, title_fontsize=9)

plt.tight_layout(pad=1.5)
for out in OUTS:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out), dpi=300, bbox_inches='tight', format='jpeg')
    print(f"Saved → {out}")
plt.close()
print("Done.")
