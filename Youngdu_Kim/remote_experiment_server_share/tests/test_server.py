import copy

import pytest
from fastapi.testclient import TestClient

from labserver.config import load_config
from labserver.server import create_app
from labserver.sim import SimWorld


@pytest.fixture
def world():
    return SimWorld(time_scale=0)


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    for icfg in c["instruments"].values():
        icfg.update(retry_delay_s=0, settle_after_read_s=0, ramp_delay_s=0)
    c["settings_file"] = str(tmp_path / "settings.local.yaml")      # not the real one
    return c


@pytest.fixture
def client(cfg, world):
    with TestClient(create_app(cfg, sim=True, world=world)) as c:
        yield c


def test_status(client):
    r = client.get("/api/status").json()
    assert r["sim"] is True and r["turning_off"] is False
    assert all(s["connected"] for s in r["instruments"].values())


def test_instruments_lists_settable_params(client):
    r = client.get("/api/instruments").json()
    assert "pump_power_dbm" in r["rf_synth"]["settable"]
    assert r["qubit_flux_bias"]["limits"]["level"] == [-1.0, 1.0]
    assert "receiver" in r["warm_switches"]["switches"]


def test_apply_and_state(client, world):
    r = client.post("/api/warm_switches", json={"receiver": "vna"})
    assert r.status_code == 200 and r.json()["receiver"] == "vna"
    assert world.switch_ports["B"] == 1
    assert client.get("/api/warm_switches/state").json()["receiver"] == "vna"


@pytest.mark.parametrize("path,body,status,code", [
    ("/api/qubit_flux_bias", {"level": 5}, 400, "instrument_error"),          # limit
    ("/api/spa", {"frequency": 1}, 400, "instrument_error"),                  # unknown param
    ("/api/nonexistent", {"x": 1}, 503, "unavailable"),
])
def test_errors_map_to_http(client, path, body, status, code):
    r = client.post(path, json=body)
    assert r.status_code == status and r.json()["error"] == code


def test_raw_scpi(client):
    r = client.post("/api/spa/scpi", json={"command": "*IDN?"}).json()
    assert "E7405A" in r["response"]
    assert client.post("/api/spa/scpi", json={"command": ":FREQ:CENT 5e9"}).json()["response"] is None
    assert client.get("/api/spa/state").json()["center_hz"] == 5e9


def test_raw_scpi_unsupported_for_windfreak(client):
    assert client.post("/api/rf_synth/scpi", json={"command": "x"}).status_code == 400


def test_outputs_off_keeps_the_server_running(client, world):
    client.post("/api/vna", json={"output": True})
    assert world.sources["vna"]["on"]
    r = client.post("/api/outputs_off").json()
    assert r["results"]["vna"] == "off" and r["turning_off"] is False   # no job running: done at once
    assert not world.sources["vna"]["on"]
    assert client.get("/api/status").json()["instruments"]["vna"]["connected"]
    assert client.post("/api/vna", json={"output": True}).status_code == 200


def test_reconnect(client):
    r = client.post("/api/spa/reconnect").json()
    assert r["connected"] and "E7405A" in r["idn"]


def test_shutdown_keeps_outputs(client, world):
    client.post("/api/vna", json={"output": True})
    client.post("/api/shutdown")
    assert world.sources["vna"]["on"]


def test_index_serves_the_ui(client):
    r = client.get("/")
    assert r.status_code == 200 and "/ws/live/" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_sa_align_runs_in_the_background_and_reserves_the_sa(cfg, world):
    import time
    cfg["instruments"]["spa"]["align_poll_s"] = 0.3
    with TestClient(create_app(cfg, sim=True, world=world)) as client:
        r = client.post("/api/spa/align")
        assert r.status_code == 200 and r.json()["running"]
        assert client.get("/api/status").json()["instruments"]["spa"]["claimed_by"] == "align:spa"
        assert client.post("/api/spa/align").status_code == 409          # already aligning
        assert client.post("/api/spa", json={"center_hz": 5e9}).status_code == 409
        t0 = time.monotonic()
        while (al := client.get("/api/status").json()["align"]["spa"])["running"]:
            assert time.monotonic() - t0 < 10
            time.sleep(0.05)
        assert al["error"] is None and al["result"]["rf_skipped_no_cable"] is True
        assert client.get("/api/status").json()["instruments"]["spa"]["claimed_by"] is None
        assert client.post("/api/spa", json={"center_hz": 5e9}).status_code == 200
        assert client.post("/api/vna/align").status_code == 400            # only the SA has one
