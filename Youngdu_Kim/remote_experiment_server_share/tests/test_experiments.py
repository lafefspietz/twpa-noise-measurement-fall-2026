import copy
import json
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
    assert [e["id"] for e in exps] == ["qubit_flux_sweep", "qubit_power_sweep", "y_factor_measurement"]
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
            "settle_s": 0, "sweep_time_s": 0.01}
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
    assert set(d["instruments"]) == {"spa", "noise_source_bias", "warm_switches", "rf_synth", "qubit_flux_bias"}
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
    ({"sweep_time_s": 0}, "zero span has no auto sweep time"),
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
    client.post("/api/rf_synth", json={"probe_enable": True})
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
    assert client.get("/api/rf_synth/state").json()["probe_enable"] is True


def test_yf_run_saves_power_vs_bias_and_restores(client, cfg):
    client.post("/api/noise_source_bias", json={"function": "SIN", "offset_v": 0.05, "output": False})
    client.post("/api/spa", json={"span_hz": 1e9, "center_hz": 6e9, "unit": "DBM", "ref_level": -20,
                                  "scale": "LIN"})
    client.post("/api/warm_switches", json={"fridge_in": "vna", "receiver": "vna", "rx_source": "fridge"})
    # a probe tone at the measurement frequency: it must be off during the sweep
    client.post("/api/rf_synth", json={"probe_enable": True, "probe_frequency_hz": 5.58e9, "probe_power_dbm": -20})
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
    assert rec["finish"] == ["noise source restored", "SA restored", "switches restored", "SynthHD probe back on"]

    ns = client.get("/api/noise_source_bias/state").json()
    assert ns["output"] is False and ns["function"] == "SIN" and ns["offset_v"] == 0.05
    sa = client.get("/api/spa/state").json()
    assert sa["span_hz"] == 1e9 and sa["center_hz"] == 6e9 and sa["unit"] == "DBM" and sa["ref_level"] == -20
    assert sa["scale"] == "LIN"
    assert sa["continuous"] is True and sa["sweep_time_auto"] is True   # auto again once the span is back
    sw = client.get("/api/warm_switches/state").json()
    assert sw["receiver"] == "vna" and sw["fridge_in"] == "vna"
    assert client.get("/api/rf_synth/state").json()["probe_enable"] is True


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
                       ("rf_synth", {"probe_enable": True})):
        assert client.post(f"/api/{name}", json=body).status_code == 409     # reserved
    client.post(f"/api/jobs/{job['id']}/stop")
    assert wait_job(client)["status"] == "stopped"
    rec = yf_record(cfg)
    assert rec["status"] == "stopped" and 1 <= len(rec["power_w"]) < 10
    assert client.get("/api/noise_source_bias/state").json()["output"] is False


def test_yf_default_sweep_time_from_rbw_vbw(client, cfg):
    from labserver.experiments.y_factor import default_sweep_time
    assert default_sweep_time(3e6, 1e3) == 0.13          # 400 / (pi 1 kHz) = 0.127 s
    assert default_sweep_time(1e3, 3e6) == 0.13          # the narrower filter sets it
    assert default_sweep_time(3e6, 3e6) == 0.01          # floor
    r = yf_plan(client)                                   # empty field: RBW 3 MHz, VBW 1 kHz
    assert r["derived"] == {"sweep_time_s": 0.13}
    assert any("sweep time 0.13 s per trace, from RBW" in n for n in r["notes"])
    r = yf_plan(client, vbw_hz=10)
    assert r["derived"]["sweep_time_s"] == 13 and any("each trace takes 13 s" in w for w in r["warnings"])
    assert yf_plan(client, sweep_time_s=0.5)["derived"] == {}            # typed: used as is
    job = yf_run(client, {**YF_SMALL, "sweep_time_s": None, "passes": 1, "traces": 1})
    assert job["status"] == "done", job
    assert yf_record(cfg)["spa_setup"]["sweep_time_s"] == 0.13


def test_yf_uses_the_rbw_vbw_the_sa_took(client, cfg):
    """The ESA snaps RBW/VBW to 1-3-10 steps (the sim too): 2 MHz -> 3 MHz, 2 kHz -> 3 kHz.
    The default sweep time comes from the values it took, not from the typed ones."""
    r = yf_plan(client, rbw_hz=2e6, vbw_hz=2e3)
    assert r["derived"]["sweep_time_s"] == 0.064                  # estimate from the typed values
    job = yf_run(client, {**YF_SMALL, "rbw_hz": 2e6, "vbw_hz": 2e3, "sweep_time_s": None,
                          "passes": 1, "traces": 1})
    assert job["status"] == "done", job
    assert any("the SA took RBW 3000 kHz (asked for 2000 kHz)" in n for n in job["notes"])
    assert any("the SA took VBW 3 kHz (asked for 2 kHz)" in n for n in job["notes"])
    rec = yf_record(cfg)
    assert rec["rbw_hz"] == 3e6 and rec["vbw_hz"] == 3e3
    assert rec["sweep_time_s"] == 0.042                           # 400 / (pi 3 kHz)
    assert rec["params"]["rbw_hz"] == 2e6                         # what was typed stays in params
    full = client.get(f"/api/jobs/{job['id']}?since=0").json()
    assert "RBW 3000 kHz, VBW 3 kHz, sweep 0.042 s" in full["axis"]["title"]



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
    # sim TWPA: 3 dB/dB above -3 dBm, so -1 dBm gives 6 dB on the chain noise at 0 V
    assert rec["power_w"][2] == pytest.approx(CHAIN_W * 10 ** 0.6, rel=0.05)
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
