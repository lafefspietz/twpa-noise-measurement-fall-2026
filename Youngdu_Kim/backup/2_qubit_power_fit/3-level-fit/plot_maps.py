"""Experiment vs theory maps of Re r and Im r (r = 1 - S21/S21_ref), like
qubit_power_fit.plot_fit_maps, for an N-level fit made by fit_levels.py.

The theory includes each trace's complex scale A_j (closed-form best value, as in the
fit), so it is compared with the raw normalized data. Traces outside the fit's power
range use the fit's parameters unchanged: there the theory is a prediction.

usage: python plot_maps.py sweep.json ref.json result_prefix [result_prefix ...]
"""
import json, os, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fit_levels import load, Transmon, model_traces, project

sweep_file, ref_file, prefixes = sys.argv[1], sys.argv[2], sys.argv[3:]
f, P, Sn, sig = load(sweep_file, ref_file)
# qubit_power_fit.py convention: Im r > 0 above resonance, i.e. conj of the VNA data
r_data = np.conj(1 - Sn)

for prefix in prefixes:
    run = json.load(open(prefix + ".json"))
    if "x" not in run:
        run["x"] = np.load(prefix + ".npz")["x"].tolist()
    m = model_traces(np.array(run["x"]), Transmon(run["NL"]), f, P)
    _, scaled = project(m, Sn)
    r_model = np.conj(1 - np.array(scaled))

    fig, axes = plt.subplots(2, 2, figsize=(11, 8), sharex=True, sharey=True)
    for row, (take, label) in enumerate(((np.real, r"Re$\,r$"), (np.imag, r"Im$\,r$"))):
        lim = np.nanpercentile(np.abs(take(r_data)), 99.5)
        vmin = 0 if row == 0 else -lim
        for col, (z, title) in enumerate(((r_data, "Experiment"), (r_model, f"Theory ({run['NL']}-level)"))):
            ax = axes[row, col]
            im = ax.pcolormesh(f/1e3, P, take(z), shading="nearest", cmap="RdBu_r", vmin=vmin, vmax=lim)
            fig.colorbar(im, ax=ax, label=label)
            ax.set_title(title)
            if col == 1 and run["pmax_dbm"] < P.max():
                ax.axhline(run["pmax_dbm"] + 0.5, color="k", ls="--", lw=1)
    for ax in axes[-1]: ax.set_xlabel("Frequency [GHz]")
    for ax in axes[:, 0]: ax.set_ylabel("VNA power [dBm]")
    rng = "all powers" if run["pmax_dbm"] >= P.max() else f"fit P ≤ {run['pmax_dbm']:g} dBm (dashed line), above: prediction"
    fig.suptitle(f"{run['NL']}-level fit, {rng}\n"
                 f"f01 = {run['f01_mhz']/1e3:.5f} GHz, α/2π = {run['alpha_mhz']:.1f} MHz, "
                 f"γ2/2π = {run['gamma2_mhz']:.2f} MHz, attenuation {run['attenuation_db']:.2f} dB, "
                 f"χ²_red = {run['red_chi2']:.2f}", fontsize=11)
    fig.tight_layout()
    fig.savefig(f"maps_{prefix}.png", dpi=110)
    print("saved", f"maps_{prefix}.png")
