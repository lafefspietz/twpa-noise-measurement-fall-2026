"""Noise floor vs SA sweep time (dwell per point) at 5.576 GHz, pump off, probe off,
flux 0 V, noise source 0 V: AVER/POW zero span and 5 MHz span, against SAMP/VBW 3 MHz
(mean of the samples in W, the bias-free reference). About 40 s of sweep per condition.

Run after run_ref_level_check.py, through the same lab server (live view on hold).
"""
from __future__ import annotations

import json
import sys
import time

import numpy as np

from run_ref_level_check import F0, apply, call, log, scpi, state

COMMON = dict(center_hz=F0, rbw_hz=100e3, vbw_hz=3e6, average_type="POW", scale="LOG", unit="DBM")
ZERO = dict(span_hz=0.0, points=401)
SPAN = dict(span_hz=5e6, points=1001)
REF = -70
TOTAL_S = 40.0

rec = {"started": time.time(), "f0_hz": F0, "conditions": []}


def measure(label, detector, geom, sweep_s):
    apply("spa", detector=detector, sweep_time_s=sweep_s, **COMMON, **geom)
    sa = apply("spa", ref_level=REF)
    scpi(":INIT:CONT OFF")
    n = max(1, round(TOTAL_S / sweep_s))
    traces = []
    for _ in range(n):
        scpi("INIT:IMM")
        time.sleep(sweep_s)             # *OPC? would hit the 10 s transport timeout on long sweeps
        scpi("*OPC?")
        traces.append([float(v) for v in scpi("TRAC? TRACE1").split(",")])
    y = np.array(traces)
    if geom["span_hz"]:
        x = np.linspace(F0 - geom["span_hz"] / 2, F0 + geom["span_hz"] / 2, geom["points"])
        y = y[:, np.abs(x - F0) > 0.7e6]
    per = 10 * np.log10(np.mean(10 ** (y / 10), axis=1))
    p = 10 * np.log10(np.mean(10 ** (y / 10)))
    log(f"{label:28s} {detector:4s} sweep {sweep_s:5.1f} s x {n:3d}: mean {p:8.3f} dBm, "
        f"trace-to-trace sd {per.std(ddof=1) if n > 1 else float('nan'):.3f} dB")
    rec["conditions"].append({"label": label, "detector": detector, "geom": geom, "sweep_time_s": sweep_s,
                              "sa": sa, "traces_dbm": traces})


def main(out):
    snap = {n: state(n) for n in ("spa", "rf_probe", "rf_synth", "qubit_flux_bias", "noise_source_bias",
                                  "warm_switches")}
    rec["snapshot_before"] = snap
    call("POST", "/api/live/spa/hold", {"hold": True})
    time.sleep(1.5)
    try:
        apply("rf_probe", probe_enable=False)
        apply("rf_synth", pump_enable=False)
        apply("warm_switches", fridge_in="rf_probe", rx_source="fridge", receiver="spa")
        apply("qubit_flux_bias", function="VOLT", range=1.0, level=0.0)
        apply("qubit_flux_bias", output=True)
        apply("noise_source_bias", function="DC", offset_v=0.0)
        apply("noise_source_bias", output=True)
        time.sleep(1.0)
        for t in (1.0, 20.0, 0.5, 5.0, 2.0, 10.0):
            measure("zero span", "AVER", ZERO, t)
        for t in (1.0, 20.0, 5.0):
            measure("5 MHz span", "AVER", SPAN, t)
        measure("zero span", "SAMP", ZERO, 1.0)
        measure("zero span", "SAMP", ZERO, 20.0)
        measure("5 MHz span", "SAMP", SPAN, 1.0)
        measure("zero span", "AVER", ZERO, 1.0)
        measure("zero span", "AVER", ZERO, 20.0)
    finally:
        log("restoring")
        for name, params in (
                ("noise_source_bias", dict(output=False)),
                ("qubit_flux_bias", dict(output=False)),
                ("qubit_flux_bias", {k: snap["qubit_flux_bias"][k] for k in ("function", "range", "level")}),
                ("noise_source_bias", {k: snap["noise_source_bias"][k] for k in ("function", "offset_v")}),
                ("spa", {k: snap["spa"][k] for k in ("detector", "vbw_hz", "rbw_hz", "center_hz", "span_hz", "points",
                                                     "sweep_time_s", "scale", "unit", "average_type")}),
                ("spa", dict(ref_level=snap["spa"]["ref_level"]))):
            try:
                apply(name, **params)
            except Exception as e:
                log(f"restore {name} {params} failed: {e}")
        for name in ("qubit_flux_bias", "noise_source_bias"):
            if snap[name]["output"]:
                apply(name, output=True)
        call("POST", "/api/live/spa/hold", {"hold": False})
        rec["snapshot_after"] = {n: state(n) for n in snap}
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        log(f"saved {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sweep_time_check.json")
