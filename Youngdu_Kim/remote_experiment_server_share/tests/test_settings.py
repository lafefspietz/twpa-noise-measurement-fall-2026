"""Settings tab: limits with open ends, loosening needs a confirmation, settings.local.yaml,
runtime apply, the SA reference level limit and the GS210 current-mode guard."""
import copy

import pytest
import yaml
from fastapi.testclient import TestClient

from labserver.config import load_config
from labserver.drivers import E7405A, GS210, InstrumentError
from labserver.server import create_app
from labserver.settings import ConfirmNeeded, Settings, SettingsError
from labserver.sim import SimWorld

VNA_POWER = "instruments.vna.limits.power_dbm"


@pytest.fixture
def world():
    return SimWorld(time_scale=0)


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    for icfg in c["instruments"].values():
        icfg.update(retry_delay_s=0, settle_after_read_s=0, ramp_delay_s=0)
    c["data_dir"] = str(tmp_path / "data")
    c["settings_file"] = str(tmp_path / "settings.local.yaml")
    return c


@pytest.fixture
def client(cfg, world):
    with TestClient(create_app(cfg, sim=True, world=world)) as c:
        yield c


def saved(cfg) -> dict:
    with open(cfg["settings_file"], encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---- Settings on its own ----
def test_tightening_applies_and_is_saved(cfg):
    s = Settings(cfg)
    assert s.update({VNA_POWER: [-80, -20]}) == [VNA_POWER]
    assert cfg["instruments"]["vna"]["limits"]["power_dbm"] == [-80, -20]
    assert saved(cfg)["instruments"]["vna"]["limits"]["power_dbm"] == [-80, -20]


@pytest.mark.parametrize("new", [[-80, 5], [-90, 0], [None, 0], [-80, None], None])
def test_loosening_needs_confirm(cfg, new):
    s = Settings(cfg)
    with pytest.raises(ConfirmNeeded, match="vna power"):
        s.update({VNA_POWER: new})
    assert cfg["instruments"]["vna"]["limits"]["power_dbm"] == [-80.0, 0.0]      # nothing changed
    s.update({VNA_POWER: new}, confirm=True)
    assert cfg["instruments"]["vna"]["limits"]["power_dbm"] == (new or [None, None])


def test_all_or_nothing(cfg):
    s = Settings(cfg)
    with pytest.raises(SettingsError):
        s.update({VNA_POWER: [-80, -20], "live.max_retries": "many"})
    assert cfg["instruments"]["vna"]["limits"]["power_dbm"] == [-80.0, 0.0]


@pytest.mark.parametrize("sid,value", [
    (VNA_POWER, [0, -10]),                  # min above max
    (VNA_POWER, [1, 2, 3]),
    ("live.max_retries", 0),                # below the minimum
    ("live.max_retries", 1.5),              # not a whole number
    ("instruments.qubit_flux_bias.allow_current_mode", "yes"),
    ("instruments.nope.limits.x", [0, 1]),
])
def test_invalid_values_refused(cfg, sid, value):
    with pytest.raises(SettingsError):
        Settings(cfg).update({sid: value}, confirm=True)


def test_back_to_config_drops_the_override(cfg):
    s = Settings(cfg)
    s.update({VNA_POWER: [-80, -20], "live.max_sweep_s": 2})
    s.update({VNA_POWER: [-80, 0]}, confirm=True)       # what config.yaml says
    assert "instruments" not in saved(cfg) and saved(cfg)["live"] == {"max_sweep_s": 2}
    s.update(reset=["live.max_sweep_s"])
    assert saved(cfg) == {} and cfg["live"]["max_sweep_s"] == s.config_value("live.max_sweep_s")


def test_limit_config_yaml_lacks_goes_away_again(cfg):
    s = Settings(cfg)
    sid = "instruments.spa.limits.ref_level_dbm"
    s.update({sid: [None, 0]})
    assert cfg["instruments"]["spa"]["limits"]["ref_level_dbm"] == [None, 0]
    s.update({sid: None}, confirm=True)
    assert "ref_level_dbm" not in cfg["instruments"]["spa"]["limits"] and saved(cfg) == {}


def test_file_applied_at_start(cfg, tmp_path):
    (tmp_path / "settings.local.yaml").write_text(yaml.safe_dump({
        "instruments": {"vna": {"limits": {"power_dbm": [-70, -10]}, "max_retries": "bad"}},
        "live": {"spa": {"max_fps": 3}},
        "experiment_defaults": {"qubit_flux_sweep": {"averages": 4}}}))
    s = Settings(cfg)
    assert cfg["instruments"]["vna"]["limits"]["power_dbm"] == [-70, -10]
    assert "max_retries" not in cfg["instruments"]["vna"]               # invalid: ignored
    assert cfg["live"]["spa"]["max_fps"] == 3
    assert cfg["experiment_defaults"] == {"qubit_flux_sweep": {"averages": 4}}
    assert s.config_value(VNA_POWER) == [-80.0, 0.0]                    # config.yaml stays the base


def test_broken_file_is_moved_aside(cfg, tmp_path):
    (tmp_path / "settings.local.yaml").write_text("live: [unclosed")
    Settings(cfg)
    assert not (tmp_path / "settings.local.yaml").exists()
    assert list(tmp_path.glob("settings.local.yaml.broken-*"))


def test_schema_lists_driver_limits_and_tunables(cfg):
    ids = set(Settings(cfg).settings)
    for sid in ("instruments.spa.limits.ref_level_dbm", "instruments.spa.limits.attenuation_db",
                "instruments.vna.limits.ifbw_hz", "instruments.rf_synth.limits.pump_frequency_hz",
                "instruments.qubit_flux_bias.limits.level_a", "instruments.qubit_flux_bias.allow_current_mode",
                "instruments.spa.max_retries", "instruments.vna.timeout_ms", "instruments.qubit_flux_bias.ramp_step",
                "live.vna.max_fps", "live.max_average", "experiments.max_points", "off_lock_timeout_s"):
        assert sid in ids, sid
    assert not any(sid.startswith("instruments.rf_synth.timeout") for sid in ids)     # serial: no timeout


# ---- open-ended limits in the drivers ----
def test_open_limit_end_is_no_bound(world):
    spa = E7405A.open("spa", {"limits": {"center_hz": [1e9, None]}, "retry_delay_s": 0}, sim=True, world=world)
    spa.apply(center_hz=12e9)
    with pytest.raises(InstrumentError, match=r"outside limit \[1e\+09, inf\]"):
        spa.apply(center_hz=0.5e9)


# ---- SA reference level, limited in dBm whatever the unit ----
@pytest.fixture
def spa(world):
    return E7405A.open("spa", {"limits": {"ref_level_dbm": [None, -20]}, "retry_delay_s": 0}, sim=True, world=world)


def test_ref_level_limit_in_dbm(spa):
    spa.apply(ref_level=-30)
    with pytest.raises(InstrumentError, match="ref_level=-10 DBM"):
        spa.apply(ref_level=-10)


def test_ref_level_limit_converts_w(spa):
    spa.apply(unit="W")
    spa.apply(ref_level=2e-8)                   # -47 dBm
    with pytest.raises(InstrumentError, match=r"\(0.00 dBm\)"):
        spa.apply(ref_level=1e-3)


def test_ref_level_uses_the_unit_of_the_same_apply(spa):
    s = spa.apply(ref_level=2e-8, unit="W")     # the unit goes first, so 2e-8 is W, not dBm
    assert s["unit"] == "W" and s["ref_level"] == pytest.approx(2e-8)
    with pytest.raises(InstrumentError, match="outside limit"):
        spa.apply(ref_level=-10, unit="DBM")


# ---- GS210 current mode ----
@pytest.fixture
def gs(world):
    return GS210.open("qubit_flux_bias", {"limits": {"level": [-1, 1], "level_a": [-1e-3, 1e-3]},
                                          "ramp_delay_s": 0}, sim=True, world=world)


def test_gs210_refuses_current_mode_by_default(gs):
    with pytest.raises(InstrumentError, match="current mode is off"):
        gs.apply(function="CURR")
    assert gs.get_state()["function"].startswith("VOLT")


def test_gs210_left_in_current_mode_refuses_level_and_output(gs):
    gs.t.write(":SOUR:FUNC CURR")          # e.g. the front panel
    with pytest.raises(InstrumentError, match="in current mode"):
        gs.apply(level=0.5)
    with pytest.raises(InstrumentError, match="in current mode"):
        gs.apply(output=True)
    gs.apply(function="VOLT", level=0.5, output=True)       # back to VOLT in the same apply is fine
    assert gs.get_state()["output"] is True


def test_gs210_current_mode_when_allowed_uses_the_amp_limit(gs):
    gs.cfg["allow_current_mode"] = True
    gs.reconfigure()
    gs.apply(function="CURR", range=1e-3, level=0.5e-3)
    with pytest.raises(InstrumentError, match="CURR mode"):
        gs.apply(level=0.5)                # 0.5 A: fine as volts, not as amps


# ---- API ----
def test_settings_api_confirm_flow(client):
    r = client.post("/api/settings", json={"changes": {VNA_POWER: [-80, 5]}})
    assert r.status_code == 409 and r.json()["error"] == "confirm"
    assert "max 0 dBm -> 5 dBm" in r.json()["loosened"][0]
    assert client.post("/api/vna", json={"power_dbm": 3}).status_code == 400
    r = client.post("/api/settings", json={"changes": {VNA_POWER: [-80, 5]}, "confirm": True})
    assert r.status_code == 200 and r.json()["changed"] == [VNA_POWER]
    assert client.post("/api/vna", json={"power_dbm": 3}).json()["power_dbm"] == 3
    assert client.get("/api/instruments").json()["vna"]["limits"]["power_dbm"] == [-80, 5]


def test_settings_api_rejects_bad_values(client):
    r = client.post("/api/settings", json={"changes": {"live.max_fps": 3}})
    assert r.status_code == 400 and r.json()["error"] == "invalid_settings"


def test_settings_reach_running_objects(client):
    app = client.app
    client.post("/api/settings", json={"changes": {
        "live.spa.max_fps": 3, "live.max_average": 50, "instruments.spa.max_retries": 7,
        "instruments.vna.timeout_ms": 20000, "off_lock_timeout_s": 1.5}})
    view = app.state.live.views["spa"]
    assert (view.max_fps, view.max_average) == (3, 50)
    assert client.post("/api/live/spa/average", json={"count": 51}).status_code == 400
    mgr = app.state.mgr
    assert mgr.slots["spa"].inst.max_retries == 7
    assert mgr.slots["vna"].inst.t.timeout_ms == 20000
    assert mgr.off_lock_timeout_s == 1.5


def test_experiment_caps_and_limits_in_plan(client):
    client.post("/api/settings", json={"changes": {"experiments.max_points": 10,
                                                   "instruments.vna.limits.ifbw_hz": [None, 100]}})
    p = client.post("/api/experiments/qubit_flux_sweep/plan", json={}).json()
    assert any("more than 10 flux points" in e for e in p["errors"])
    assert any("IFBW outside the limit [-inf, 100] Hz: 1000" in e for e in p["errors"])


def test_y_factor_plan_checks_the_sa_ref_level_limit(client):
    client.post("/api/settings", json={"changes": {"instruments.spa.limits.ref_level_dbm": [None, -50]}})
    p = client.post("/api/experiments/y_factor_measurement/plan", json={}).json()
    assert any("SA reference level outside the limit [-inf, -50] dBm: -45" in e for e in p["errors"])


def test_experiment_defaults(client, cfg):
    d = client.put("/api/experiments/qubit_flux_sweep/defaults", json={"averages": 5, "settle_s": 0.05}).json()
    assert d["saved_defaults"] == ["averages"]           # settle_s is the built-in default: not saved
    assert next(p for p in d["params"] if p["key"] == "averages")["default"] == 5
    assert client.post("/api/experiments/qubit_flux_sweep/plan", json={}).json()["params"]["averages"] == 5
    assert saved(cfg)["experiment_defaults"] == {"qubit_flux_sweep": {"averages": 5}}
    assert client.get("/api/settings").json()["experiment_defaults"] == {"qubit_flux_sweep": ["averages"]}
    d = client.delete("/api/experiments/qubit_flux_sweep/defaults").json()
    assert d["saved_defaults"] == [] and saved(cfg) == {}
