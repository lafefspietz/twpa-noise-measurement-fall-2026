"""Y-factor method (261020_summary.ipynb, wide-span SA + VNA gain + hot-wire fit) against the
single-point SNR method (SMW tone + noise, G from the VNA) at 5.576 GHz, from
run_methods_compare.py (10-21) and the 10-20 records.
"""
from __future__ import annotations

import json

import numpy as np

kB, h, e = 1.380649e-23, 6.62607015e-34, 1.602176634e-19
F0 = 5.576e9
ENBW = 112.8e3
A_IN = 102.36
WIRE_FIT_CSV = "../../261020/2_wire_calibration/hotwire-qubit-plane-fit.csv"
BAND = 25e6
hf2k = h * F0 / (2 * kB)
ph = 2 * hf2k
dBm = lambda w: 10 * np.log10(np.asarray(w) / 1e-3)

head = open(WIRE_FIT_CSV).readline()
kV = float(head.split("k_V ")[1].split()[0]); V0 = float(head.split("V0 ")[1].split()[0])
eta = float(head.split("eta ")[1].split(",")[0])


def TN_HE(V, f, T0):
    Th = h * f / (2 * kB)
    nu = np.maximum(np.sqrt(3) / (2 * np.pi) * e * abs(kV * 1e-3 * (V - V0 * 1e-3)) / (kB * T0), 1e-9)
    Te = T0 / 2 * (1 + (nu + 1 / nu) * np.arctan(nu))
    return eta * Th / np.tanh(Th / Te) + (1 - eta) * Th


def yfactor_method(vna_file, sa_file, tag):
    v = json.load(open(vna_file, encoding="utf-8")); d = json.load(open(sa_file, encoding="utf-8"))
    f = np.array(v["frequency_hz"])
    s_off = np.array(v["s21_off_real"]) + 1j * np.array(v["s21_off_imag"])
    s_on = np.array(v["s21_on_real"]) + 1j * np.array(v["s21_on_imag"])
    G = np.abs(s_on / s_off) ** 2
    P = {k: np.array(x) for k, x in d["power_w"].items()}
    dTN = TN_HE(d["hot_wire_on_v"], f, d["fridge_temperature_k"]) - h * f / (2 * kB)
    a_off = (P["pump_off_wire_on"] - P["pump_off_wire_0"]) / dTN
    out = {}
    for name, m in (("5.575/5.580", np.isin(np.round(f / 1e6), [5575, 5580])),
                    (f"±{BAND / 1e6:g} MHz", np.abs(f - F0) <= BAND)):
        p0, p1 = P["pump_off_wire_0"][m].mean(), P["pump_off_wire_on"][m].mean()
        pon = P["pump_on_wire_0"][m].mean()
        a, g = a_off[m].mean(), G[m].mean()
        s21 = 10 * np.log10(np.mean(np.abs(s_off[m]) ** 2))
        Toff, Ton = p0 / a, pon / (a * g)
        Tadd = Ton - hf2k - (Toff - hf2k) / g
        out[name] = dict(p0=p0, pon=pon, Y=p1 / p0, Gchain=10 * np.log10(a / (kB * ENBW)), G_vna=s21 + A_IN,
                         G_twpa=10 * np.log10(g), Toff=Toff, Ton=Ton, Tadd=Tadd)
        print(f"  {tag} {name:12s}: P_off {dBm(p0):.2f} dBm, rise {dBm(pon) - dBm(p0):.2f} dB, Y {p1 / p0:.4f}, "
              f"G_chain(Y) {out[name]['Gchain']:.2f} dB, G_VNA {s21 + A_IN:.2f} dB, G_TWPA {10 * np.log10(g):.2f} dB | "
              f"T_sys off {Toff:.2f} K, on {Ton:.3f} K ({Ton / ph:.2f} ph), TWPA add {Tadd / ph:.2f} ph")
    return out


def single_point(file, G_db):
    rec = json.load(open(file, encoding="utf-8"))
    res = {}
    for c in rec["conditions"]:
        y = 1e-3 * 10 ** (np.array(c["traces_dbm"]) / 10)
        x = np.linspace(F0 - 2.5e6, F0 + 2.5e6, y.shape[1])
        far, near = np.abs(x - F0) > 0.7e6, np.abs(x - F0) < 0.6e6
        n = y[:, far].mean()
        t = (y[:, near] - y[:, far].mean(axis=1)[:, None]).sum(axis=1).mean() * (x[1] - x[0]) / ENBW
        res.setdefault(c["label"], []).append((n, t))
    get = lambda lab, i: np.mean([v[i] for v in res[lab]])
    n_off_t, n_on_t = get("S SMW tone, pump off", 0), get("S SMW tone, pump on", 0)
    n_off, n_on = get("S floor, pump off", 0), get("S floor, pump on", 0)
    t_off, t_on = get("S SMW tone, pump off", 1), get("S SMW tone, pump on", 1)
    g = t_on / t_off
    a = kB * ENBW * 10 ** (G_db / 10)
    Toff, Ton = n_off / a, n_on / (a * g)
    Tadd = Ton - hf2k - (Toff - hf2k) / g
    print(f"  floor off {dBm(n_off):.3f} (with tone {dBm(n_off_t):.3f}), on {dBm(n_on):.3f} (with tone "
          f"{dBm(n_on_t):.3f}) dBm, rise {dBm(n_on) - dBm(n_off):.2f} dB; tone {dBm(t_off):.3f} / {dBm(t_on):.3f} dBm, "
          f"G_TWPA {10 * np.log10(g):.2f} dB")
    print(f"  G {G_db:.2f} dB: T_sys off {Toff:.2f} K, on {Ton:.3f} K ({Ton / ph:.2f} ph), TWPA add {Tadd / ph:.2f} ph")
    print(f"  SMW-path A_in implied by G: {-15 - (dBm(t_off) - G_db):.2f} dB")
    return dict(Toff=Toff, Ton=Ton)


files = json.load(open("methods_compare_files.json"))
print("Y-factor method, 10-20 (8_52ghz_6dbm):")
y20 = yfactor_method("../../261020/8_52ghz_6dbm_vna.json", "../../261020/8_52ghz_6dbm_sa.json", "10-20")
print("Y-factor method, 10-21 (now):")
y21 = yfactor_method(files["vna"], files["sa"], "10-21")
G_now = y21["5.575/5.580"]["G_vna"]
print(f"\nsingle-point SNR method, 10-21 (now), G from the VNA run just before = {G_now:.2f} dB:")
single_point("single_point_smw.json", G_now)
print("  with G 53.72 dB:")
single_point("single_point_smw.json", 53.72)
