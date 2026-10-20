import copy
import json
import re
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from labserver.averaging import TraceAverager
from labserver.config import load_config
from labserver.drivers import Trace
from labserver.experiments.qubit_power_sweep import segment_points, sweep_values
from labserver.server import create_app
from labserver.sim import SimWorld

URL = "/api/experiments/qubit_power_sweep"


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    for icfg in c["instruments"].values():
        icfg.update(retry_delay_s=0, settle_after_read_s=0, ramp_delay_s=0)
    c["data_dir"] = str(tmp_path / "data")
    c["settings_file"] = str(tmp_path / "settings.local.yaml")      # not the real one
    return c


@pytest.fixture
def world():
    return SimWorld(time_scale=0)


@pytest.fixture
def client(cfg, world):
    with TestClient(create_app(cfg, sim=True, world=world)) as c:
        yield c


def seg(start, stop, step, averages):
    return {"start_dbm": start, "stop_dbm": stop, "step_db": step, "averages": averages}


SMALL = {"segments": [seg(-70, -50, 10, 4), seg(-50, -10, 20, 2)], "settle_s": 0, "points": 101}


def plan(client, **params):
    r = client.post(f"{URL}/plan", json=params)
    assert r.status_code == 200, r.text
    return r.json()


def wait_job(client, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        job = client.get("/api/jobs/current").json()
        if job["status"] not in ("starting", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


# ---------------------------------------------------------------- settings / plan
def test_list_and_describe(client):
    exps = client.get("/api/experiments").json()
    assert [e["id"] for e in exps] == ["qubit_flux_sweep", "qubit_power_sweep", "qubit_power_sweep_sa",
                                     "y_factor_measurement", "twpa_pump_sweep", "twpa_signal_sweep",
                                     "twpa_noise_spectrum", "twpa_vna_gain"]
    d = client.get(f"{URL}").json()
    seg_param = next(p for p in d["params"] if p["key"] == "segments")
    assert [c["key"] for c in seg_param["columns"]] == ["start_dbm", "stop_dbm", "step_db", "averages"]
    assert client.get("/api/experiments/nope").status_code == 503


def test_sweep_values_and_segments():
    assert sweep_values(-63, -60, 1) == ([-63, -62, -61, -60], None)
    vals, warn = sweep_values(0, 1, 0.3)
    assert vals == [0, 0.3, 0.6, 0.9] and "not on the" in warn
    pts, errors, _ = segment_points([seg(-80, -70, 5, 100), seg(-70, -60, 10, 10)])
    assert pts == [(-80, 100), (-75, 100), (-70, 100), (-60, 10)] and errors == []   # -70 measured once
    _, errors, _ = segment_points([seg(-80, -60, 10, 1), seg(-70, -65, 5, 1)])
    assert any("overlap" in e for e in errors)


def test_default_plan(client):
    r = plan(client)
    assert r["errors"] == []
    powers = [p["power_dbm"] for p in r["points"]]
    assert powers[0] == -80 and powers[-1] == 0 and len(powers) == len(set(powers))
    assert {p["averages"] for p in r["points"] if p["power_dbm"] < -60} == {100}
    assert r["estimate_s"] > 0


@pytest.mark.parametrize("params,needle", [
    ({"segments": [seg(-90, -70, 5, 1)]}, "outside the limit"),
    ({"segments": [seg(-40, -30, 0, 1)]}, "step must be > 0"),
    ({"segments": [seg(-40, -30, 1, 0)]}, "traces per point"),
    ({"segments": []}, "at least one"),
    ({"stop_hz": 1e9}, "stop frequency"),
])
def test_plan_errors(client, params, needle):
    assert any(needle in e for e in plan(client, **params)["errors"])


@pytest.mark.parametrize("body", [
    {"swep": 1},                                                   # unknown setting
    {"finish": "explode"},                                         # unknown option
    {"segments": [{"start_dbm": -40, "stop_dbm": -30}]},          # missing columns
    {"segments": [{**seg(-40, -30, 1, 1), "foo": 1}]},            # unknown column
])
def test_bad_settings_are_refused(client, body):
    r = client.post(f"{URL}/plan", json=body)
    assert r.status_code == 400 and r.json()["error"] == "invalid_settings"


def test_plan_checks_switches_without_changing_them(client):
    client.post("/api/warm_switches", json={"receiver": "spa"})
    assert any("receiver=spa" in w for w in plan(client, set_switches=False)["warnings"])
    assert any("receiver spa -> vna" in n for n in plan(client, set_switches=True)["notes"])
    assert client.get("/api/warm_switches/state").json()["receiver"] == "spa"


# ---------------------------------------------------------------- running
def test_run_saves_2d_result_and_restores(client, cfg):
    client.post("/api/vna", json={"power_dbm": -33, "output": False, "points": 201})
    client.post("/api/warm_switches", json={"receiver": "spa"})
    r = client.post(f"{URL}/start", json=SMALL)
    assert r.status_code == 200, r.text
    job = wait_job(client)
    assert job["status"] == "done", job
    assert job["points_done"] == job["points_total"] == 5
    assert job["traces_done"] == job["traces_total"] == 3 * 4 + 2 * 2

    full = client.get(f"/api/jobs/{job['id']}?since=0").json()
    assert [row["power_dbm"] for row in full["rows"]] == [-70, -60, -50, -30, -10]
    assert len(full["axis"]["x"]) == 101 and len(full["rows"][0]["s_db"]) == 101
    assert client.get(f"/api/jobs/{job['id']}?since=3").json()["rows"][0]["power_dbm"] == -30

    rec = json.loads(next(Path(cfg["data_dir"]).rglob("*qubit-power-sweep.json")).read_text())
    assert rec["status"] == "done" and rec["power_dbm"] == [-70, -60, -50, -30, -10]
    assert rec["averages"] == [4, 4, 4, 2, 2]
    t0, t1 = np.array(rec["point_start_time"]), np.array(rec["point_end_time"])
    assert len(t0) == len(t1) == 5 and np.all(t1 >= t0) and np.all(t0[1:] >= t1[:-1])
    assert rec["start_time"] <= t0[0] and t1[-1] <= rec["finish_time"] + 1
    s = np.array(rec["s_real"]) + 1j * np.array(rec["s_imag"])
    assert s.shape == (5, 101)
    # the sim qubit saturates around -36 dBm at the VNA: the dip is deeper at low power
    assert np.min(np.abs(s[0])) < np.min(np.abs(s[-1]))
    assert "instruments" in rec and "attenuator" in rec["instruments"]

    vna = client.get("/api/vna/state").json()
    assert vna["power_dbm"] == -33 and vna["output"] is False and vna["points"] == 201
    assert client.get("/api/warm_switches/state").json()["receiver"] == "spa"
    assert client.get("/api/status").json()["job"]["status"] == "done"


def test_start_refuses_bad_plan_and_second_job(client, world):
    r = client.post(f"{URL}/start", json={"segments": [seg(-90, -70, 5, 1)]})
    assert r.status_code == 400 and "outside the limit" in r.json()["detail"]
    world.time_scale = 1.0          # slow enough to still be running
    assert client.post(f"{URL}/start", json=SMALL).status_code == 200
    r = client.post(f"{URL}/start", json=SMALL)
    assert r.status_code == 409
    client.post(f"/api/jobs/{client.get('/api/jobs/current').json()['id']}/stop")
    assert wait_job(client)["status"] == "stopped"


def test_stop_keeps_partial_data_and_reserves_instruments(client, cfg, world):
    world.time_scale = 1.0
    job = client.post(f"{URL}/start", json={**SMALL, "finish": "rf_off"}).json()
    t0 = time.monotonic()
    while client.get(f"/api/jobs/{job['id']}").json()["n_rows"] < 1:
        assert time.monotonic() - t0 < 20
        time.sleep(0.02)
    r = client.post("/api/vna", json={"power_dbm": -20})
    assert r.status_code == 409             # the sweep holds the VNA
    client.post(f"/api/jobs/{job['id']}/stop")
    done = wait_job(client)
    assert done["status"] == "stopped" and done["file"]
    rec = json.loads(next(Path(cfg["data_dir"]).rglob("*.json")).read_text())
    assert rec["status"] == "stopped" and 1 <= len(rec["power_dbm"]) < 5
    assert len(rec["point_start_time"]) == len(rec["point_end_time"]) == len(rec["power_dbm"])
    assert client.get("/api/vna/state").json()["output"] is False


def test_outputs_off_stops_the_job_without_its_finish_step(client, cfg, world):
    world.time_scale = 1.0
    client.post("/api/vna", json={"output": True})      # "restore" would turn it back on
    job = client.post(f"{URL}/start", json={**SMALL, "finish": "restore"}).json()
    t0 = time.monotonic()
    while client.get(f"/api/jobs/{job['id']}").json()["n_rows"] < 1:
        assert time.monotonic() - t0 < 20
        time.sleep(0.02)
    assert client.post("/api/outputs_off").json()["results"]["vna"] == "off"
    done = wait_job(client)
    assert done["status"] == "outputs_off"
    rec = json.loads(next(Path(cfg["data_dir"]).rglob("*.json")).read_text())
    assert rec["status"] == "outputs_off" and "finish" not in rec
    assert not world.sources["vna"]["on"]
    assert client.get("/api/status").json()["turning_off"] is False
    assert client.post("/api/vna", json={"output": False}).status_code == 200


# ---------------------------------------------------------------- power sweep with the step attenuator
def att_seg(start, stop, step, averages):
    return {"start_db": start, "stop_db": stop, "step_db": step, "averages": averages}


# drive after the attenuator -60, -40, -20 dBm; the sim qubit saturates around -36 dBm there.
# IFBW 10 Hz keeps the sim receiver noise below the signal at 60 dB.
ATT_SMALL = {"power_control": "attenuator", "vna_power_dbm": 0, "att_segments": [att_seg(60, 20, 20, 2)],
             "settle_s": 0, "points": 101, "ifbw_hz": 10}


def test_attenuator_plan(client):
    d = client.get(URL).json()
    p = next(p for p in d["params"] if p["key"] == "att_segments")
    assert [c["key"] for c in p["columns"]] == ["start_db", "stop_db", "step_db", "averages"]
    assert p["show_if"] == {"power_control": ["attenuator"]}
    assert "attenuator" in d["instruments"]

    client.post("/api/attenuator", json={"attenuation_db": 25})
    r = plan(client, **ATT_SMALL)
    assert r["errors"] == []
    assert [pt["attenuation_db"] for pt in r["points"]] == [60, 40, 20]
    assert r["columns"][1] == ["attenuation_db", "attenuator", "dB"]
    assert any("drive after the attenuator -60 -> -20 dBm" in n for n in r["notes"])
    assert any("at 25 dB now; set to 60 dB" in n and "restored" in n for n in r["notes"])
    assert client.get("/api/attenuator/state").json()["attenuation_db"] == 25     # plan changes nothing
    # the default attenuation segments are valid too
    assert plan(client, power_control="attenuator")["errors"] == []


@pytest.mark.parametrize("params,needle", [
    ({"att_segments": [att_seg(70, 60, 5, 1)]}, "attenuation outside the limit"),
    ({"att_segments": [att_seg(40, 39, 0.1, 1)]}, "not a multiple of 0.25 dB"),
    ({"vna_power_dbm": 5}, "VNA power outside the limit [-80, 0] dBm: 5"),
    ({"att_segments": []}, "at least one segment"),
])
def test_attenuator_plan_errors(client, params, needle):
    assert any(needle in e for e in plan(client, **{**ATT_SMALL, **params})["errors"])


def test_attenuator_run_steps_attenuator_at_fixed_vna_power(client, cfg):
    client.post("/api/vna", json={"power_dbm": -33, "output": False, "points": 201})
    client.post("/api/attenuator", json={"attenuation_db": 25})
    r = client.post(f"{URL}/start", json=ATT_SMALL)
    assert r.status_code == 200, r.text
    job = wait_job(client)
    assert job["status"] == "done", job

    full = client.get(f"/api/jobs/{job['id']}?since=0").json()
    assert full["axis"]["y_key"] == "attenuation_db" and full["axis"]["y_unit"] == "dB"
    assert [row["attenuation_db"] for row in full["rows"]] == [60, 40, 20]

    rec = json.loads(next(Path(cfg["data_dir"]).rglob("*qubit-power-sweep.json")).read_text())
    assert rec["attenuation_db"] == [60, 40, 20] and rec["drive_dbm"] == [-60, -40, -20]
    assert "power_dbm" not in rec
    s = np.array(rec["s_real"]) + 1j * np.array(rec["s_imag"])
    # the dip is deeper at low drive, and the VNA power was fixed: |S21| off resonance scales
    # with the attenuation (20 dB per 20 dB step) instead of staying put
    level = np.median(np.abs(s), axis=1)
    assert np.min(np.abs(s[0])) / level[0] < np.min(np.abs(s[-1])) / level[-1]
    assert level[-1] / level[0] == pytest.approx(100, rel=0.2)
    assert rec["instruments"]["attenuator"]["attenuation_db"] == 25
    assert rec["finish"] == ["VNA restored", "switches restored", "attenuator restored"]

    vna = client.get("/api/vna/state").json()
    assert vna["power_dbm"] == -33 and vna["output"] is False and vna["points"] == 201
    assert client.get("/api/attenuator/state").json()["attenuation_db"] == 25


def test_attenuator_run_finish_last_and_restore_higher(client):
    job = client.post(f"{URL}/start", json={**ATT_SMALL, "finish": "last"}).json()
    assert wait_job(client)["status"] == "done", job
    assert client.get("/api/vna/state").json()["power_dbm"] == 0
    assert client.get("/api/attenuator/state").json()["attenuation_db"] == 20

    client.post("/api/attenuator", json={"attenuation_db": 63})     # higher than the last point
    client.post(f"{URL}/start", json=ATT_SMALL)
    assert wait_job(client)["status"] == "done"
    assert client.get("/api/attenuator/state").json()["attenuation_db"] == 63


# ---------------------------------------------------------------- averaging
def trace(y, unit="dBm", **meta):
    return Trace(x=np.arange(len(y), dtype=float), y=np.asarray(y), x_unit="Hz", y_unit=unit, meta=meta)


def test_averager_power_mean_in_db():
    avg = TraceAverager(2)
    avg.add(trace([0.0, -10.0]))
    out = avg.add(trace([-10.0, -10.0]))
    assert out.meta["averages"] == 2
    assert np.allclose(out.y, [10 * np.log10((1 + 0.1) / 2), -10])


def test_averager_window_restart_and_complex():
    avg = TraceAverager(3)
    for v in (1, 2, 3, 4):
        out = avg.add(trace([v + 1j * v], unit="S21"))
    assert out.meta["averages"] == 3 and np.allclose(out.y, [3 + 3j])     # mean of 2, 3, 4
    out = avg.add(trace([10 + 0j], unit="S21", power_dbm=-10))            # setting changed: restart
    assert out.meta["averages"] == 1 and np.allclose(out.y, [10])
    avg.set_count(1)
    assert avg.add(trace([5 + 0j], unit="S21", power_dbm=-10)).meta["averages"] == 1
    with pytest.raises(ValueError):
        avg.set_count(0)


def test_live_average_api(client):
    r = client.post("/api/live/vna/average", json={"count": 16})
    assert r.status_code == 200 and r.json()["average_count"] == 16
    assert client.post("/api/live/vna/average", json={"restart": True}).json()["averaged"] == 0
    assert client.post("/api/live/vna/average", json={"count": 0}).status_code == 400


def test_index_has_experiments_tab(client):
    html = client.get("/").text
    assert 'data-tab="experiments"' in html and 'id="tab-experiments"' in html


# ---------------------------------------------------------------- flux sweep
FLUX_URL = "/api/experiments/qubit_flux_sweep"
# 5 biases; the sim qubit is at 5.58 GHz at 0 V and about 5.27 GHz at +-0.3 V
FLUX_SMALL = {"flux_start_v": -0.6, "flux_stop_v": 0.6, "flux_step_v": 0.3, "averages": 2, "settle_s": 0,
              "start_hz": 5.2e9, "stop_hz": 5.65e9, "points": 451, "ifbw_hz": 100, "power_dbm": -40}


def flux_plan(client, **params):
    r = client.post(f"{FLUX_URL}/plan", json=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_flux_default_plan(client):
    r = flux_plan(client)
    assert r["errors"] == [], r["errors"]
    biases = [p["flux_bias"] for p in r["points"]]
    assert len(biases) == 201 and biases[0] == -0.28 and biases[-1] == -0.24
    assert r["columns"][1] == ["flux_bias", "flux bias", "V"]
    assert any("output is off" in n for n in r["notes"])


@pytest.mark.parametrize("params,needle", [
    ({"flux_start_v": -1.2, "flux_step_v": 0.01}, "flux bias outside the limit"),
    ({"flux_start_v": -0.9}, "more than 2000 flux points"),
    ({"flux_step_v": 0}, "flux step must be > 0"),
    ({"averages": 0}, "traces per point"),
    ({"power_dbm": -95}, "VNA power outside the limit [-80, 0] dBm: -95"),
    ({"stop_hz": 1e9}, "stop frequency"),
])
def test_flux_plan_errors(client, params, needle):
    assert any(needle in e for e in flux_plan(client, **params)["errors"])


def test_flux_plan_checks_range(client):
    client.post("/api/qubit_flux_bias", json={"range": 0.1, "level": 0.0, "output": True})
    assert any("range is 0.1 V" in e for e in flux_plan(client)["errors"])
    client.post("/api/qubit_flux_bias", json={"output": False})
    r = flux_plan(client)
    assert r["errors"] == [] and any("range 0.1 -> 1 V" in n for n in r["notes"])
    assert client.get("/api/qubit_flux_bias/state").json()["range"] == 0.1      # plan changes nothing


def test_flux_run_follows_qubit_and_restores(client, cfg):
    client.post("/api/qubit_flux_bias", json={"range": 0.1, "level": 0.05, "output": False})
    r = client.post(f"{FLUX_URL}/start", json=FLUX_SMALL)
    assert r.status_code == 200, r.text
    job = wait_job(client)
    assert job["status"] == "done", job
    assert job["points_done"] == job["points_total"] == 5 and job["traces_done"] == 10

    full = client.get(f"/api/jobs/{job['id']}?since=0").json()
    assert full["axis"]["y_key"] == "flux_bias" and full["axis"]["y_unit"] == "V"
    assert [row["flux_bias"] for row in full["rows"]] == [-0.6, -0.3, 0, 0.3, 0.6]

    rec = json.loads(next(Path(cfg["data_dir"]).rglob("*qubit-flux-sweep.json")).read_text())
    assert rec["status"] == "done" and rec["flux_bias"] == [-0.6, -0.3, 0, 0.3, 0.6]
    s = np.array(rec["s_real"]) + 1j * np.array(rec["s_imag"])
    f = np.array(rec["frequency_hz"])
    dips = f[np.argmin(np.abs(s), axis=1)]
    assert abs(dips[2] - 5.58e9) < 3e6                          # sweet spot at 0 V
    assert abs(dips[1] - dips[3]) < 3e6 and dips[1] < dips[2] - 200e6   # symmetric, lower off the sweet spot
    assert "flux bias restored" in rec["finish"]

    flux = client.get("/api/qubit_flux_bias/state").json()
    assert flux["output"] is False and flux["range"] == 0.1 and flux["level"] == 0.05


def test_flux_run_finish_off_and_restore_on(client):
    client.post("/api/qubit_flux_bias", json={"level": 0.1, "output": True})
    assert wait_after(client, {**FLUX_SMALL, "flux_start_v": -0.3, "flux_stop_v": 0.3})["status"] == "done"
    flux = client.get("/api/qubit_flux_bias/state").json()
    assert flux["output"] is True and flux["level"] == 0.1            # ramped back, still on
    assert wait_after(client, {**FLUX_SMALL, "finish": "off"})["status"] == "done"
    flux = client.get("/api/qubit_flux_bias/state").json()
    assert flux["output"] is False and flux["level"] == 0
    assert client.get("/api/vna/state").json()["output"] is False


def wait_after(client, params):
    r = client.post(f"{FLUX_URL}/start", json=params)
    assert r.status_code == 200, r.text
    return wait_job(client)


# ---------------------------------------------------------------- Y factor measurement
YF_URL = "/api/experiments/y_factor_measurement"
# 5 biases, 2 passes (the second one reversed), 2 traces each
YF_SMALL = {"bias_start_v": -0.2, "bias_stop_v": 0.2, "bias_step_v": 0.1, "traces": 2, "passes": 2,
            "settle_s": 0, "sweep_time_s": 0.01, "rbw_hz": 3e6, "vbw_hz": 1e3}
# (fast; the sim SA's noise mean is only exact with VBW << RBW)
CHAIN_W = 1.15e-15 * 3e6        # sim noise at 0 V in RBW 3 MHz


def yf_plan(client, **params):
    r = client.post(f"{YF_URL}/plan", json=params)
    assert r.status_code == 200, r.text
    return r.json()


def yf_run(client, params):
    r = client.post(f"{YF_URL}/start", json=params)
    assert r.status_code == 200, r.text
    return wait_job(client)


def yf_record(cfg):
    return json.loads(next(Path(cfg["data_dir"]).rglob("*y-factor.json")).read_text())


def test_yf_default_plan(client):
    d = client.get(YF_URL).json()
    assert d["turns_on"].startswith("the noise source bias")
    assert set(d["instruments"]) == {"spa", "noise_source_bias", "warm_switches", "rf_probe", "rf_synth",
                                     "qubit_flux_bias"}
    r = yf_plan(client)
    assert r["errors"] == [], r["errors"]
    biases = [p["bias_v"] for p in r["points"]]
    assert len(biases) == 41 and biases[0] == -0.2 and biases[-1] == 0.2       # as the notebook
    assert r["columns"][2] == ["bias_v", "bias", "V"]
    assert any("output is off: set to DC -0.2 V" in n for n in r["notes"])
    assert any("fridge_in vna -> rf_probe" in n for n in r["notes"])         # the sim starts on the VNA
    assert any("flux bias left as it is: output off" in n for n in r["notes"])
    client.post("/api/warm_switches", json={"receiver": "vna"})
    assert any("receiver vna -> spa" in n for n in yf_plan(client)["notes"])


def test_yf_passes_go_back_and_forth(client):
    pts = yf_plan(client, **YF_SMALL)["points"]
    assert [p["bias_v"] for p in pts] == [-0.2, -0.1, 0, 0.1, 0.2, 0.2, 0.1, 0, -0.1, -0.2]
    assert [p["pass"] for p in pts] == [0] * 5 + [1] * 5


@pytest.mark.parametrize("params,needle", [
    ({"bias_stop_v": 0.3}, "noise source bias outside the limit"),
    ({"bias_step_v": 0}, "bias step must be > 0"),
    ({"traces": 0}, "traces per point"),
    ({"passes": 0}, "passes must be"),
    ({"rbw_hz": 0}, "RBW and VBW"),
    ({"sweep_time_s": 0}, "sweep time must be > 0"),
    ({"flux_mode": "set", "flux_bias_v": 2}, "flux bias outside the limit [-1, 1] V: 2"),
    ({"ref_level_unit": "w", "ref_level_w": -20}, "reference level in W must be > 0"),
])
def test_yf_plan_errors(client, params, needle):
    assert any(needle in e for e in yf_plan(client, **params)["errors"])


def test_yf_plan_checks_sources(client):
    client.post("/api/noise_source_bias", json={"function": "SIN", "output": True})
    assert any("on in SIN mode" in e for e in yf_plan(client)["errors"])
    client.post("/api/noise_source_bias", json={"function": "DC", "offset_v": 0.1})
    r = yf_plan(client)
    assert r["errors"] == [] and any("output is on at 0.1 V" in n for n in r["notes"])
    # the probe is turned off by the sweep; the VNA matters only if the switches stay on it
    client.post("/api/rf_probe", json={"probe_enable": True})
    assert any("probe is on" in n and "back on after" in n for n in yf_plan(client)["notes"])
    client.post("/api/warm_switches", json={"fridge_in": "vna"})
    client.post("/api/vna", json={"output": True})
    assert not any("VNA RF" in w for w in yf_plan(client)["warnings"])
    assert any("VNA RF output is on" in w for w in yf_plan(client, set_switches=False)["warnings"])
    # flux bias: set mode checks it as the flux sweep does
    client.post("/api/qubit_flux_bias", json={"range": 0.1, "level": 0.0, "output": True})
    assert any("range is 0.1 V" in e for e in yf_plan(client, flux_mode="set", flux_bias_v=0.3)["errors"])
    assert any("flux bias left as it is: on at 0 V" in n for n in yf_plan(client)["notes"])
    # LIN scale is allowed but warned about
    assert not any("LIN scale" in w for w in yf_plan(client)["warnings"])
    assert any("LIN scale" in w for w in yf_plan(client, scale="LIN")["warnings"])
    # plan changes nothing
    assert client.get("/api/noise_source_bias/state").json()["offset_v"] == 0.1
    assert client.get("/api/rf_probe/state").json()["probe_enable"] is True


def test_yf_run_saves_power_vs_bias_and_restores(client, cfg):
    client.post("/api/noise_source_bias", json={"function": "SIN", "offset_v": 0.05, "output": False})
    client.post("/api/spa", json={"span_hz": 1e9, "center_hz": 6e9, "unit": "DBM", "ref_level": -20,
                                  "scale": "LIN"})
    client.post("/api/warm_switches", json={"fridge_in": "vna", "receiver": "vna", "rx_source": "fridge"})
    # a probe tone at the measurement frequency: it must be off during the sweep
    client.post("/api/rf_probe", json={"probe_enable": True, "probe_frequency_hz": 5.58e9, "probe_power_dbm": -20})
    job = yf_run(client, YF_SMALL)
    assert job["status"] == "done", job
    assert job["points_done"] == job["points_total"] == 10 and job["traces_done"] == 20

    full = client.get(f"/api/jobs/{job['id']}?since=0").json()
    assert full["axis"]["plot"] == "xy" and full["axis"]["x_key"] == "bias_v"
    assert [row["bias_v"] for row in full["rows"]] == [-0.2, -0.1, 0, 0.1, 0.2, 0.2, 0.1, 0, -0.1, -0.2]

    rec = yf_record(cfg)
    assert rec["status"] == "done" and rec["experiment"] == "y_factor_measurement"
    assert rec["pass"] == [0] * 5 + [1] * 5 and rec["traces"] == [2] * 10
    assert rec["frequency_hz"] == 5.58e9
    assert rec["spa_setup"]["span_hz"] == 0 and rec["spa_setup"]["unit"] == "W"
    assert rec["spa_setup"]["scale"] == "LOG"                                # the default, not the LIN it found
    assert rec["spa_setup"]["ref_level"] == pytest.approx(10 ** (-45 / 10) * 1e-3)        # -45 dBm, sent in W
    assert np.array(rec["trace_mean_w"]).shape == (10, 2) and "samples_w" not in rec
    assert rec["flux_bias_state"]["output"] is False                         # left as it was
    p = np.array(rec["power_w"])
    # sim hot wire: noise ~ 1.15e-15 + 1e-14 V^2 W/Hz, so +-0.2 V is ~35 % above 0 V and symmetric;
    # 0 V is the bare chain noise, so the probe tone was off
    assert p[0] / p[2] == pytest.approx(1.35, rel=0.05) and p[4] / p[0] == pytest.approx(1, rel=0.05)
    assert p[2] == pytest.approx(CHAIN_W, rel=0.05)
    assert rec["instruments"]["noise_source_bias"]["function"] == "SIN"
    assert rec["finish"] == ["noise source restored", "SA restored", "switches restored", "probe back on"]

    ns = client.get("/api/noise_source_bias/state").json()
    assert ns["output"] is False and ns["function"] == "SIN" and ns["offset_v"] == 0.05
    sa = client.get("/api/spa/state").json()
    assert sa["span_hz"] == 1e9 and sa["center_hz"] == 6e9 and sa["unit"] == "DBM" and sa["ref_level"] == -20
    assert sa["scale"] == "LIN"
    assert sa["continuous"] is True and sa["sweep_time_auto"] is True   # auto again once the span is back
    sw = client.get("/api/warm_switches/state").json()
    assert sw["receiver"] == "vna" and sw["fridge_in"] == "vna"
    assert client.get("/api/rf_probe/state").json()["probe_enable"] is True


def test_yf_flux_set_and_restore(client, cfg):
    client.post("/api/qubit_flux_bias", json={"range": 1.0, "level": 0.05, "output": False})
    job = yf_run(client, {**YF_SMALL, "passes": 1, "flux_mode": "set", "flux_bias_v": 0.3})
    assert job["status"] == "done", job
    rec = yf_record(cfg)
    assert rec["flux_bias_state"]["output"] is True and rec["flux_bias_state"]["level"] == 0.3
    assert "flux bias restored" in rec["finish"]
    flux = client.get("/api/qubit_flux_bias/state").json()
    assert flux["output"] is False and flux["level"] == 0.05


def test_yf_finish_off_samples_and_clipping(client, cfg):
    client.post("/api/qubit_flux_bias", json={"range": 1.0, "level": 0.0, "output": False})
    params = {**YF_SMALL, "passes": 1, "traces": 1, "finish": "off", "save_samples": True, "ref_level_unit": "w", "ref_level_w": 1e-12,
              "flux_mode": "set", "flux_bias_v": -0.2}
    job = yf_run(client, params)
    assert job["status"] == "done", job
    assert any("reference level (1e-12 W)" in n and "may have clipped" in n for n in job["notes"])
    rec = yf_record(cfg)
    assert np.array(rec["samples_w"]).shape == (5, 1, 401) and rec["spa_setup"]["ref_level"] == 1e-12
    assert rec["params"]["ref_level_w"] == 1e-12 and "ref_level_dbm" not in rec["params"]
    assert rec["params"]["flux_bias_v"] == -0.2
    assert rec["finish"] == ["noise source at 0 V, off", "flux bias at 0 V, off"]
    ns = client.get("/api/noise_source_bias/state").json()
    assert ns["output"] is False and ns["offset_v"] == 0
    flux = client.get("/api/qubit_flux_bias/state").json()
    assert flux["output"] is False and flux["level"] == 0
    assert client.get("/api/spa/state").json()["unit"] == "W"                  # SA left as swept
    assert client.get("/api/warm_switches/state").json()["fridge_in"] == "rf_probe"


def test_yf_stop_keeps_partial_data(client, cfg, world):
    world.time_scale = 1.0
    job = client.post(f"{YF_URL}/start", json={**YF_SMALL, "sweep_time_s": 0.2}).json()
    t0 = time.monotonic()
    while client.get(f"/api/jobs/{job['id']}").json()["n_rows"] < 1:
        assert time.monotonic() - t0 < 20
        time.sleep(0.02)
    for name, body in (("noise_source_bias", {"offset_v": 0}), ("qubit_flux_bias", {"level": 0}),
                       ("rf_probe", {"probe_enable": True}), ("rf_synth", {"pump_enable": True})):
        assert client.post(f"/api/{name}", json=body).status_code == 409     # reserved
    client.post(f"/api/jobs/{job['id']}/stop")
    assert wait_job(client)["status"] == "stopped"
    rec = yf_record(cfg)
    assert rec["status"] == "stopped" and 1 <= len(rec["power_w"]) < 10
    assert client.get("/api/noise_source_bias/state").json()["output"] is False


def test_yf_average_detector_and_sweep_time(client, cfg):
    r = yf_plan(client)                                   # empty field: 1 s
    assert r["derived"] == {"sweep_time_s": 1.0}
    assert any("sweep time 1 s per trace (the default)" in n for n in r["notes"])
    assert not any("RBW" in w or "VBW" in w or "sweep" in w for w in r["warnings"]), r["warnings"]
    assert yf_plan(client, sweep_time_s=2)["derived"] == {}                  # typed: used as is
    assert "detector" not in {p["key"] for p in client.get(YF_URL).json()["params"]}
    # RBW 1 MHz reads low, a narrow VBW biases it, a short sweep barely averages
    w = yf_plan(client, rbw_hz=1e6, vbw_hz=1e3, sweep_time_s=0.01)["warnings"]
    assert any("use RBW <= 100 kHz" in x for x in w)
    assert any("VBW filter averages in the vertical scale" in x for x in w)
    assert any("each point averages only ~1 samples" in x for x in w)
    assert not any("correction" in x for x in w)

    client.post("/api/spa", json={"detector": "POS", "average_type": "LOG"})
    job = yf_run(client, {**YF_SMALL, "sweep_time_s": None, "passes": 1, "traces": 1})
    assert job["status"] == "done", job
    rec = yf_record(cfg)
    assert rec["detector"] == "AVER" and rec["average_type"] == "POW"
    assert rec["spa_setup"]["detector"] == "AVER" and rec["spa_setup"]["sweep_time_s"] == 1.0
    sa = client.get("/api/spa/state").json()
    assert sa["detector"] == "POS"                                           # restored
    assert sa["average_type"] == "POW"                                       # stays POW


def test_yf_uses_the_rbw_vbw_the_sa_took(client, cfg):
    """The ESA snaps RBW/VBW to 1-3-10 steps (the sim too): 20 kHz -> 30 kHz, 2 MHz -> 3 MHz."""
    job = yf_run(client, {**YF_SMALL, "rbw_hz": 2e4, "vbw_hz": 2e6, "passes": 1, "traces": 1})
    assert job["status"] == "done", job
    assert any("the SA took RBW 30 kHz (asked for 20 kHz)" in n for n in job["notes"])
    assert any("the SA took VBW 3000 kHz (asked for 2000 kHz)" in n for n in job["notes"])
    rec = yf_record(cfg)
    assert rec["rbw_hz"] == 3e4 and rec["vbw_hz"] == 3e6 and rec["sweep_time_s"] == 0.01
    assert rec["params"]["rbw_hz"] == 2e4                         # what was typed stays in params
    # only what the run used: the reference level in dBm (the default), no pump settings (pump left as it is)
    assert rec["params"]["ref_level_dbm"] == -45 and "ref_level_w" not in rec["params"]
    assert not {"pump_frequency_hz", "pump_power_dbm", "pump_settle_s", "flux_bias_v"} & set(rec["params"])
    full = client.get(f"/api/jobs/{job['id']}?since=0").json()
    assert "RBW 30 kHz, VBW 3000 kHz, sweep 0.01 s" in full["axis"]["title"]


@pytest.mark.parametrize("params,needle", [
    ({"pump_power_dbm": 15}, "pump power outside the limit [-80, 10] dBm: 15"),
    ({"pump_frequency_hz": 20e9}, "pump frequency must be within 0.01..15 GHz"),
    ({"pump_settle_s": -1}, "wait after the pump must be >= 0"),
])
def test_yf_pump_plan_errors(client, params, needle):
    assert any(needle in e for e in yf_plan(client, pump_mode="set", **params)["errors"])
    assert not any("pump" in e for e in yf_plan(client, **params)["errors"])      # keep: not checked


def test_yf_pump_plan_notes(client):
    assert any(n == "TWPA pump left as it is: off" for n in yf_plan(client)["notes"])
    r = yf_plan(client, pump_mode="set", pump_frequency_hz=8.5e9, pump_power_dbm=-1)
    assert any(n == "TWPA pump: off -> on at 8.5 GHz, -1 dBm, back as it was after" for n in r["notes"])
    assert r["estimate_s"] >= 1.0                                   # the pump wait counts
    client.post("/api/rf_synth", json={"pump_frequency_hz": 8.5e9, "pump_power_dbm": -1, "pump_enable": True})
    r = yf_plan(client, pump_mode="set", pump_frequency_hz=8.5e9, pump_power_dbm=-1)
    assert any(n == "TWPA pump is already on at 8.5 GHz, -1 dBm" for n in r["notes"])
    assert any(n == "TWPA pump is on at 8.5 GHz, -1 dBm: turned off"
               for n in yf_plan(client, pump_mode="off", finish="last")["notes"])
    assert client.get("/api/rf_synth/state").json()["pump_enable"] is True       # plan changes nothing


def test_yf_pump_set_and_restore(client, cfg):
    client.post("/api/rf_synth", json={"pump_frequency_hz": 6e9, "pump_power_dbm": -30, "pump_enable": False})
    job = yf_run(client, {**YF_SMALL, "passes": 1, "pump_mode": "set", "pump_frequency_hz": 8.5e9,
                          "pump_power_dbm": -1, "pump_settle_s": 0})
    assert job["status"] == "done", job
    rec = yf_record(cfg)
    assert rec["pump_state"] == {"pump_enable": True, "pump_frequency_hz": 8.5e9, "pump_power_dbm": -1,
                                 "pump_locked": True}
    # sim TWPA: 3 dB/dB above -3 dBm, so -1 dBm gives 6 dB on the 10 % of the chain noise
    # that is there at its input (0 V)
    assert rec["power_w"][2] == pytest.approx(CHAIN_W * (0.1 * 10 ** 0.6 + 0.9), rel=0.05)
    assert "TWPA pump restored" in rec["finish"]
    s = client.get("/api/rf_synth/state").json()
    assert (s["pump_enable"], s["pump_frequency_hz"], s["pump_power_dbm"]) == (False, 6e9, -30)


def test_yf_pump_off_and_finish(client, cfg, world):
    on = {"pump_frequency_hz": 8.5e9, "pump_power_dbm": -1, "pump_enable": True}
    client.post("/api/rf_synth", json=on)
    job = yf_run(client, {**YF_SMALL, "passes": 1, "traces": 1, "pump_mode": "off", "pump_settle_s": 0})
    assert job["status"] == "done", job
    rec = yf_record(cfg)
    assert rec["pump_state"]["pump_enable"] is False
    assert rec["power_w"][2] == pytest.approx(CHAIN_W, rel=0.05)                  # no TWPA gain
    assert client.get("/api/rf_synth/state").json()["pump_enable"] is True        # restored
    # keep: left on and saved; finish off turns the pump off only if the sweep set it
    yf_run(client, {**YF_SMALL, "passes": 1, "traces": 1, "finish": "off"})
    assert client.get("/api/rf_synth/state").json()["pump_enable"] is True
    job = yf_run(client, {**YF_SMALL, "passes": 1, "traces": 1, "finish": "off", "pump_mode": "set",
                          "pump_frequency_hz": 8.5e9, "pump_power_dbm": -1, "pump_settle_s": 0})
    assert job["status"] == "done", job
    assert not world.sources["rf_pump"]["on"]


# ---------------------------------------------------------------- TWPA pump sweep
TP_URL = "/api/experiments/twpa_pump_sweep"
# one pump frequency, three pump powers; ref level high so the sim tone never "clips"
TP_SMALL = {"pump_freq_start_hz": 8.5e9, "pump_freq_stop_hz": 8.5e9, "pump_power_start_dbm": -3,
            "pump_power_stop_dbm": -1, "pump_power_step_db": 1, "traces": 16, "vbw_hz": 100, "settle_s": 0,
            "pump_settle_s": 0, "ref_level_dbm": 0}


def tp_plan(client, **params):
    r = client.post(f"{TP_URL}/plan", json=params)
    assert r.status_code == 200, r.text
    return r.json()


def tp_run(client, params):
    r = client.post(f"{TP_URL}/start", json=params)
    assert r.status_code == 200, r.text
    return wait_job(client)


def tp_record(cfg):
    return json.loads(next(Path(cfg["data_dir"]).rglob("*twpa-pump-sweep.json")).read_text())


def test_tp_analyze():
    from labserver.experiments.twpa_pump_sweep import analyze, sa_freqs
    f = sa_freqs(6e9, 5e6)                          # 12.5 kHz bins, bin 200 at 6 GHz
    p = np.full(f.shape, 1e-11)
    p[200] += 1e-9
    p[:10] = 3e-11                                  # outside the noise region (outer 2.3 MHz)
    a = analyze(f, p, 6e9, 6e9, 1e6, 2.3e6, 30e3)
    assert a["n_noise"] == 2 * (184 - 80 + 1)       # 80..184 bins from the center, both sides
    assert a["noise_w"] == pytest.approx(1e-11)
    assert a["peak_w"] == pytest.approx(1.01e-9) and a["peak_hz"] == 6e9
    assert a["signal_w"] == pytest.approx(1e-9)
    # a window narrower than a bin uses the nearest bin; no tone above the noise -> None
    assert analyze(f, np.full(f.shape, 1e-11), 6e9, 6.000004e9, 1e6, 2.5e6, 1e3)["signal_w"] is None


def test_tp_default_plan(client):
    d = client.get(TP_URL).json()
    assert set(d["instruments"]) == {"spa", "rf_probe", "rf_synth", "warm_switches", "attenuator", "qubit_flux_bias"}
    r = tp_plan(client)
    assert r["errors"] == [], r["errors"]
    assert len(r["points"]) == 11 * 13
    assert r["points"][0] == {"i": 0, "pump_frequency_ghz": 8.0, "pump_power_dbm": -6.0, "traces": 1}
    assert r["points"][13]["pump_frequency_ghz"] == 8.1           # frequency outer, power inner
    assert r["columns"][1] == ["pump_frequency_ghz", "pump frequency", "GHz"]
    assert any("fridge_in vna -> rf_probe" in n for n in r["notes"])
    assert any("11 pump frequencies x 13 pump powers" in n for n in r["notes"])
    one = tp_plan(client, **TP_SMALL)
    assert [p["pump_power_dbm"] for p in one["points"]] == [-3, -2, -1]


@pytest.mark.parametrize("params,needle", [
    ({"pump_power_step_db": 0}, "pump power step must be > 0"),
    ({"pump_power_stop_dbm": 15}, "pump power outside the limit [-80, 10] dBm"),
    ({"traces": 0}, "traces per point"),
    ({"span_hz": 0}, "span must be > 0"),
    ({"noise_inner_hz": 3e6}, "'noise from' must be >= 0 and below 'noise to'"),
    ({"noise_outer_hz": 3e6}, "beyond the edge of the span"),
    ({"noise_inner_hz": 2.4e6}, "bins on one side"),
    ({"probe_frequency_hz": 6.01e9}, "outside the SA span"),
    ({"probe_frequency_hz": 6.0015e9}, "falls in the noise region"),
    ({"att_mode": "set", "attenuation_db": 10.1}, "multiple of 0.25 dB"),
    ({"flux_mode": "set", "flux_bias_v": 2}, "flux bias outside the limit"),
])
def test_tp_plan_errors(client, params, needle):
    errors = tp_plan(client, **params)["errors"]
    assert any(needle in e for e in errors), errors


def test_tp_plan_warnings(client):
    assert any("within 3 RBW" in w for w in tp_plan(client, probe_frequency_hz=6.00095e9, signal_window_hz=10e3,
                                                   noise_inner_hz=1e6)["warnings"])
    assert any("narrower than the bin spacing" in w for w in tp_plan(client, signal_window_hz=1e3)["warnings"])


def test_tp_run_gain_snri_and_restore(client, cfg, world):
    client.post("/api/rf_synth", json={"pump_frequency_hz": 6e9, "pump_power_dbm": -30, "pump_enable": False})
    client.post("/api/rf_probe", json={"probe_frequency_hz": 5e9, "probe_power_dbm": -40, "probe_enable": False})
    sa0 = client.get("/api/spa/state").json()
    job = tp_run(client, TP_SMALL)
    assert job["status"] == "done", job
    rec = tp_record(cfg)
    assert rec["pump_power_dbm"] == [-3, -2, -1] and rec["pump_frequency_hz"] == [8.5e9] * 3
    assert len(rec["traces_w"]) == 3 and len(rec["frequency_hz"]) == 401
    ref = rec["reference"]
    assert ref["peak_hz"] == 6e9 and ref["snr_db"] > 15
    # sim TWPA: 0 dB at -3 dBm, 6 dB at -1 dBm; 10 % of the chain noise is before it
    assert rec["gain_db"][0] == pytest.approx(0, abs=0.3)
    assert rec["gain_db"][2] == pytest.approx(6, abs=0.3)
    rise = 10 * np.log10(0.1 * 10 ** 0.6 + 0.9)
    assert rec["noise_rise_db"][2] == pytest.approx(rise, abs=0.2)
    assert rec["snri_db"][2] == pytest.approx(6 - rise, abs=0.4)
    assert rec["snri_db"][2] == pytest.approx(rec["gain_db"][2] - rec["noise_rise_db"][2], abs=1e-3)
    assert rec["spa_setup"]["span_hz"] == 5e6 and rec["spa_setup"]["detector"] == "SAMP"
    assert set(rec["finish"]) >= {"TWPA pump restored", "probe restored", "SA restored", "switches restored"}
    rows = client.get(f"/api/jobs/{job['id']}?since=0").json()
    assert rows["axis"]["plot"] == "grid" and len(rows["axis"]["ref"]["trace_dbm"]) == 401
    assert rows["rows"][2]["gain_db"] == rec["gain_db"][2] and len(rows["rows"][2]["trace_dbm"]) == 401
    s = client.get("/api/rf_synth/state").json()
    assert (s["pump_enable"], s["pump_frequency_hz"], s["pump_power_dbm"]) == (False, 6e9, -30)
    s = client.get("/api/rf_probe/state").json()
    assert (s["probe_enable"], s["probe_frequency_hz"], s["probe_power_dbm"]) == (False, 5e9, -40)
    sa = client.get("/api/spa/state").json()
    assert (sa["span_hz"], sa["center_hz"], sa["unit"]) == (sa0["span_hz"], sa0["center_hz"], sa0["unit"])
    assert client.get("/api/warm_switches/state").json()["fridge_in"] == "vna"


def test_tp_signal_center_plan(client):
    # around the SA frequency: the probe is checked against the window there
    w = tp_plan(client, signal_center="sa", probe_frequency_hz=6.0003e9, signal_window_hz=100e3)["warnings"]
    assert any("300 kHz from the SA frequency, outside the signal window (+-100 kHz)" in x for x in w), w
    assert not any("from the SA frequency" in x for x in tp_plan(client, signal_center="sa", signal_window_hz=100e3,
                                                                 probe_frequency_hz=6.00005e9)["warnings"])
    # the window and the probe tone must both stay out of the noise region
    assert any("signal window falls in the noise region" in e
               for e in tp_plan(client, probe_frequency_hz=6.0015e9)["errors"])
    assert any("probe tone (+- signal window) falls in the noise region" in e
               for e in tp_plan(client, signal_center="sa", probe_frequency_hz=6.0015e9)["errors"])


def test_tp_signal_center_sa_finds_an_offset_tone(client, cfg):
    # the tone 50 kHz off the SA frequency (as with unlocked clocks): a window around the SA finds it
    job = tp_run(client, {**TP_SMALL, "traces": 2, "probe_frequency_hz": 6.00005e9, "signal_center": "sa",
                          "signal_window_hz": 100e3})
    assert job["status"] == "done", job
    rec = tp_record(cfg)
    assert rec["signal_center_hz"] == 6e9 and rec["params"]["signal_center"] == "sa"
    assert rec["reference"]["peak_hz"] == 6.00005e9 and rec["reference"]["snr_db"] > 15
    ax = client.get(f"/api/jobs/{job['id']}?since=0").json()["axis"]
    assert ax["f_signal"] == 6e9 and ax["f_probe"] == 6.00005e9 and "around the SA" in ax["title"]


def test_tp_finish_off(client, cfg, world):
    job = tp_run(client, {**TP_SMALL, "traces": 1, "finish": "off", "save_traces": False})
    assert job["status"] == "done", job
    assert not world.sources["rf_pump"]["on"] and not world.sources["rf_probe"]["on"]
    rec = tp_record(cfg)
    assert "traces_w" not in rec and rec["finish"] == ["TWPA pump and probe off"]
    assert "2.5 dB low" in rec["analysis_note"]


# ---------------------------------------------------------------- TWPA signal sweep
TS_URL = "/api/experiments/twpa_signal_sweep"
# sim TWPA at pump 8.5 GHz, 7 dBm: 20 dB gain; 10 % of the chain noise is before it. Three
# signal frequencies, the hot wire at 0 and 0.2 V; ref level high so the sim tone never "clips"
TS_SMALL = {"signal_freq_start_hz": 5.9e9, "signal_freq_stop_hz": 6.1e9, "signal_freq_step_hz": 100e6,
            "noise_mode": "sweep", "noise_bias_start_v": 0.0, "noise_bias_stop_v": 0.2, "noise_bias_step_v": 0.2,
            "traces": 8, "vbw_hz": 100, "settle_s": 0, "pump_settle_s": 0, "noise_settle_s": 0, "ref_level_dbm": 0}


def ts_plan(client, **params):
    r = client.post(f"{TS_URL}/plan", json=params)
    assert r.status_code == 200, r.text
    return r.json()


def ts_run(client, params):
    r = client.post(f"{TS_URL}/start", json=params)
    assert r.status_code == 200, r.text
    return wait_job(client)


def ts_record(cfg, label="twpa-signal-sweep"):
    return json.loads(next(Path(cfg["data_dir"]).rglob(f"*-{label}.json")).read_text())


def test_ts_default_plan(client):
    d = client.get(TS_URL).json()
    assert set(d["instruments"]) == {"spa", "rf_probe", "rf_synth", "warm_switches", "attenuator", "noise_source_bias",
                                     "qubit_flux_bias"}
    r = ts_plan(client)
    assert r["errors"] == [], r["errors"]
    assert len(r["points"]) == 11                   # 5.5..6.5 GHz, noise source left as it is
    assert r["points"][0] == {"i": 0, "signal_frequency_ghz": 5.5, "signal_power_dbm": -20.0, "traces": 1}
    assert [c[0] for c in r["columns"]] == ["i", "signal_frequency_ghz", "signal_power_dbm", "traces"]
    assert any("11 signal frequencies (5.5 -> 6.5 GHz) at -20 dBm = 11 points, pump off then on: 22 spectra" in n
               for n in r["notes"]), r["notes"]
    assert any("noise source left as it is: output off" in n for n in r["notes"])
    assert any("TWPA pump is off: on at 8.5 GHz, 7 dBm, off for each pump-off pass" in n for n in r["notes"])
    # noise source bias outer, signal inner
    r = ts_plan(client, **TS_SMALL)
    assert r["errors"] == [], r["errors"]
    assert [(p["noise_bias_v"], p["signal_frequency_ghz"]) for p in r["points"]] == \
        [(0, 5.9), (0, 6.0), (0, 6.1), (0.2, 5.9), (0.2, 6.0), (0.2, 6.1)]
    assert any("set to DC 0 V, then turned on" in n for n in r["notes"])
    # power axis, no reference
    r = ts_plan(client, signal_axis="power", signal_power_start_dbm=-30, signal_power_stop_dbm=-20,
                signal_power_step_db=5, reference="none")
    assert [p["signal_power_dbm"] for p in r["points"]] == [-30, -25, -20]
    assert {p["signal_frequency_ghz"] for p in r["points"]} == {6.0}
    assert any("pump on only: 3 spectra" in n for n in r["notes"])


@pytest.mark.parametrize("params,needle", [
    ({"signal_freq_step_hz": 0}, "signal frequency step must be > 0"),
    ({"signal_power_dbm": 5}, "signal power outside the limit [-145, 0] dBm"),
    ({"signal_axis": "power", "signal_power_step_db": -1}, "signal power step must be > 0"),
    ({"pump_power_dbm": 15}, "pump power outside the limit [-80, 10] dBm"),
    ({"noise_mode": "sweep", "noise_bias_stop_v": 0.5}, "noise source bias outside the limit [-0.2, 0.2] V"),
    ({"traces": 0}, "traces per point"),
    ({"span_hz": 0}, "span must be > 0"),
    ({"noise_inner_hz": 3e6}, "'noise from' must be >= 0 and below 'noise to'"),
    ({"noise_inner_hz": 2.4e6}, "bins on one side"),
    ({"signal_window_hz": 1.2e6}, "signal window reaches into the noise region"),
    ({"att_mode": "set", "attenuation_db": 10.1}, "multiple of 0.25 dB"),
    ({"flux_mode": "set", "flux_bias_v": 2}, "flux bias outside the limit"),
])
def test_ts_plan_errors(client, params, needle):
    errors = ts_plan(client, **params)["errors"]
    assert any(needle in e for e in errors), errors


def test_ts_plan_warnings_and_noise_source_checks(client):
    w = ts_plan(client, signal_freq_start_hz=8.0e9, signal_freq_stop_hz=8.5e9)["warnings"]
    assert any("the pump (8.5 GHz) is inside the SA span at signal 8.5 GHz" in x for x in w), w
    assert any("ppm of the signal frequency" in x for x in ts_plan(client, sa_offset_hz=500e3)["warnings"])
    # an output already on must be in DC to be swept; left as it is, it is only a warning
    client.post("/api/noise_source_bias", json={"function": "SIN", "output": True})
    assert any("output is on in SIN mode" in e for e in ts_plan(client, **TS_SMALL)["errors"])
    assert any("on in SIN mode, not a DC bias" in x for x in ts_plan(client)["warnings"])


def test_ts_run_snr_vs_noise_and_restore(client, cfg, world):
    client.post("/api/rf_synth", json={"pump_frequency_hz": 6e9, "pump_power_dbm": -30, "pump_enable": False})
    client.post("/api/rf_probe", json={"probe_frequency_hz": 5e9, "probe_power_dbm": -40, "probe_enable": False})
    sa0 = client.get("/api/spa/state").json()
    job = ts_run(client, TS_SMALL)
    assert job["status"] == "done", job
    rec = ts_record(cfg)
    assert rec["noise_bias_v"] == [0, 0, 0, 0.2, 0.2, 0.2]
    assert rec["signal_frequency_hz"] == [5.9e9, 6e9, 6.1e9] * 2 and rec["sa_center_hz"] == rec["signal_frequency_hz"]
    assert rec["pump_frequency_hz"] == 8.5e9 and rec["pump_power_dbm"] == 7
    assert np.array(rec["traces_w"]).shape == np.array(rec["ref_traces_w"]).shape == (6, 401)
    assert rec["peak_hz"] == rec["signal_frequency_hz"] and rec["ref_peak_hz"] == rec["signal_frequency_hz"]
    # sim: 20 dB gain; noise rise 10 log10(0.1 * 100 + 0.9) with the hot wire cold
    rise0 = 10 * np.log10(10.9)
    for i in range(3):
        assert rec["gain_db"][i] == pytest.approx(20, abs=0.3)
        assert rec["noise_rise_db"][i] == pytest.approx(rise0, abs=0.3)
        assert rec["snri_db"][i] == pytest.approx(20 - rise0, abs=0.4)
        assert rec["snri_db"][i] == pytest.approx(rec["snr_db"][i] - rec["ref_snr_db"][i], abs=1e-3)
    # the hot wire at 0.2 V (4e-16 W/Hz before the TWPA) lowers the SNR, pump on and off
    on = 10 * np.log10((1.15e-15 * 10.9 + 4e-14) / (1.15e-15 * 10.9))
    off = 10 * np.log10((1.15e-15 + 4e-16) / 1.15e-15)
    for i in range(3):
        assert rec["snr_db"][i] - rec["snr_db"][i + 3] == pytest.approx(on, abs=0.4)
        assert rec["ref_snr_db"][i] - rec["ref_snr_db"][i + 3] == pytest.approx(off, abs=0.4)
    assert rec["noise_source_state"]["output"] and rec["noise_source_state"]["function"] == "DC"
    assert rec["synth_state"]["pump_enable"] and rec["spa_setup"]["detector"] == "SAMP"
    assert set(rec["finish"]) >= {"noise source restored", "TWPA pump restored", "probe restored",
                                  "SA restored", "switches restored"}
    # the plot: signal frequency x noise source bias, each row with its own pump-off spectrum
    view = client.get(f"/api/jobs/{job['id']}?since=0").json()
    ax, rows = view["axis"], view["rows"]
    assert (ax["plot"], ax["x_key"], ax["y_key"], ax["quantities"][0]) == ("grid", "signal_frequency_hz",
                                                                           "noise_bias_v", "snr_db")
    assert ax["per_point_ref"] and "ref" not in ax and len(ax["f"]) == 401
    assert rows[4]["snr_db"] == rec["snr_db"][4] and rows[4]["sa_center_hz"] == 6e9
    assert len(rows[4]["trace_dbm"]) == len(rows[4]["ref_trace_dbm"]) == 401
    # everything back as it was
    s = client.get("/api/rf_synth/state").json()
    assert (s["pump_enable"], s["pump_frequency_hz"], s["pump_power_dbm"]) == (False, 6e9, -30)
    s = client.get("/api/rf_probe/state").json()
    assert (s["probe_enable"], s["probe_frequency_hz"], s["probe_power_dbm"]) == (False, 5e9, -40)
    ns = client.get("/api/noise_source_bias/state").json()
    assert not ns["output"] and world.noise_source_v == 0
    sa = client.get("/api/spa/state").json()
    assert (sa["span_hz"], sa["center_hz"], sa["unit"]) == (sa0["span_hz"], sa0["center_hz"], sa0["unit"])
    assert client.get("/api/warm_switches/state").json()["fridge_in"] == "vna"


def test_ts_power_axis_without_reference_and_finish_off(client, cfg, world):
    # noise source left on at 0.1 V: kept, and recorded as the bias of every point
    client.post("/api/noise_source_bias", json={"function": "DC", "offset_v": 0.1})
    client.post("/api/noise_source_bias", json={"output": True})
    job = ts_run(client, {**TS_SMALL, "noise_mode": "keep", "signal_axis": "power", "signal_frequency_hz": 6e9,
                          "signal_power_start_dbm": -30, "signal_power_stop_dbm": -20, "signal_power_step_db": 10,
                          "reference": "none", "save_traces": False, "finish": "off", "traces": 64})
    assert job["status"] == "done", job
    rec = ts_record(cfg)
    assert rec["signal_power_dbm"] == [-30, -20] and rec["noise_bias_v"] == [0.1, 0.1]
    assert "gain_db" not in rec and "ref_snr_db" not in rec and "traces_w" not in rec
    # the tone 10 dB up, the noise the same
    assert 10 * np.log10(rec["signal_w"][1] / rec["signal_w"][0]) == pytest.approx(10, abs=0.2)
    assert rec["noise_w"][1] == pytest.approx(rec["noise_w"][0], rel=0.15)
    assert rec["snr_db"][1] - rec["snr_db"][0] == pytest.approx(10, abs=0.3)
    assert rec["finish"] == ["TWPA pump and signal off"]
    assert not world.sources["rf_pump"]["on"] and not world.sources["rf_probe"]["on"]
    assert world.noise_source_v == 0.1                  # left as it was
    view = client.get(f"/api/jobs/{job['id']}?since=0").json()
    assert view["axis"]["x_key"] == "signal_power_dbm"
    assert view["axis"]["quantities"] == ["snr_db", "signal_dbm", "noise_dbm"]
    assert "gain_db" not in view["rows"][0] and "trace_dbm" not in view["rows"][0]


def test_ts_finish_off_turns_a_swept_noise_source_off(client, cfg):
    job = ts_run(client, {**TS_SMALL, "traces": 1, "signal_freq_stop_hz": 5.9e9, "finish": "off"})
    assert job["status"] == "done", job
    rec = ts_record(cfg)
    assert rec["finish"] == ["TWPA pump and signal off", "noise source at 0 V, off"]
    assert not client.get("/api/noise_source_bias/state").json()["output"]


def test_ts_stop_keeps_partial_data(client, cfg, world):
    world.time_scale = 1.0
    job = client.post(f"{TS_URL}/start", json={**TS_SMALL, "traces": 20, "vbw_hz": 1000}).json()
    t0 = time.monotonic()
    while client.get(f"/api/jobs/{job['id']}").json()["n_rows"] < 1:
        assert time.monotonic() - t0 < 30
        time.sleep(0.02)
    client.post(f"/api/jobs/{job['id']}/stop")
    assert wait_job(client, timeout=30)["status"] == "stopped"
    rec = ts_record(cfg)
    n = len(rec["snr_db"])
    assert rec["status"] == "stopped" and 1 <= n < 6
    assert all(len(rec[k]) == n for k in ("ref_snr_db", "gain_db", "noise_bias_v", "traces_w", "ref_traces_w"))


# ---------------------------------------------------------------- qubit power sweep on the SA
QS_URL = "/api/experiments/qubit_power_sweep_sa"
# sim qubit at 5.58 GHz (1 MHz wide), saturating at about -36.5 dBm at the fridge input; with the
# attenuator at 0 dB: -40 dBm probe shows a ~4.7 dB dip, -10 dBm none. VBW 1 Hz keeps the sim
# noise smooth; the tone (-73.6 dBm off resonance) is ~16 dB above the noise at RBW 1 kHz.
QS_SMALL = {"segments": [seg(-40, -10, 30, 2)], "att_mode": "set", "attenuation_db": 0,
            "freq_start_hz": 5.575e9, "freq_stop_hz": 5.585e9, "freq_step_hz": 1e6, "vbw_hz": 1,
            "settle_s": 0, "ref_level_dbm": 0}
QS_ATT = {**QS_SMALL, "power_control": "attenuator", "synth_power_dbm": 0, "att_segments": [att_seg(40, 10, 30, 2)]}


def qs_plan(client, **params):
    r = client.post(f"{QS_URL}/plan", json=params)
    assert r.status_code == 200, r.text
    return r.json()


def qs_run(client, params):
    r = client.post(f"{QS_URL}/start", json=params)
    assert r.status_code == 200, r.text
    return wait_job(client)


def qs_record(cfg):
    return json.loads(max(Path(cfg["data_dir"]).rglob("*qubit-power-sweep-sa.json")).read_text())


def test_qs_default_plan(client):
    d = client.get(QS_URL).json()
    assert set(d["instruments"]) == {"spa", "rf_probe", "rf_synth", "warm_switches", "attenuator", "qubit_flux_bias"}
    client.post("/api/attenuator", json={"attenuation_db": 25})
    r = qs_plan(client)
    assert r["errors"] == [], r["errors"]
    assert len(r["points"]) == 11 + 20 + 10                     # power lines, low first
    assert r["points"][0] == {"i": 0, "probe_power_dbm": -60.0, "drive_dbm": -85.0, "averages": 4}
    assert r["columns"][1] == ["probe_power_dbm", "SMW200A probe power", "dBm"]
    assert any("41 powers x 51 probe frequencies" in n for n in r["notes"])
    assert any("drive after the attenuator -85 -> -25 dBm" in n for n in r["notes"])
    assert any("fridge_in vna -> rf_probe" in n for n in r["notes"])
    assert r["estimate_s"] > 0
    a = qs_plan(client, power_control="attenuator")
    assert a["errors"] == [] and a["points"][0]["attenuation_db"] == 63 and a["points"][0]["drive_dbm"] == -73
    assert any("at 25 dB now; set to 63 dB before the probe is turned on" in n for n in a["notes"])
    q = qs_plan(client, line_atten_db=77.4)
    assert any("dBm at the qubit" in n for n in q["notes"])
    # the plan changes nothing
    assert client.get("/api/attenuator/state").json()["attenuation_db"] == 25
    assert client.get("/api/warm_switches/state").json()["fridge_in"] == "vna"


@pytest.mark.parametrize("params,needle", [
    ({"freq_step_hz": 0}, "probe frequency step must be > 0"),
    ({"freq_step_hz": 1e3}, "more than 2000 probe frequencies"),
    ({"segments": [seg(-10, 10, 5, 1)]}, "probe power outside the limit"),
    ({"segments": []}, "at least one segment"),
    ({"span_hz": 0}, "span must be > 0"),
    ({"signal_window_hz": 40e3}, "tone window reaches into the noise region"),
    ({"noise_inner_hz": 49e3}, "bins on one side"),
    ({"noise_outer_hz": 60e3}, "beyond the edge of the span"),
    ({"att_mode": "set", "attenuation_db": 10.1}, "multiple of 0.25 dB"),
    ({"power_control": "attenuator", "att_segments": [att_seg(40, 39, 0.1, 1)]}, "multiple of 0.25 dB"),
    ({"power_control": "attenuator", "att_segments": [att_seg(70, 60, 5, 1)]}, "attenuation outside the limit"),
    ({"flux_mode": "set", "flux_bias_v": 2}, "flux bias outside the limit"),
    ({"rbw_hz": 0}, "RBW and VBW must be > 0"),
])
def test_qs_plan_errors(client, params, needle):
    errors = qs_plan(client, **params)["errors"]
    assert any(needle in e for e in errors), errors


def test_qs_plan_warnings(client):
    assert any("within 3 RBW" in w for w in qs_plan(client, noise_inner_hz=16e3)["warnings"])
    assert any("wider than the RBW" in w for w in qs_plan(client, span_hz=1e6, noise_inner_hz=100e3,
                                                          signal_window_hz=50e3)["warnings"])


def test_qs_run_map_shows_saturation_and_restores(client, cfg, world):
    client.post("/api/rf_probe", json={"probe_frequency_hz": 5e9, "probe_power_dbm": -30, "probe_enable": False})
    client.post("/api/attenuator", json={"attenuation_db": 25})
    sa0 = client.get("/api/spa/state").json()
    job = qs_run(client, {**QS_SMALL, "line_atten_db": 70})
    assert job["status"] == "done", job
    rec = qs_record(cfg)
    assert rec["probe_power_dbm"] == [-40, -10] and rec["averages"] == [2, 2]
    assert rec["drive_dbm"] == [-40, -10] and rec["qubit_power_dbm"] == [-110, -80]
    assert len(rec["frequency_hz"]) == 11 and len(rec["peak_w"]) == 2 and len(rec["peak_w"][0]) == 11
    assert len(rec["sa_bin_offset_hz"]) == 401 and rec["sa_center_offset_hz"] == 0 and "traces_w" not in rec
    assert rec["peak_hz"][1][5] == pytest.approx(5.58e9, abs=500)
    assert all(s is not None for line in rec["signal_w"] for s in line)
    full = client.get(f"/api/jobs/{job['id']}?since=0").json()
    ax, rows = full["axis"], full["rows"]
    assert ax["y_key"] == "probe_power_dbm" and ax["no_phase"] and len(ax["x"]) == 11
    low, high = np.array(rows[0]["s_db"]), np.array(rows[1]["s_db"])
    # off resonance: tone - probe power = the sim chain, -103.55 + 70 dB
    assert high[0] == pytest.approx(-33.55, abs=0.5)
    # a dip at the qubit at low drive, gone when saturated
    assert -6 < low[5] - low[0] < -3
    assert abs(high[5] - high[0]) < 0.5
    assert "2.5 dB low" in rec["analysis_note"]
    assert rec["spa_setup"]["span_hz"] == 100e3 and rec["spa_setup"]["detector"] == "SAMP"
    assert set(rec["finish"]) >= {"probe restored", "attenuator restored", "SA restored", "switches restored"}
    s = client.get("/api/rf_probe/state").json()
    assert (s["probe_enable"], s["probe_frequency_hz"], s["probe_power_dbm"]) == (False, 5e9, -30)
    assert client.get("/api/attenuator/state").json()["attenuation_db"] == 25
    sa = client.get("/api/spa/state").json()
    assert (sa["span_hz"], sa["center_hz"]) == (sa0["span_hz"], sa0["center_hz"])
    assert client.get("/api/warm_switches/state").json()["fridge_in"] == "vna"


def test_qs_attenuator_run_and_finish(client, cfg, world):
    client.post("/api/attenuator", json={"attenuation_db": 25})
    job = qs_run(client, QS_ATT)
    assert job["status"] == "done", job
    rec = qs_record(cfg)
    assert rec["attenuation_db"] == [40, 10] and rec["drive_dbm"] == [-40, -10]
    assert "probe_power_dbm" not in rec
    full = client.get(f"/api/jobs/{job['id']}?since=0").json()
    assert full["axis"]["y_key"] == "attenuation_db"
    # s_db is tone - probe power (includes the attenuator, like the VNA S21)
    assert full["rows"][0]["s_db"][0] == pytest.approx(-33.55 - 40, abs=0.5)
    assert client.get("/api/attenuator/state").json()["attenuation_db"] == 25

    assert qs_run(client, {**QS_ATT, "finish": "last"})["status"] == "done"
    assert client.get("/api/attenuator/state").json()["attenuation_db"] == 10
    assert world.sources["rf_probe"]["on"]
    client.post("/api/attenuator", json={"attenuation_db": 63})     # higher than any point
    assert qs_run(client, QS_ATT)["status"] == "done"
    assert client.get("/api/attenuator/state").json()["attenuation_db"] == 63


def test_qs_finish_off_with_flux_and_traces(client, cfg, world):
    job = qs_run(client, {**QS_SMALL, "finish": "off", "flux_mode": "set", "flux_bias_v": 0.1,
                          "save_traces": True, "freq_stop_hz": 5.576e9})
    assert job["status"] == "done", job
    assert not world.sources["rf_probe"]["on"]
    rec = qs_record(cfg)
    assert rec["finish"] == ["probe off", "flux bias at 0 V, off"]
    assert rec["flux_bias_state"]["level"] == pytest.approx(0.1)
    assert np.array(rec["traces_w"]).shape == (2, 2, 401)


def test_qs_sa_center_offset_follows_an_unlocked_clock(client, cfg, world):
    # the probe 10 ppm high: the tone lands 55.8 kHz above the probe frequency at 5.58 GHz,
    # beyond the edge of a 100 kHz span centred on the probe
    world.synth_clock_error_ppm = 10
    params = {**QS_SMALL, "segments": [seg(-10, -10, 1, 1)], "freq_start_hz": 5.58e9, "freq_stop_hz": 5.58e9}
    job = qs_run(client, params)
    assert job["status"] == "done", job
    missed = client.get(f"/api/jobs/{job['id']}?since=0").json()["rows"][0]["s_db"][0]
    assert missed < -33.55 - 10                     # only noise in the window

    r = qs_plan(client, **{**params, "sa_offset_hz": 55.8e3})
    assert r["errors"] == [] and any("SA center = probe frequency +55.8 kHz" in n for n in r["notes"])
    job = qs_run(client, {**params, "sa_offset_hz": 55.8e3, "label": "offset"})
    assert job["status"] == "done", job
    rec = json.loads(next(Path(cfg["data_dir"]).rglob("*-offset.json")).read_text())
    assert rec["sa_center_offset_hz"] == 55.8e3 and rec["spa_setup"]["center_hz"] == 5.58e9 + 55.8e3
    assert rec["peak_hz"][0][0] == pytest.approx(5.58e9 * (1 + 10e-6), abs=300)
    assert rec["signal_w"][0][0] is not None
    s_db = client.get(f"/api/jobs/{job['id']}?since=0").json()["rows"][0]["s_db"]
    assert s_db[0] == pytest.approx(-33.55, abs=0.5)
    assert client.get("/api/spa/state").json()["center_hz"] != 5.58e9 + 55.8e3      # SA restored


def test_qs_sa_center_offset_plan_checks(client):
    assert any("ppm of the probe frequency" in w for w in qs_plan(client, sa_offset_hz=500e3)["warnings"])
    assert not any("ppm" in w for w in qs_plan(client, sa_offset_hz=-20e3)["warnings"])


# ---------------------------------------------------------------- TWPA noise spectrum
TN_URL = "/api/experiments/twpa_noise_spectrum"
TN_SMALL = {"rounds": 2, "settle_s": 0, "pump_settle_s": 0, "sweep_time_s": 0.01, "center_hz": 6e9, "span_hz": 100e6,
            "pump_frequency_hz": 8.5e9, "pump_power_dbm": 5}


def tn_plan(client, **params):
    r = client.post(f"{TN_URL}/plan", json=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_tn_default_plan_and_order(client):
    r = tn_plan(client)
    assert r["errors"] == [], r["errors"]
    assert r["warnings"] == [], r["warnings"]
    pts = [(p["round"], p["pump"], p["hot_wire_v"]) for p in r["points"]]
    assert pts[:8] == [(1, "off", 0), (1, "off", 0.1), (1, "on", 0.1), (1, "on", 0),      # every other round reversed
                       (2, "on", 0), (2, "on", 0.1), (2, "off", 0.1), (2, "off", 0)]
    assert len(pts) == 16                                           # 4 rounds by default
    assert any("16 spectra of 10 s" in n for n in r["notes"])


@pytest.mark.parametrize("params,needle", [
    ({"center_hz": 8e9}, "the pump (8.05 GHz) is inside the span"),
    ({"bias_on_v": 0.3}, "hot-wire bias outside the limit"),
    ({"bias_on_v": 0}, "same as off"),
    ({"rounds": 0}, "rounds and spectra per state must be >= 1"),
    ({"span_hz": 0}, "span must be > 0"),
])
def test_tn_plan_errors(client, params, needle):
    assert any(needle in e for e in tn_plan(client, **params)["errors"])


def test_tn_plan_warnings(client):
    w = tn_plan(client, rbw_hz=1e6, vbw_hz=1e3)["warnings"]
    assert any("use RBW <= 100 kHz" in x for x in w) and any("VBW filter averages" in x for x in w)
    assert any("within 50 MHz of the span" in x for x in tn_plan(client, center_hz=7.53e9)["warnings"])


def test_tn_run_records_states_and_restores(client, cfg):
    client.post("/api/spa", json={"detector": "POS", "span_hz": 5e6})
    client.post("/api/rf_synth", json={"pump_enable": False, "pump_frequency_hz": 9e9, "pump_power_dbm": -10})
    client.post("/api/noise_source_bias", json={"function": "DC", "offset_v": 0.05, "output": False})
    r = client.post(f"{TN_URL}/start", json=TN_SMALL)
    assert r.status_code == 200, r.text
    job = wait_job(client)
    assert job["status"] == "done", job
    assert job["points_done"] == job["points_total"] == 8
    full = client.get(f"/api/jobs/{job['id']}?since=0").json()
    ax = full["axis"]
    assert ax["plot"] == "spectra" and len(ax["f"]) == 401
    assert set(ax["spectra"]) == {"pump off, hot wire 0 V", "pump off, hot wire on", "pump on, hot wire 0 V",
                                  "pump on, hot wire on"}
    rec = json.loads(next(Path(cfg["data_dir"]).rglob("*twpa-noise-spectrum.json")).read_text())
    assert rec["status"] == "done" and rec["spa_setup"]["detector"] == "AVER"
    assert rec["spa_setup"]["average_type"].startswith("POW") and rec["spa_setup"]["span_hz"] == 100e6
    assert rec["pump"] == ["off", "off", "on", "on", "on", "on", "off", "off"]
    assert rec["hot_wire_v"] == [0, 0.1, 0.1, 0, 0, 0.1, 0.1, 0]
    assert np.array(rec["traces_w"]).shape == (8, 401) and len(rec["power_w"]) == 4
    # the sim hot wire raises the noise, the sim TWPA amplifies it
    assert np.nanmedian(np.array(rec["y_pump_off"], dtype=float)) > 1
    assert np.nanmedian(np.array(rec["gain_db"], dtype=float)) > 3
    assert rec["pump_state"]["pump_enable"] and rec["pump_state"]["pump_frequency_hz"] == 8.5e9
    assert rec["finish"][0] == "noise source restored"
    assert client.get("/api/rf_synth/state").json()["pump_enable"] is False             # restored
    assert client.get("/api/rf_synth/state").json()["pump_frequency_hz"] == 9e9
    ns = client.get("/api/noise_source_bias/state").json()
    assert ns["output"] is False and ns["offset_v"] == 0.05
    sa = client.get("/api/spa/state").json()
    assert sa["detector"] == "POS" and sa["span_hz"] == 5e6
    html = client.get(job["html_url"])                                  # the interactive export
    assert html.status_code == 200 and re.search(r'"plot": ?"spectra"', html.text)


# ---------------------------------------------------------------- TWPA VNA gain
VG_URL = "/api/experiments/twpa_vna_gain"
VG_SMALL = {"rounds": 2, "averages": 2, "pump_settle_s": 0, "points": 51, "pump_frequency_hz": 8.5e9,
            "pump_power_dbm": 5}


def vg_plan(client, **params):
    r = client.post(f"{VG_URL}/plan", json=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_vg_default_plan_and_order(client):
    r = vg_plan(client)
    assert r["errors"] == [], r["errors"]
    assert [(p["round"], p["pump"]) for p in r["points"]] == [(1, "off"), (1, "on"), (2, "on"), (2, "off")]
    assert any("2 rounds x 2 pump states x 10 = 40 traces" in n for n in r["notes"])


@pytest.mark.parametrize("params,needle", [
    ({"stop_hz": 9e9}, "inside the VNA range"),
    ({"att_mode": "set", "attenuation_db": 25.1}, "not a multiple of"),
    ({"rounds": 0}, "rounds and traces per state must be >= 1"),
    ({"stop_hz": 5e9}, "stop frequency must be above start"),
])
def test_vg_plan_errors(client, params, needle):
    assert any(needle in e for e in vg_plan(client, **params)["errors"])


def test_vg_run_records_gain_and_restores(client, cfg):
    client.post("/api/rf_synth", json={"pump_enable": False, "pump_frequency_hz": 9e9, "pump_power_dbm": -10})
    client.post("/api/attenuator", json={"attenuation_db": 30})
    vna0 = client.get("/api/vna/state").json()
    r = client.post(f"{VG_URL}/start", json={**VG_SMALL, "att_mode": "set", "attenuation_db": 20})
    assert r.status_code == 200, r.text
    job = wait_job(client)
    assert job["status"] == "done", job
    assert job["points_done"] == job["points_total"] == 4
    ax = client.get(f"/api/jobs/{job['id']}?since=0").json()["axis"]
    assert ax["plot"] == "spectra" and ax["spectra_label"] == "|S21| [dB]" and len(ax["f"]) == 51
    assert set(ax["spectra"]) == {"pump off", "pump on"} and set(ax["derived"]) == {"gain_db"}
    rec = json.loads(next(Path(cfg["data_dir"]).rglob("*twpa-vna-gain.json")).read_text())
    assert rec["status"] == "done" and rec["pump"] == ["off", "on", "on", "off"]
    assert np.array(rec["s_real"]).shape == (4, 51) and len(rec["s21_on_real"]) == 51
    assert np.nanmedian(np.array(rec["gain_db"], dtype=float)) > 3          # the sim TWPA amplifies
    assert rec["attenuator_state"]["attenuation_db"] == 20
    assert rec["pump_state"]["pump_enable"] and rec["pump_state"]["pump_frequency_hz"] == 8.5e9
    synth = client.get("/api/rf_synth/state").json()
    assert synth["pump_enable"] is False and synth["pump_frequency_hz"] == 9e9            # restored
    assert client.get("/api/attenuator/state").json()["attenuation_db"] == 30
    assert client.get("/api/vna/state").json()["start_hz"] == vna0["start_hz"]
    html = client.get(job["html_url"])
    assert html.status_code == 200 and re.search(r'"plot": ?"spectra"', html.text)
