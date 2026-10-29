"""Analysis of ref_level_check.json (run_ref_level_check.py): floor vs ref level, the Y
factor (hot wire 0 / 0.1 V) and the single-point SNR method (ZVA CW tone, A_in known).

Noise: mean in W of the bins more than 0.7 MHz from 5.576 GHz (span) or of all samples
(zero span, no tone). Tone: sum over |f - f0| < 0.6 MHz of (bin - noise) x bin / ENBW.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

import numpy as np

kB, h, e = 1.380649e-23, 6.62607015e-34, 1.602176634e-19
ENBW = 112.8e3
G_VNA_DB = 53.72
A_IN_VNA_DB = 102.36            # ZVA port -> qubit plane, RCDAT 25 dB included (2026-10-15)
WIRE_FIT_CSV = "../../261020/2_wire_calibration/hotwire-qubit-plane-fit.csv"
T0 = 0.012

W = lambda dbm: 1e-3 * 10 ** (np.asarray(dbm) / 10)
dBm = lambda w: 10 * np.log10(np.asarray(w) / 1e-3)

rec = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "ref_level_check.json", encoding="utf-8"))
f0 = rec["f0_hz"]
hf2k = h * f0 / (2 * kB)
ph = 2 * hf2k


def noise_tone(c):
    y = W(np.array(c["traces_dbm"]))            # traces x bins, W
    g = c["geom"]
    if g["span_hz"] == 0:
        pw = y.mean(axis=1)
        return pw.mean(), pw.std(ddof=1) / np.sqrt(len(pw)), None
    x = np.linspace(f0 - g["span_hz"] / 2, f0 + g["span_hz"] / 2, g["points"])
    far, near = np.abs(x - f0) > 0.7e6, np.abs(x - f0) < 0.6e6
    df = x[1] - x[0]
    n_tr = y[:, far].mean(axis=1)
    t_tr = (y[:, near] - n_tr[:, None]).sum(axis=1) * df / ENBW
    nm = n_tr.mean()
    tone = t_tr.mean() if y[:, near].mean(axis=0).max() > 3 * nm else None      # a tone 5 dB over the floor
    return nm, n_tr.std(ddof=1) / np.sqrt(len(n_tr)), tone


rows = []
for c in rec["conditions"]:
    n, n_se, t = noise_tone(c)
    rows.append(dict(c=c, label=c["label"], ref=c["ref_level"], noise=n, noise_se=n_se, tone=t,
                     att=c["sa"].get("att_q"), att_auto=c["sa"].get("att_auto_q")))

print(f"{'condition':44s} {'ref':>4} {'att':>4} {'noise dBm':>10} {'±dB':>5} {'tone dBm':>9}  T_sys(G 53.72)")
for r in rows:
    se_db = 10 * np.log10(1 + r["noise_se"] / r["noise"])
    tg = r["noise"] / (kB * ENBW * 10 ** (G_VNA_DB / 10))
    tone = f"{dBm(r['tone']):9.3f}" if r["tone"] else " " * 9
    zs = " zero" if r["c"]["geom"]["span_hz"] == 0 else ""
    print(f"{r['label'] + zs:44s} {r['ref']:4.0f} {r['att']:>4} {dBm(r['noise']):10.3f} {se_db:5.3f} {tone}  {tg:6.3f} K")

by = defaultdict(list)
for r in rows:
    by[(r["label"], r["ref"], r["c"]["geom"]["span_hz"] == 0)].append(r)


def get(label, ref, zero=False, what="noise"):
    v = [r[what] for r in by[(label, ref, zero)] if r[what] is not None]
    return np.mean(v) if v else None


# ---- floor: ref level and configuration ----
print("\nfloor relative to ref -70 (dB):")
base = get("A floor, flux off, nsrc off", -70)
for ref in (-70, -60, -50, -40, -30, -20):
    print(f"  A ref {ref}: {dBm(get('A floor, flux off, nsrc off', ref)) - dBm(base):+.3f}")
for lab in ("B floor, flux 0V, nsrc off", "C floor, flux 0V, nsrc 0V", "F floor, VNA path, VNA output off",
            "H floor again, flux off, nsrc off"):
    print(f"  {lab}: ref -70 {dBm(get(lab, -70)) - dBm(base):+.3f}, ref -40 {dBm(get(lab, -40)) - dBm(base):+.3f}")

# ---- Y factor ----
head = open(WIRE_FIT_CSV).readline()
kV = float(head.split("k_V ")[1].split()[0]); V0 = float(head.split("V0 ")[1].split()[0])
eta = float(head.split("eta ")[1].split(",")[0])
Vw = kV * 1e-3 * (0.1 - V0 * 1e-3)
nu = np.sqrt(3) / (2 * np.pi) * e * abs(Vw) / (kB * T0)
Te = T0 / 2 * (1 + (nu + 1 / nu) * np.arctan(nu))
dTN = eta * (hf2k / np.tanh(hf2k / Te) - hf2k)
print(f"\nY factor: wire fit k_V {kV} mV/V, eta {eta}; 0.1 V -> T_eff {Te:.3f} K, dT_N^HE {dTN:.4f} K")
Y = {}
for ref in (-70, -40):
    hot = np.mean([get(f"D Y factor c{c} wire 0.1 V", ref) for c in (1, 2)])
    cold = np.mean([get(f"D Y factor c{c} wire 0 V", ref) for c in (1, 2)])
    hots = [get(f"D Y factor c{c} wire 0.1 V", ref) / get(f"D Y factor c{c} wire 0 V", ref) for c in (1, 2)]
    a = (hot - cold) / dTN
    Y[ref] = dict(a=a, cold=cold, T=cold / a)
    print(f"  ref {ref}: Y {hot / cold:.4f} (cycles {hots[0]:.4f}, {hots[1]:.4f}); a_off -> G_chain "
          f"{10 * np.log10(a / (kB * ENBW)):.3f} dB; T_sys,off {cold / a:.3f} K ({cold / a / ph:.2f} ph)")

# ---- SNR method: ZVA tone with A_in ----
P_in = W(-25.0 - A_IN_VNA_DB)
print(f"\nSNR method: ZVA -25 dBm - A_in {A_IN_VNA_DB} dB = {dBm(P_in):.2f} dBm at the qubit plane")
print(f"{'':10} {'tone dBm':>9} {'G_tone dB':>9} {'noise same trace':>16} {'T_sys same trace':>16}")
G_tone = {}
for ref in (-70, -60, -50, -40, -30):
    t = get("F ZVA tone, pump off", ref, what="tone")
    n = get("F ZVA tone, pump off", ref)
    if t is None:
        continue
    G_tone[ref] = t / P_in
    print(f"  ref {ref:4}: {dBm(t):9.3f} {10 * np.log10(t / P_in):9.3f} {dBm(n):16.3f} "
          f"{n / (kB * ENBW * t / P_in):14.3f} K")
for ref in (-70, -40):
    tz = get("F ZVA tone zero span, pump off", ref, zero=True) - get("F floor, VNA path, VNA output off", ref)
    print(f"  zero span ref {ref}: tone {dBm(tz):.3f} dBm (mean W - floor), G_tone {10 * np.log10(tz / P_in):.3f} dB")

print("\nT_sys,off by method (K):")
nf = {ref: get("F floor, VNA path, VNA output off", ref) for ref in (-70, -40)}
for ref in (-70, -40):
    print(f"  ref {ref}: Y factor {Y[ref]['T']:.3f}; G 53.72 {Y[ref]['cold'] / (kB * ENBW * 10 ** (G_VNA_DB / 10)):.3f}; "
          + "; ".join(f"SNR tone@{tr} {nf[ref] / (kB * ENBW * G_tone[tr]):.3f}" for tr in sorted(G_tone)))

# ---- pump on ----
print("\npump on:")
for ref in (-60, -50, -40):
    t_on, n_on = get("F ZVA tone, pump on", ref, what="tone"), get("F ZVA tone, pump on", ref)
    t_off = G_tone.get(ref, G_tone[min(G_tone, key=lambda r: abs(r - ref))]) * P_in
    if t_on is None:
        continue
    G_tw = t_on / t_off
    print(f"  ref {ref}: tone {dBm(t_on):.3f} dBm, G_TWPA {10 * np.log10(G_tw):.3f} dB, noise {dBm(n_on):.3f} dBm, "
          f"T_sys,on SNR {n_on / (kB * ENBW * t_on / P_in):.3f} K")
G_tw = np.mean([get("F ZVA tone, pump on", r, what="tone") / (G_tone[r] * P_in) for r in (-60, -50, -40)
                if r in G_tone])
for ref in (-70, -40):
    p_on = get("E pump on, wire 0 V", ref)
    Ton = p_on / (Y[ref]["a"] * G_tw)
    Toff = Y[ref]["T"]
    Tadd = Ton - hf2k - (Toff - hf2k) / G_tw
    print(f"  Y-factor ref {ref}: P_on {dBm(p_on):.3f} dBm, rise {dBm(p_on) - dBm(Y[ref]['cold']):.3f} dB, "
          f"T_sys,on {Ton:.3f} K ({Ton / ph:.2f} ph), TWPA T_add {Tadd / ph:.2f} ph (G_TWPA {10 * np.log10(G_tw):.2f} dB)")
hw = get("E pump on, wire 0.1 V", -70) / get("E pump on, wire 0 V", -70)
yoff = get("D Y factor c1 wire 0.1 V", -70) / get("D Y factor c1 wire 0 V", -70)
print(f"  hot wire pump on: Y {hw:.4f}; G_hw = dP_on/dP_off = "
      f"{10 * np.log10((get('E pump on, wire 0.1 V', -70) - get('E pump on, wire 0 V', -70)) / (Y[-70]['a'] * dTN)):.2f} dB")

# ---- SMW path ----
for ref in (-60, -40):
    t = get("G SMW tone, pump off", ref, what="tone")
    if t is not None and ref in G_tone:
        print(f"\nSMW -15 dBm, ref {ref}: tone {dBm(t):.3f} dBm -> A_in(SMW path) "
              f"{-15 - (dBm(t) - 10 * np.log10(G_tone[ref])):.2f} dB (with the ZVA G_tone)")
