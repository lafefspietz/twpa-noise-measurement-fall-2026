import copy
import json
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from labserver import report
from labserver.config import load_config
from labserver.server import create_app
from labserver.sim import SimWorld


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    for icfg in c["instruments"].values():
        icfg.update(retry_delay_s=0, settle_after_read_s=0, ramp_delay_s=0)
    c["data_dir"] = str(tmp_path / "data")
    c["settings_file"] = str(tmp_path / "settings.local.yaml")      # not the real one
    return c


@pytest.fixture
def client(cfg):
    with TestClient(create_app(cfg, sim=True, world=SimWorld(time_scale=0))) as c:
        yield c


def seg(start, stop, step, averages):
    return {"start_dbm": start, "stop_dbm": stop, "step_db": step, "averages": averages}


TP = {"pump_freq_start_hz": 8.4e9, "pump_freq_stop_hz": 8.5e9, "pump_freq_step_hz": 0.1e9,
      "pump_power_start_dbm": -3, "pump_power_stop_dbm": -2, "pump_power_step_db": 1, "traces": 2,
      "vbw_hz": 100, "settle_s": 0, "pump_settle_s": 0, "ref_level_dbm": 0}
RUNS = [
    ("qubit_power_sweep", {"segments": [seg(-70, -50, 10, 2)], "settle_s": 0, "points": 51}),
    ("qubit_power_sweep", {"power_control": "attenuator", "settle_s": 0, "points": 51,
                           "att_segments": [{"start_db": 40, "stop_db": 30, "step_db": 5, "averages": 1}]}),
    ("qubit_flux_sweep", {"flux_start_v": -0.3, "flux_stop_v": 0.3, "flux_step_v": 0.3, "averages": 1,
                          "settle_s": 0, "points": 51}),
    ("y_factor_measurement", {"bias_start_v": -0.1, "bias_stop_v": 0.1, "bias_step_v": 0.1, "traces": 2,
                              "passes": 2, "settle_s": 0, "sweep_time_s": 0.01}),
    ("twpa_pump_sweep", TP),
    ("twpa_signal_sweep", {"signal_freq_start_hz": 5.9e9, "signal_freq_stop_hz": 6.0e9, "signal_freq_step_hz": 100e6,
                           "noise_mode": "sweep", "noise_bias_start_v": 0, "noise_bias_stop_v": 0.2,
                           "noise_bias_step_v": 0.2, "traces": 2, "vbw_hz": 100, "settle_s": 0, "pump_settle_s": 0,
                           "noise_settle_s": 0, "ref_level_dbm": 0}),
    ("twpa_vna_gain", {"rounds": 1, "averages": 1, "pump_settle_s": 0, "points": 51, "pump_frequency_hz": 8.5e9}),
    ("twpa_signal_sweep", {"signal_axis": "power", "signal_power_start_dbm": -30, "signal_power_stop_dbm": -20,
                           "signal_power_step_db": 10, "reference": "none", "save_traces": False, "settle_s": 0,
                           "pump_settle_s": 0, "ref_level_dbm": 0}),
    ("qubit_power_sweep_sa", {"segments": [seg(-40, -10, 30, 1)], "freq_start_hz": 5.579e9, "freq_stop_hz": 5.581e9,
                              "freq_step_hz": 1e6, "settle_s": 0, "ref_level_dbm": 0}),
    ("qubit_power_sweep_sa", {"power_control": "attenuator", "synth_power_dbm": 0, "freq_start_hz": 5.58e9,
                              "freq_stop_hz": 5.58e9, "settle_s": 0, "ref_level_dbm": 0,
                              "att_segments": [{"start_db": 40, "stop_db": 30, "step_db": 5, "averages": 1}]}),
]


def run(client, exp_id, params):
    r = client.post(f"/api/experiments/{exp_id}/start", json=params)
    assert r.status_code == 200, r.text
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:
        job = client.get("/api/jobs/current").json()
        if job["status"] not in ("starting", "running"):
            assert job["status"] == "done", job
            return client.get(f"/api/jobs/{job['id']}?since=0").json()
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def page_data(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="resultData">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


@pytest.mark.parametrize("exp_id,params", RUNS, ids=[f"{e}-{i}" for i, (e, _) in enumerate(RUNS)])
def test_html_shows_what_the_run_showed(client, cfg, exp_id, params):
    job = run(client, exp_id, params)
    assert job["html_url"] == job["url"][:-len(".json")] + ".html"
    r = client.get(job["html_url"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert 'attachment; filename="' + job["html_url"].split("/")[-1] in r.headers["content-disposition"]
    html = r.text
    data = page_data(html)
    # the same plot and rows as the experiments tab got while it ran, plus each point's record values
    assert data["axis"] == json.loads(json.dumps(job["axis"]))
    assert len(data["rows"]) == len(job["rows"]) > 0
    for i, (mine, live) in enumerate(zip(data["rows"], job["rows"])):
        assert mine.pop("point_index") == i
        point = mine.pop("point")
        assert mine == live
        assert "point_start_time" in point and "point_end_time" in point
    rec = json.loads(next(Path(cfg["data_dir"]).rglob(job["file"])).read_text())
    # every instrument's state at the start, and the settings of the run
    for name, st in rec["instruments"].items():
        assert f"<legend>{name}" in html
    assert "(used by this experiment)" in html
    for key in rec["params"]:
        assert f'<td class="key">{key}</td>' in html
    assert "function drawResult()" in html and "/api/" not in html.split("<script>")[1]


def test_twpa_html_without_saved_spectra_keeps_the_reference(client, cfg):
    job = run(client, "twpa_pump_sweep", {**TP, "save_traces": False})
    data = page_data(client.get(job["html_url"]).text)
    assert data["axis"]["ref"]["trace_dbm"] == job["axis"]["ref"]["trace_dbm"]
    assert all("trace_dbm" not in row for row in data["rows"])
    assert data["rows"][0]["point"]["gain_db"] == job["rows"][0]["gain_db"]


def test_html_only_for_experiment_records(client):
    r = client.post("/api/live/spa/save", json={"label": "t"})
    if r.status_code == 200:            # a live trace is not an experiment record
        url = r.json()["url"]
        assert client.get(url).status_code == 200
        assert client.get(url[:-5] + ".html").status_code == 404
    assert client.get("/api/data/01-01-2000/nope.html").status_code == 404
    assert client.get("/api/data/01-01-2000/nope.txt").status_code == 404


def test_static_files_and_index_use_the_shared_plots(client):
    for name in ("results.js", "results.css"):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert client.get("/static/nope.js").status_code == 404
    assert client.get("/static/..%2Fserver.py").status_code == 404
    index = client.get("/").text
    assert '<script src="/static/results.js">' in index and "function drawMap" not in index


def test_command_line_writes_html_next_to_the_record(client, cfg, tmp_path, monkeypatch, capsys):
    job = run(client, *RUNS[0])
    src = next(Path(cfg["data_dir"]).rglob(job["file"]))
    monkeypatch.setattr(sys, "argv", ["report", str(src)])
    report.main()
    out = src.with_suffix(".html")
    assert out.is_file() and page_data(out.read_text(encoding="utf-8"))["rows"]
    assert str(out) in capsys.readouterr().out


def test_value_text():
    assert report.value_text("center_hz", 5.58e9) == "5580000000  (5.58 GHz)"
    assert report.value_text("noise_w", 1e-3) == "0.001  (0.000 dBm)"
    assert report.value_text("output", True) == "true" and report.value_text("x", None) == "none"
    assert report.value_text("s_real", [[1.0] * 3] * 20) == "[20 x 3 values: in the JSON file]"
