"""The two T_sys methods back to back, pump 8.52 GHz / 6 dBm as on 2026-10-20:

1. Y-factor method (261020_summary.ipynb): twpa_vna_gain + twpa_noise_spectrum with the
   10-20 parameters (SA 5.5-7.5 GHz, hot wire 0 / 0.1 V x pump off / on).
2. Single-point SNR method: SMW200A tone at 5.576 GHz (-15 dBm) and the noise around it,
   pump off and on, SA span 5 MHz, ref -40, 10 x 1 s (as in the 10-21 live traces), then
   the same with the probe off.

The experiment records are downloaded next to this script.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

import run_ref_level_check as r
from run_ref_level_check import BASE, apply, call, log, state

VNA_PARAMS = {"rounds": 2, "averages": 10, "pump_settle_s": 2.0, "start_hz": 5.5e9, "stop_hz": 7.5e9, "points": 401,
              "ifbw_hz": 1000.0, "power_dbm": -25.0, "att_mode": "keep", "pump_frequency_hz": 8.52e9,
              "pump_power_dbm": 6.0, "flux_mode": "set", "flux_bias_v": 0.0, "set_switches": True,
              "finish": "restore", "label": "twpa-vna-gain-1021"}
SA_PARAMS = {"rounds": 2, "traces": 2, "settle_s": 0.5, "pump_settle_s": 2.0, "center_hz": 6.5e9, "span_hz": 2e9,
             "rbw_hz": 100e3, "vbw_hz": 3e6, "sweep_time_s": 10.0, "ref_level_dbm": -60.0, "bias_on_v": 0.1,
             "pump_frequency_hz": 8.52e9, "pump_power_dbm": 6.0, "flux_mode": "set", "flux_bias_v": 0.0,
             "set_switches": True, "finish": "restore", "fridge_temperature_k": 0.012,
             "label": "twpa-noise-spectrum-1021"}


def run_experiment(exp_id, params):
    job = call("POST", f"/api/experiments/{exp_id}/start", params)
    log(f"{exp_id} started: {job['id']}")
    while True:
        time.sleep(5)
        job = call("GET", f"/api/jobs/{job['id']}")
        if job["status"] not in ("running", "starting", "queued"):
            break
    log(f"{exp_id} {job['status']}: {job.get('message')} notes {job.get('notes')}")
    if job["status"] != "done":
        raise RuntimeError(f"{exp_id} ended {job['status']}")
    with urllib.request.urlopen(BASE + job["url"], timeout=60) as resp:
        data = resp.read()
    with open(job["file"], "wb") as fh:
        fh.write(data)
    log(f"saved {job['file']}")
    return job["file"]


def single_point(out):
    r.rec = {"started": time.time(), "f0_hz": r.F0, "conditions": []}
    snap = {n: state(n) for n in ("spa", "vna", "rf_probe", "rf_synth", "qubit_flux_bias", "noise_source_bias",
                                  "warm_switches", "attenuator")}
    r.rec["snapshot_before"] = snap
    call("POST", "/api/live/spa/hold", {"hold": True})
    time.sleep(1.5)
    try:
        apply("warm_switches", fridge_in="rf_probe", rx_source="fridge", receiver="spa")
        apply("noise_source_bias", output=False)
        apply("qubit_flux_bias", function="VOLT", range=1.0, level=0.0)
        apply("qubit_flux_bias", output=True)
        apply("rf_synth", pump_frequency_hz=8.52e9, pump_power_dbm=6.0)
        apply("rf_probe", probe_frequency_hz=r.F0, probe_power_dbm=-15.0)
        r.set_ctx(fridge_in="rf_probe", flux="0V", noise_src="off", wire_v=None)
        for pump in (False, True, False, True):
            apply("rf_synth", pump_enable=pump)
            time.sleep(2.0)
            apply("rf_probe", probe_enable=True)
            r.set_ctx(pump=pump, source="smw -15 dBm")
            time.sleep(0.5)
            r.measure(f"S SMW tone, pump {'on' if pump else 'off'}", -40)
            apply("rf_probe", probe_enable=False)
            r.set_ctx(source="none")
            time.sleep(0.5)
            r.measure(f"S floor, pump {'on' if pump else 'off'}", -40)
    finally:
        log("restoring")
        r.restore(snap)
        r.rec["finished"] = time.time()
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(r.rec, fh)
        log(f"saved {out}")


if __name__ == "__main__":
    files = {"vna": run_experiment("twpa_vna_gain", VNA_PARAMS),
             "sa": run_experiment("twpa_noise_spectrum", SA_PARAMS)}
    single_point(sys.argv[1] if len(sys.argv) > 1 else "single_point_smw.json")
    with open("methods_compare_files.json", "w") as fh:
        json.dump(files, fh)
