"""Noise floor vs SA reference level, flux output and noise source output, then the
Y factor (hot wire 0 / 0.1 V) against the single-point SNR method (ZVA CW tone with the
known A_in, and the SMW200A tone), all at 5.576 GHz, through the lab server.

The SA is driven with raw SCPI (single sweeps, ASCII traces) while the live view is on
hold; every other instrument goes through /api/{name}. Every condition stores its raw
traces in dBm. All instruments are put back to their starting state at the end, also
after an error.

Run:  .venv/Scripts/python.exe run_ref_level_check.py [out.json]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "https://ydkim-laptop-ubuntu.tailc20c5e.ts.net"
F0 = 5.576e9
N = 10              # traces per condition
N_Y = 20            # traces per Y-factor state
SA_COMMON = dict(center_hz=F0, rbw_hz=100e3, vbw_hz=3e6, detector="AVER", average_type="POW",
                 scale="LOG", unit="DBM", sweep_time_s=1.0)
SPAN = dict(span_hz=5e6, points=1001)
ZERO = dict(span_hz=0.0, points=401)
VNA_POWER_DBM = -25.0
PROBE_POWER_DBM = -15.0
PUMP = dict(pump_frequency_hz=8.52e9, pump_power_dbm=6.0)
HOT_V = 0.1


def call(method: str, path: str, body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} {body} -> {e.code} {e.read().decode(errors='replace')}") from None


def state(name):
    return call("GET", f"/api/{name}/state")


def apply(name, **params):
    return call("POST", f"/api/{name}", params)


def scpi(cmd):
    return call("POST", "/api/spa/scpi", {"command": cmd})["response"]


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


rec = {"started": time.time(), "f0_hz": F0, "conditions": []}
ctx = {}            # what is set now (saved with each condition)


def sa_setup(ref, geom):
    apply("spa", **SA_COMMON, **geom)
    st = apply("spa", ref_level=ref)
    scpi(":INIT:CONT OFF")
    st["att_q"] = scpi(":POW:ATT?")
    st["att_auto_q"] = scpi(":POW:ATT:AUTO?")
    return st


def measure(label, ref, geom=SPAN, n=N, settle_s=0.0):
    sa = sa_setup(ref, geom)
    if settle_s:
        time.sleep(settle_s)
    traces, times = [], []
    for _ in range(n):
        scpi("INIT:IMM")
        scpi("*OPC?")
        y = [float(v) for v in scpi("TRAC? TRACE1").split(",")]
        if len(y) != geom["points"]:
            raise RuntimeError(f"{label}: trace has {len(y)} points, expected {geom['points']}")
        traces.append(y)
        times.append(time.time())
    # quick look: mean in W of the whole trace (span: bins > 0.7 MHz from F0)
    import numpy as np
    y = np.array(traces)
    if geom["span_hz"] > 0:
        x = np.linspace(F0 - geom["span_hz"] / 2, F0 + geom["span_hz"] / 2, geom["points"])
        m = np.abs(x - F0) > 0.7e6
    else:
        m = np.ones(y.shape[1], bool)
    p = 10 * np.log10(np.mean(10 ** (y[:, m] / 10)))
    log(f"{label:40s} ref {ref:5.0f}  {'span' if geom['span_hz'] else 'zero'}  mean {p:8.3f} dBm  max {y.max():7.2f}")
    rec["conditions"].append({"label": label, "ref_level": ref, "geom": geom, "ctx": dict(ctx), "sa": sa,
                              "traces_dbm": traces, "times": times})


def set_ctx(**kw):
    ctx.update(kw)


def main(out):
    snap = {n: state(n) for n in ("spa", "vna", "rf_probe", "rf_synth", "qubit_flux_bias", "noise_source_bias",
                                  "warm_switches", "attenuator")}
    rec["snapshot_before"] = snap
    log(f"att {snap['attenuator']['attenuation_db']} dB, switches {snap['warm_switches']}")
    call("POST", "/api/live/spa/hold", {"hold": True})
    time.sleep(1.5)
    try:
        run()
    finally:
        log("restoring")
        restore(snap)
        rec["finished"] = time.time()
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        log(f"saved {out}")


def run():
    # common start: probe off, pump off, fridge_in -> probe path, flux and noise source off
    apply("rf_probe", probe_enable=False)
    apply("rf_synth", pump_enable=False)
    apply("warm_switches", fridge_in="rf_probe", rx_source="fridge", receiver="spa")
    apply("qubit_flux_bias", output=False)
    apply("noise_source_bias", output=False)
    set_ctx(fridge_in="rf_probe", source="none", pump=False, flux="off", noise_src="off", wire_v=None)

    # A: floor vs ref level (today's configuration: flux off, noise source off)
    for ref in (-70, -60, -50, -40, -30, -20, -70, -40):
        measure("A floor, flux off, nsrc off", ref)
    for ref in (-70, -40):
        measure("A floor zero span, flux off, nsrc off", ref, geom=ZERO)

    # B: flux on at 0 V
    apply("qubit_flux_bias", function="VOLT", range=1.0, level=0.0)
    apply("qubit_flux_bias", output=True)
    set_ctx(flux="0V")
    time.sleep(1.0)
    for ref in (-70, -40):
        measure("B floor, flux 0V, nsrc off", ref)

    # C: + noise source on at 0 V
    apply("noise_source_bias", function="DC", offset_v=0.0)
    apply("noise_source_bias", output=True)
    set_ctx(noise_src="0V", wire_v=0.0)
    time.sleep(1.0)
    for ref in (-70, -40):
        measure("C floor, flux 0V, nsrc 0V", ref)
    measure("C floor zero span, flux 0V, nsrc 0V", -70, geom=ZERO)

    # D: Y factor, hot wire 0.1 V / 0 V, at ref -70 and -40, two cycles
    for cycle in (1, 2):
        for ref in (-70, -40):
            for v in (HOT_V, 0.0):
                apply("noise_source_bias", offset_v=v)
                set_ctx(wire_v=v)
                measure(f"D Y factor c{cycle} wire {v:g} V", ref, n=N_Y, settle_s=0.5)
    apply("noise_source_bias", offset_v=0.0)
    set_ctx(wire_v=0.0)

    # E: pump on, wire 0 V (and 0.1 V at ref -70 as a diagnostic)
    apply("rf_synth", **PUMP)
    apply("rf_synth", pump_enable=True)
    set_ctx(pump=True)
    time.sleep(2.0)
    for ref in (-70, -40):
        measure("E pump on, wire 0 V", ref)
    apply("noise_source_bias", offset_v=HOT_V)
    set_ctx(wire_v=HOT_V)
    measure("E pump on, wire 0.1 V", -70, n=N_Y, settle_s=0.5)
    apply("noise_source_bias", offset_v=0.0)
    set_ctx(wire_v=0.0)
    apply("rf_synth", pump_enable=False)
    set_ctx(pump=False)
    time.sleep(2.0)

    # F: ZVA CW tone through the VNA path (A_in known), flux 0 V, wire 0 V
    apply("warm_switches", fridge_in="vna")
    apply("vna", sweep_type="CW", cw_hz=F0, power_dbm=VNA_POWER_DBM)
    apply("vna", output=False)
    set_ctx(fridge_in="vna", source="vna_off")
    time.sleep(1.0)
    for ref in (-70, -40):
        measure("F floor, VNA path, VNA output off", ref)
    apply("vna", output=True)
    set_ctx(source=f"vna_cw {VNA_POWER_DBM:g} dBm")
    time.sleep(1.0)
    for ref in (-70, -60, -50, -40, -30):
        measure("F ZVA tone, pump off", ref)
    for ref in (-70, -40):
        measure("F ZVA tone zero span, pump off", ref, geom=ZERO)
    apply("rf_synth", pump_enable=True)
    set_ctx(pump=True)
    time.sleep(2.0)
    for ref in (-60, -50, -40):
        measure("F ZVA tone, pump on", ref)
    apply("rf_synth", pump_enable=False)
    set_ctx(pump=False)
    time.sleep(2.0)
    apply("vna", output=False)

    # G: SMW200A tone through the probe path
    apply("warm_switches", fridge_in="rf_probe")
    apply("rf_probe", probe_frequency_hz=F0, probe_power_dbm=PROBE_POWER_DBM)
    apply("rf_probe", probe_enable=True)
    set_ctx(fridge_in="rf_probe", source=f"smw {PROBE_POWER_DBM:g} dBm")
    time.sleep(1.0)
    for ref in (-60, -40):
        measure("G SMW tone, pump off", ref)
    apply("rf_probe", probe_enable=False)
    set_ctx(source="none")

    # H: back to the start configuration, for drift
    apply("noise_source_bias", output=False)
    apply("qubit_flux_bias", output=False)
    set_ctx(flux="off", noise_src="off", wire_v=None)
    time.sleep(1.0)
    for ref in (-70, -40):
        measure("H floor again, flux off, nsrc off", ref)


def restore(snap):
    steps = [
        ("rf_probe", dict(probe_enable=False)),
        ("rf_synth", dict(pump_enable=False)),
        ("noise_source_bias", dict(output=False)),
        ("qubit_flux_bias", dict(output=False)),
    ]
    v, s = snap["vna"], snap["vna"]
    steps += [
        ("vna", dict(sweep_type=s["sweep_type"], start_hz=s["start_hz"], stop_hz=s["stop_hz"], points=s["points"],
                     ifbw_hz=s["ifbw_hz"], power_dbm=s["power_dbm"], cw_hz=s["cw_hz"])),
        ("vna", dict(output=v["output"])),
        ("warm_switches", {k: snap["warm_switches"][k] for k in ("fridge_in", "rx_source", "receiver")}),
        ("attenuator", dict(attenuation_db=snap["attenuator"]["attenuation_db"])),
        ("rf_probe", {k: snap["rf_probe"][k] for k in ("probe_frequency_hz", "probe_power_dbm")}),
        ("rf_synth", {k: snap["rf_synth"][k] for k in ("pump_frequency_hz", "pump_power_dbm")}),
        ("qubit_flux_bias", {k: snap["qubit_flux_bias"][k] for k in ("function", "range", "level")}),
        ("noise_source_bias", {k: snap["noise_source_bias"][k] for k in ("function", "offset_v")}),
    ]
    for name, on in (("rf_probe", "probe_enable"), ("rf_synth", "pump_enable")):
        if snap[name][on]:
            steps.append((name, {on: True}))
    for name in ("qubit_flux_bias", "noise_source_bias"):
        if snap[name]["output"]:
            steps.append((name, {"output": True}))
    sa = snap["spa"]
    steps += [("spa", {k: sa[k] for k in ("detector", "vbw_hz", "rbw_hz", "center_hz", "span_hz", "points",
                                          "sweep_time_s", "scale", "unit", "average_type")}),
              ("spa", dict(ref_level=sa["ref_level"]))]
    for name, params in steps:
        try:
            apply(name, **params)
        except Exception as e:
            log(f"restore {name} {params} failed: {e}")
    try:
        call("POST", "/api/live/spa/hold", {"hold": False})
    except Exception as e:
        log(f"live hold off failed: {e}")
    rec["snapshot_after"] = {n: state(n) for n in snap}


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else f"{int(time.time())}-ref-level-check.json")
