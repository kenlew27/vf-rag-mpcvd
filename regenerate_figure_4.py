"""
regenerate_figure_4.py

Regenerates Fig. 4 — ablation bar chart — with:
  - Exact '90%' label (no tilde) on the Full System delivery bar
  - Vertical inverted-U bracket for the 87% reduction annotation
    (sits above the bars; does not clip through them)

Saves to:
  - figures/figure_4.png  (relative to repo root)
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path

HERE = Path(__file__).parent
OUTS = [
    HERE / "figures" / "figure_4.png",
]

# ── data ──────────────────────────────────────────────────────────────────────
conditions  = ["No\nVerification", "Verifier\nOnly", "Full\nSystem"]
violations  = [77, 10, 10]
delivery    = [100, 27, 90]

fr_conditions = ["No\nVerification", "Verifier\nOnly", "Full\nSystem"]
fr_vals  = [0, 26.1, 0]

x = np.arange(len(conditions))
width = 0.35

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5.5),
                                gridspec_kw={'width_ratios': [2, 1]})

# ── (a) violations and delivery ───────────────────────────────────────────────
bar_v = ax1.bar(x - width/2, violations, width, color='#1a1a1a', label='Violations')
bar_d = ax1.bar(x + width/2, delivery,   width, color='#999999', label='Delivery')

ax1.set_title('(a) Violations and delivery (n = 100)', fontweight='bold', pad=10)
ax1.set_ylabel('Percentage (%)', fontsize=11)
ax1.set_xticks(x)
ax1.set_xticklabels(conditions, fontsize=11)
ax1.set_ylim(0, 120)
ax1.yaxis.grid(True, linestyle='--', alpha=0.7)
ax1.set_axisbelow(True)
ax1.spines['top'].set_visible(False)
ax1.spines['right'].set_visible(False)
ax1.legend(loc='upper right', framealpha=0.9)

# Bar labels
labels_v = ['77%', '10%', '10%']
labels_d = ['100%', '27%', '90%']   # exact 90%, no tilde

for bar, lbl in zip(bar_v, labels_v):
    ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1.5,
             lbl, ha='center', va='bottom', fontweight='bold', fontsize=10)

for bar, lbl in zip(bar_d, labels_d):
    ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1.5,
             lbl, ha='center', va='bottom', fontweight='bold', fontsize=10)

# ── Vertical inverted-U bracket for 87% reduction ─────────────────────────────
# Runs vertically above each violation bar and connects at the top.
x_left  = bar_v[0].get_x() + bar_v[0].get_width() / 2   # No-Verification violation bar centre
x_right = bar_v[1].get_x() + bar_v[1].get_width() / 2   # Verifier-Only violation bar centre
y_left  = 77   # top of No-Verification violation bar
y_right = 10   # top of Verifier-Only violation bar
x_mid   = (x_left + x_right) / 2
y_bracket_top = 108  # height of horizontal connector (above both bars)

# Left vertical leg: bar top → bracket height
ax1.annotate('', xy=(x_left, y_bracket_top), xytext=(x_left, y_left + 2),
             arrowprops=dict(arrowstyle='-', lw=1.2, color='black'))
# Right vertical leg: bar top → bracket height
ax1.annotate('', xy=(x_right, y_bracket_top), xytext=(x_right, y_right + 2),
             arrowprops=dict(arrowstyle='-', lw=1.2, color='black'))
# Horizontal connector at top
ax1.annotate('', xy=(x_right, y_bracket_top), xytext=(x_left, y_bracket_top),
             arrowprops=dict(arrowstyle='-', lw=1.2, color='black'))
# Label above connector
ax1.text(x_mid, y_bracket_top + 1.5, '87% reduction',
         ha='center', va='bottom', fontsize=9, fontweight='bold')

# ── (b) false rejections ──────────────────────────────────────────────────────
x2 = np.arange(len(fr_conditions))
bar_fr = ax2.bar(x2, fr_vals, width=0.5, color='white', edgecolor='black', linewidth=1.2)

ax2.set_title('(b) False rejections (n = 23)', fontweight='bold', pad=10)
ax2.set_xticks(x2)
ax2.set_xticklabels(fr_conditions, fontsize=11)
ax2.set_ylim(0, 45)
ax2.yaxis.grid(True, linestyle='--', alpha=0.7)
ax2.set_axisbelow(True)
ax2.spines['top'].set_visible(False)
ax2.spines['right'].set_visible(False)

fr_labels = ['0%\n(0/23)', '26.1%\n(6/23)', '0%\n(0/23)']
for bar, lbl in zip(bar_fr, fr_labels):
    ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.8,
             lbl, ha='center', va='bottom', fontweight='bold', fontsize=9)

plt.tight_layout(pad=2.0)
for out in OUTS:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out), dpi=300, bbox_inches='tight')
    print(f"Saved -> {out}")
plt.close()
print("Done.")
