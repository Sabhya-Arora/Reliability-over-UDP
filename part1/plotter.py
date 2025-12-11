import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from scipy import stats

# === Load the dataset ===
file_path = "reliability_jitter.csv"  # change path if needed
df = pd.read_csv(file_path)

# === Function to compute mean and 90% CI ===
def compute_confidence_intervals(df, group_col, time_col='ttc'):
    grouped = df.groupby(group_col)[time_col].agg(['mean', 'std', 'count'])
    grouped['sem'] = grouped['std'] / np.sqrt(grouped['count'])
    # 90% confidence interval => t(0.95)
    grouped['ci90'] = grouped['sem'] * stats.t.ppf(0.95, grouped['count'] - 1)
    return grouped.reset_index()

# === Compute for both loss and delay ===
loss_stats = compute_confidence_intervals(df, 'loss')
jitter_stats = compute_confidence_intervals(df, 'jitter')

# === Plot 1: Download time vs Packet Loss ===
plt.figure(figsize=(8, 5))
plt.errorbar(loss_stats['loss'], loss_stats['mean'], yerr=loss_stats['ci90'],
             fmt='-o', capsize=5, label='Download time vs Loss')
plt.xlabel('Packet Loss (%)', fontsize=12)
plt.ylabel('Download Time (s)', fontsize=12)
plt.title('Download Time vs Packet Loss (90% Confidence Intervals)', fontsize=13)
plt.grid(True, linestyle='--', alpha=0.7)
plt.legend()
plt.tight_layout()
plt.show()

# === Plot 2: Download time vs Network Jitter ===
plt.figure(figsize=(8, 5))
plt.errorbar(jitter_stats['jitter'], jitter_stats['mean'], yerr=jitter_stats['ci90'],
             fmt='-o', capsize=5, color='orange', label='Download time vs Jitter')
plt.xlabel('Network Jitter (ms)', fontsize=12)
plt.ylabel('Download Time (s)', fontsize=12)
plt.title('Download Time vs Network Jitter (90% Confidence Intervals)', fontsize=13)
plt.grid(True, linestyle='--', alpha=0.7)
plt.legend()
plt.tight_layout()
plt.show()
