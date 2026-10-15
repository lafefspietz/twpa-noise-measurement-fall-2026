import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

from labserver.config import load_config
from labserver.live import LiveHub
from labserver.manager import InstrumentManager
from labserver.server import create_app
from labserver.sim import SimWorld


def wait_for(cond, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    for icfg in c["instruments"].values():
        icfg.update(retry_delay_s=0, settle_after_read_s=0, ramp_delay_s=0)
    c["data_dir"] = str(tmp_path / "data")
    c["settings_file"] = str(tmp_path / "settings.local.yaml")      # not the real one
    c["live"] = {"spa": {"max_fps": 100}, "vna": {"max_fps": 100}}
    return c


@pytest.fixture
def world():
    # small but nonzero sweep times, so reads interleave like on hardware
    return SimWorld(time_scale=0.1)


@pytest.fixture
def mgr(cfg, world):
    m = InstrumentManager(cfg, sim=True, world=world)
    m.connect_all()
    yield m
    m.shutdown()


@pytest.fixture
def hub(mgr, cfg):
    h = LiveHub(mgr, cfg["live"])
    yield h
    h.stop_all()


def test_only_trace_instruments_get_live_views(hub):
    assert set(hub.views) == {"spa", "vna"}


def test_frames_flow_while_subscribed(hub):
    spa = hub.views["spa"]
    spa.subscribe()
    assert wait_for(lambda: spa.seq >= 5)
    f = spa.frame
    assert f["type"] == "frame" and len(f["x"]) == len(f["y"]) == 401
    assert spa.info()["status"] == "running" and spa.info()["fps"] > 0


def test_vna_frames_are_db_and_phase(hub):
    vna = hub.views["vna"]
    vna.subscribe()
    assert wait_for(lambda: vna.frame is not None)
    assert len(vna.frame["y_db"]) == len(vna.frame["y_deg"]) == 401


def test_stops_and_hands_front_panel_back_when_nobody_watches(hub, mgr):
    spa = hub.views["spa"]
    spa.subscribe()
    assert wait_for(lambda: spa.seq >= 2)
    assert mgr.slots["spa"].inst.t.s["cont"] is False     # single sweep while reading
    spa.unsubscribe()
    assert wait_for(lambda: mgr.slots["spa"].inst.t.s["cont"] is True)
    assert wait_for(lambda: spa._thread is None, timeout=5)
    seq = spa.seq
    time.sleep(0.2)
    assert spa.seq == seq


def test_api_calls_are_not_starved(hub, mgr):
    hub.views["spa"].subscribe()
    assert wait_for(lambda: hub.views["spa"].seq >= 2)
    t0 = time.monotonic()
    for _ in range(10):
        mgr.apply("spa", ref_level=-10)
    assert time.monotonic() - t0 < 2.0


def test_pauses_while_a_job_claims_the_instrument(hub, mgr):
    spa = hub.views["spa"]
    spa.subscribe()
    assert wait_for(lambda: spa.seq >= 2)
    with mgr.claim(["spa"], owner="job:test"):
        assert wait_for(lambda: spa.status == "paused: in use by job:test")
        seq = spa.seq
        with mgr.use("spa", owner="job:test") as inst:     # the job can use it
            inst.get_state()
        time.sleep(0.2)
        assert spa.seq == seq
    assert wait_for(lambda: spa.seq > seq)


def test_pauses_while_outputs_are_turned_off(hub, mgr):
    spa = hub.views["spa"]
    spa.subscribe()
    assert wait_for(lambda: spa.seq >= 2)
    mgr.outputs_off()
    assert wait_for(lambda: spa.status == "paused: turning outputs off")
    seq = spa.seq
    time.sleep(0.2)
    assert spa.seq == seq
    mgr.end_halt()
    assert wait_for(lambda: spa.seq > seq)


def test_hold_freezes_last_frame(hub):
    spa = hub.views["spa"]
    spa.subscribe()
    assert wait_for(lambda: spa.seq >= 2)
    spa.set_hold(True)
    assert wait_for(lambda: spa.status == "hold")
    frame = spa.frame
    time.sleep(0.2)
    assert spa.frame is frame
    spa.set_hold(False)
    assert wait_for(lambda: spa.frame is not frame)


def test_read_errors_are_reported_and_recovered(hub, world):
    spa = hub.views["spa"]
    spa.subscribe()
    world.fail_rate = 1.0
    hub.views["spa"].mgr.slots["spa"].inst.max_retries = 1
    assert wait_for(lambda: spa.status.startswith("error"))
    world.fail_rate = 0.0
    assert wait_for(lambda: spa.status == "running", timeout=4)


# ---- through the server ----
@pytest.fixture
def client(cfg, world):
    with TestClient(create_app(cfg, sim=True, world=world)) as c:
        yield c


def test_websocket_streams_frames_and_status(client):
    with client.websocket_connect("/ws/live/spa") as ws:
        kinds = set()
        for _ in range(20):
            msg = ws.receive_json()
            kinds.add(msg["type"])
            if kinds == {"frame", "status"}:
                break
    assert kinds == {"frame", "status"}


def test_websocket_unknown_instrument(client):
    with client.websocket_connect("/ws/live/attenuator") as ws:
        assert ws.receive_json()["type"] == "error"


def test_save_live_trace_with_snapshot(client, cfg):
    with client.websocket_connect("/ws/live/vna") as ws:
        while ws.receive_json()["type"] != "frame":
            pass
        r = client.post("/api/live/vna/save", json={"label": "vna check", "note": "sim test"})
    assert r.status_code == 200, r.text
    files = list((__import__("pathlib").Path(cfg["data_dir"])).rglob("*.json"))
    assert len(files) == 1 and files[0].name.endswith("-vna-check.json")
    rec = json.loads(files[0].read_text())
    assert {"timestamp", "date", "time", "timezone"} <= rec.keys()
    assert rec["note"] == "sim test" and len(rec["trace"]["y_real"]) == 401
    assert rec["instruments"]["warm_switches"]["receiver"] == "spa"
    assert r.json()["instruments"] == rec["instruments"]     # the UI refreshes its panels from this
    assert (files[0].parent / "README.md").exists()
    d = client.get(r.json()["url"])
    assert d.status_code == 200 and d.content == files[0].read_bytes()
    assert "attachment" in d.headers["content-disposition"]


def test_slug():
    from labserver.storage import slug
    assert slug("sampleA/run 1") == "sampleA-run-1"
    assert slug("../escape/x") == "escape-x"
    assert slug("큐빗") == "record"


def test_download_stays_inside_data_dir(client, cfg):
    secret = __import__("pathlib").Path(cfg["data_dir"]).parent / "secret.json"
    secret.write_text("{}")
    assert client.get("/api/data/../secret.json").status_code != 200     # normalized away before routing
    assert client.get("/api/data/%2E%2E/secret.json").status_code == 404
    assert client.get("/api/data/..%2F/secret.json").status_code == 404
    assert client.get("/api/data/01-01-2026/missing.json").status_code == 404


def test_save_without_trace_is_an_error(client):
    assert client.post("/api/live/spa/save", json={}).status_code == 502


def test_hold_endpoint(client):
    assert client.post("/api/live/spa/hold", json={"hold": True}).json()["hold"] is True


def test_refuses_sweeps_that_would_lock_the_instrument(hub, mgr):
    spa = hub.views["spa"]
    spa.subscribe()
    assert wait_for(lambda: spa.seq >= 1)
    mgr.apply("spa", rbw_hz=1e3)                      # full span at 1 kHz RBW: hours
    assert wait_for(lambda: spa.status.startswith("paused: sweep time"))
    t0 = time.monotonic()
    mgr.apply("spa", span_hz=1e6, rbw_hz=100e3)       # the fix goes through right away
    assert time.monotonic() - t0 < 1.0
    seq = spa.seq
    assert wait_for(lambda: spa.status == "running" and spa.seq > seq)


def test_live_reads_use_few_retries(hub, mgr):
    """A dead instrument must not be held for the driver's 50 retries x timeout."""
    spa = hub.views["spa"]
    inst = mgr.slots["spa"].inst
    inst.max_retries = 50
    attempts = []

    def dead():
        attempts.append(1)
        raise __import__("labserver.transport", fromlist=["TransportError"]).TransportError("no answer")
    inst._single_sweep = dead
    spa.subscribe()
    assert wait_for(lambda: spa.status.startswith("error"))
    assert len(attempts) <= spa.max_retries
    t0 = time.monotonic()
    mgr.apply("spa", ref_level=-10)                   # the instrument is free between attempts
    assert time.monotonic() - t0 < 2.0


def test_averaging_counts_toward_live_limit(hub, mgr):
    spa = hub.views["spa"]
    spa.subscribe()
    assert wait_for(lambda: spa.seq >= 1)
    spa.state_refresh_s = 0                           # re-read settings before every read
    with mgr.use("spa") as inst:                      # front panel: 8000 averages
        inst.t.write(":AVER:COUN 8000")
        inst.t.write(":AVER ON")
    assert wait_for(lambda: spa.status.startswith("paused: sweep time"))
    with mgr.use("spa") as inst:
        inst.t.write(":AVER OFF")
    assert wait_for(lambda: spa.status == "running")


def test_front_panel_changes_reach_the_x_axis(hub, mgr):
    spa = hub.views["spa"]
    spa.state_refresh_s = 0.2
    spa.subscribe()
    assert wait_for(lambda: spa.seq >= 1)
    mgr.slots["spa"].inst.t.s.update(center=3e9, span=1e9)   # behind the driver's back
    assert wait_for(lambda: spa.frame["meta"]["center_hz"] == 3e9)
    assert spa.frame["x"][0] == pytest.approx(2.5e9)


def test_trace_for_keeps_recent_frames(hub):
    spa = hub.views["spa"]
    spa.subscribe()
    assert wait_for(lambda: spa.seq >= 3)
    seq = spa.seq
    assert spa.trace_for(seq) is not None
    assert spa.trace_for(-1) is None


def test_save_stores_the_frame_on_screen(client, cfg):
    with client.websocket_connect("/ws/live/spa") as ws:
        while (shown := ws.receive_json())["type"] != "frame":
            pass
        live = client.app.state.live.views["spa"]
        assert wait_for(lambda: live.seq > shown["seq"])   # newer frames exist now
        r = client.post("/api/live/spa/save", json={"label": "on screen", "seq": shown["seq"]})
        gone = client.post("/api/live/spa/save", json={"seq": shown["seq"] - 1000})
    assert r.status_code == 200, r.text
    rec = json.loads(client.get(r.json()["url"]).content)
    assert rec["seq"] == shown["seq"] and rec["trace"]["y"] == shown["y"]
    assert gone.status_code == 502 and "no longer kept" in gone.json()["detail"]


def test_raw_scpi_drops_cached_settings(client):
    client.get("/api/spa/state")                      # fills the cache
    client.post("/api/spa/scpi", json={"command": ":FREQ:CENT 4e9"})
    inst = client.app.state.mgr.slots["spa"].inst
    assert inst._get_axis()["center_hz"] == 4e9
