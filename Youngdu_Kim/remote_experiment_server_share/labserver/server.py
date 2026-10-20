"""HTTP API for the instrument laptop.

    python -m labserver.server                       # real instruments, reachable from LAN + Tailscale
    python -m labserver.server --sim                 # simulators
    python -m labserver.server --host tailscale      # only reachable over Tailscale
    python -m labserver.server --host local          # only from this machine

Interactive API docs at /docs. Endpoints are plain `def`, so FastAPI runs them in its
thread pool; the InstrumentManager locks keep instruments from being used twice at once.
Live traces stream over the WebSocket /ws/live/{instrument}. Experiments run as jobs,
one at a time (jobs.py); the UI polls /api/jobs/current for progress and result rows.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import load_config
from .drivers import InstrumentError, MeasurementError
from .experiments import EXPERIMENTS, ExperimentError
from .jobs import JobRunner
from .live import LiveHub
from .manager import InstrumentBusy, InstrumentManager, InstrumentUnavailable, OutputsOff
from .report import NotAnExperiment, render
from .settings import ConfirmNeeded, Settings, SettingsError
from .sim import SimWorld
from .storage import save_record
from .transport import TransportError

log = logging.getLogger("labserver.server")
STATIC = Path(__file__).resolve().parent / "static"

# exception -> (HTTP status, short code shown in the UI)
ERRORS = {
    InstrumentBusy: (409, "busy"),
    InstrumentUnavailable: (503, "unavailable"),
    OutputsOff: (423, "outputs_off"),
    InstrumentError: (400, "instrument_error"),
    ExperimentError: (400, "invalid_settings"),
    SettingsError: (400, "invalid_settings"),
    MeasurementError: (502, "measurement_failed"),
    TransportError: (502, "io_error"),
}


def create_app(cfg: dict, *, sim: bool = False, world: SimWorld | None = None) -> FastAPI:
    settings = Settings(cfg)        # first: it applies settings.local.yaml to cfg
    mgr = InstrumentManager(cfg, sim=sim, world=world)
    hub = LiveHub(mgr, cfg.get("live"))
    runner = JobRunner(mgr, cfg["data_dir"])

    def settings_changed(ids: list[str]):
        """Push changed settings to what is running (limits are read where they are used)."""
        for name in sorted({sid.split(".")[1] for sid in ids if sid.startswith("instruments.")}):
            mgr.reconfigure(name)
        if any(sid.startswith("live.") for sid in ids):
            hub.configure(cfg.get("live"))
        mgr.off_lock_timeout_s = cfg.get("off_lock_timeout_s", 3.0)

    settings.on_change(settings_changed)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        log.info("connecting instruments (sim=%s)", sim)
        mgr.connect_all()
        for name, s in mgr.status().items():
            log.info("  %-18s %s", name, s["idn"] if s["connected"] else f"NOT CONNECTED: {s['error']}")
        yield
        if app.state.shut_down_by_api:
            return          # /api/shutdown already closed everything and logged it
        # Ctrl-C / normal exit: outputs stay as they are
        runner.stop_all()
        hub.stop_all()
        mgr.shutdown()
        log.info("instruments closed, outputs left as they were")

    app = FastAPI(title="Remote Experiment Server", lifespan=lifespan)
    app.state.mgr = mgr
    app.state.live = hub
    app.state.jobs = runner
    app.state.settings = settings
    experiments = {cls.id: cls(cfg) for cls in EXPERIMENTS}
    aligning: dict[str, dict] = {}      # instrument -> its last alignment (see /api/{name}/align)
    app.state.uvicorn_server = None      # set by main() so /api/shutdown can stop the process
    app.state.shut_down_by_api = False

    for exc_type, (status, code) in ERRORS.items():
        def handler(request: Request, exc: Exception, status=status, code=code):
            log.warning("%s %s -> %d %s: %s", request.method, request.url.path, status, code, exc)
            return JSONResponse(status_code=status, content={"error": code, "detail": str(exc)})
        app.add_exception_handler(exc_type, handler)

    @app.exception_handler(ConfirmNeeded)
    def confirm_needed(request: Request, exc: ConfirmNeeded):
        return JSONResponse(status_code=409, content={"error": "confirm", "detail": str(exc),
                                                      "loosened": exc.loosened})

    def known(name: str):
        if name not in mgr.slots:
            raise InstrumentUnavailable(f"unknown instrument {name!r}; known: {sorted(mgr.slots)}")

    # ---- overview ----
    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/static/{file}")
    def static(file: str):
        """The page's other files (results.js/.css, shared with the HTML export)."""
        path = (STATIC / file).resolve()
        if path.parent != STATIC or not path.is_file():
            return JSONResponse(status_code=404, content={"error": "not_found", "detail": file})
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    def status():
        job = runner.job
        return {"sim": sim, "turning_off": mgr.halt.is_set(), "instruments": mgr.status(), "live": hub.info(),
                "align": aligning,
                "job": job.info() if job else None}

    @app.get("/api/instruments")
    def instruments():
        """What each instrument is and what can be set (for building UI panels)."""
        out = {}
        for name, slot in mgr.slots.items():
            inst = slot.inst
            out[name] = {
                "driver": slot.cfg["driver"],
                "kind": inst.kind if inst else None,
                "settable": inst.settable() if inst else [],
                "limits": inst.limits if inst else slot.cfg.get("limits", {}),
            }
            if inst is not None and hasattr(inst, "switches"):
                out[name]["switches"] = inst.switches
        return out

    # ---- settings tab (before /api/{name} as well) ----
    @app.get("/api/settings")
    def get_settings():
        """Every setting the settings tab offers, with its value now and in config.yaml."""
        return settings.describe()

    @app.post("/api/settings")
    def change_settings(changes: dict = Body({}, embed=True), reset: list[str] = Body([], embed=True),
                        confirm: bool = Body(False, embed=True)):
        """Apply {id: value} and put `reset` ids back to config.yaml, all or nothing.
        Loosening a limit or turning a safety switch on needs confirm=true (else 409
        with the list in `loosened`)."""
        changed = settings.update(changes, reset, confirm)
        return {"changed": changed, **settings.describe()}

    # ---- safety (registered before /api/{name} so the paths aren't captured) ----
    @app.post("/api/outputs_off")
    def outputs_off():
        """Turn every output off; the server keeps running. A running experiment stops
        without its 'when done' step; until it has, commands get 423 outputs_off."""
        results = mgr.outputs_off()
        if not runner.busy:
            mgr.end_halt()      # else the runner does, when the job has stopped
        return {"results": results, "turning_off": mgr.halt.is_set()}

    @app.post("/api/shutdown")
    def shutdown():
        """Stop the server. Outputs stay as they are (POST /api/outputs_off first to turn them off)."""
        log.warning("shutdown requested")
        runner.stop_all()
        hub.stop_all()
        mgr.shutdown()
        app.state.shut_down_by_api = True
        log.info("instruments closed, outputs left as they were")
        server = app.state.uvicorn_server
        if server is not None:
            # let this response go out first
            threading.Timer(0.5, lambda: setattr(server, "should_exit", True)).start()
        return {"stopping": server is not None}

    # ---- experiments (before /api/{name}/... as well) ----
    def experiment(exp_id: str):
        if exp_id not in experiments:
            raise InstrumentUnavailable(f"unknown experiment {exp_id!r}; known: {sorted(experiments)}")
        return experiments[exp_id]

    @app.get("/api/experiments")
    def list_experiments():
        """Every predefined experiment with its settings (the UI builds its forms from this)."""
        return [e.describe() for e in experiments.values()]

    @app.get("/api/experiments/{exp_id}")
    def describe_experiment(exp_id: str):
        return experiment(exp_id).describe()

    @app.put("/api/experiments/{exp_id}/defaults")
    def save_experiment_defaults(exp_id: str, params: dict = Body({})):
        """Save these settings as the experiment's defaults (settings.local.yaml): what the
        form starts with in a new browser and after its "defaults" button."""
        exp = experiment(exp_id)
        builtin = {p.key: p.default for p in exp.params}
        settings.set_experiment_defaults(exp_id, {k: v for k, v in exp.parse(params).items() if v != builtin[k]})
        return exp.describe()

    @app.delete("/api/experiments/{exp_id}/defaults")
    def reset_experiment_defaults(exp_id: str):
        exp = experiment(exp_id)
        settings.set_experiment_defaults(exp_id, None)
        return exp.describe()

    @app.post("/api/experiments/{exp_id}/plan")
    def plan_experiment(exp_id: str, params: dict = Body({}, examples=[{"sweep": "attenuator", "att_step_db": 3}])):
        """Check settings and list the sweep points. Changes no instrument (it only reads
        the warm switches)."""
        exp = experiment(exp_id)
        p = exp.parse(params)
        return {"params": p, **exp.plan(p, mgr)}

    @app.post("/api/experiments/{exp_id}/start")
    def start_experiment(exp_id: str, params: dict = Body({})):
        """Check the settings (as plan does) and start the run in the background."""
        exp = experiment(exp_id)
        p = exp.parse(params)
        errors = exp.plan(p, mgr)["errors"]
        if errors:
            raise ExperimentError("; ".join(errors))
        return runner.start(exp, p).info(since=0)

    # ---- jobs ----
    def find_job(job_id: str):
        job = runner.get(job_id)
        if job is None:
            raise InstrumentUnavailable(f"no job {job_id!r} (only the latest job is kept)")
        return job

    @app.get("/api/jobs/current")
    def current_job(since: int | None = None):
        """The running job or the last one (null if none yet). With ?since=k, also the
        result rows from k on."""
        job = runner.job
        return job.info(since) if job else None

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, since: int = 0):
        return find_job(job_id).info(since)

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str):
        """Stops after the trace being read; the settings are left as 'when done' says
        and the points measured so far are saved."""
        job = find_job(job_id)
        job.stop()
        return job.info()

    # ---- live view ----
    def live_view(name: str):
        if name not in hub.views:
            raise InstrumentUnavailable(f"no live view for {name!r}; available: {sorted(hub.views)}")
        return hub.views[name]

    @app.get("/api/live")
    def live_info():
        return hub.info()

    @app.post("/api/live/{name}/hold")
    def live_hold(name: str, hold: bool = Body(..., embed=True)):
        view = live_view(name)
        view.set_hold(hold)
        return view.info()

    @app.post("/api/live/{name}/average")
    def live_average(name: str, count: int | None = Body(None, embed=True), restart: bool = Body(False, embed=True)):
        """Running average over the last `count` traces (1 = off); restart=true starts it over."""
        view = live_view(name)
        if count is not None:
            try:
                view.set_average(count)
            except ValueError as e:
                raise InstrumentError(str(e)) from None
        if restart:
            view.restart_average()
        return view.info()

    @app.post("/api/live/{name}/save")
    def live_save(name: str, label: str = Body("", embed=True), note: str = Body("", embed=True),
                  seq: int | None = Body(None, embed=True)):
        """Save the trace on screen, with a snapshot of every instrument. `seq` is the
        frame the viewer is looking at (the UI sends it); without it, the latest trace."""
        view = live_view(name)
        if seq is None:
            seq, tr = view.seq, view.trace
            if tr is None:
                raise MeasurementError(f"no {name} trace yet (is the live view running?)")
        else:
            tr = view.trace_for(seq)
            if tr is None:      # never save a different trace than the one asked for
                raise MeasurementError(f"{name} frame {seq} is no longer kept (latest is {view.seq}); "
                                       f"press hold, then save")
        record = {"kind": "live_trace", "instrument": name, "note": note, "sim": sim, "seq": seq,
                  "trace": tr.to_dict(), "instruments": mgr.snapshot()}
        path = save_record(cfg["data_dir"], label or f"{name}-trace", record)
        log.info("saved %s", path)
        # the snapshot goes back too: the UI shows it instead of polling the instruments
        return {"path": str(path), "file": path.name, "url": f"/api/data/{path.parent.name}/{path.name}",
                "instruments": record["instruments"]}

    @app.get("/api/data/{day}/{file}")
    def download(day: str, file: str):
        """A saved record, sent as a download (the UI fetches this right after saving).
        <name>.html instead of <name>.json: the interactive HTML page of an experiment
        record (report.py), built from the JSON on request."""
        root = Path(cfg["data_dir"]).resolve()
        path = (root / day / file).resolve()
        src = path.with_suffix(".json")
        not_found = JSONResponse(status_code=404, content={"error": "not_found", "detail": f"{day}/{file}"})
        if path.parent.parent != root or path.suffix not in (".json", ".html") or not src.is_file():
            return not_found
        if path.suffix == ".json":
            return FileResponse(path, media_type="application/json", filename=path.name)
        rec = json.loads(src.read_text(encoding="utf-8"))
        exp = experiments.get(rec.get("experiment"))
        try:
            if exp is None:
                raise NotAnExperiment(f"{src.name} is not an experiment record")
            page = render(exp, rec, src.name)
        except NotAnExperiment as e:
            return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(e)})
        return HTMLResponse(page, headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

    @app.websocket("/ws/live/{name}")
    async def ws_live(ws: WebSocket, name: str):
        """Sends {"type": "frame", ...} for every new trace (latest only) and
        {"type": "status", ...} at least every 0.5 s."""
        await ws.accept()
        view = hub.views.get(name)
        if view is None:
            await ws.send_json({"type": "error", "detail": f"no live view for {name!r}"})
            await ws.close()
            return
        view.subscribe()
        last_seq, last_status, last_status_t = -1, None, 0.0
        try:
            while True:
                frame = view.frame
                if frame is not None and frame["seq"] != last_seq:
                    last_seq = frame["seq"]
                    await ws.send_json(frame)
                now = time.monotonic()
                if view.status != last_status or now - last_status_t > 0.5:
                    last_status, last_status_t = view.status, now
                    await ws.send_json({"type": "status", "instrument": name, **view.info()})
                await asyncio.sleep(0.02)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            view.unsubscribe()

    # ---- per instrument ----
    @app.get("/api/{name}/state")
    def get_state(name: str):
        known(name)
        return mgr.get_state(name)

    @app.post("/api/{name}")
    def apply(name: str, params: dict = Body(..., examples=[{"center_hz": 5.58e9, "span_hz": 0}])):
        known(name)
        log.info("apply %s %s", name, params)
        return mgr.apply(name, **params)

    @app.post("/api/{name}/align")
    def align(name: str):
        """Start the instrument's own alignment (SA: Align Now, All; 3-6 min) in the
        background. The instrument is reserved meanwhile, so the live view pauses and
        experiments can't use it. Progress and the result are in /api/status under "align"."""
        known(name)
        mgr.check_halt()
        slot = mgr.slots[name]
        if slot.inst is None or not hasattr(slot.inst, "align"):
            raise InstrumentError(f"{name} has no alignment")
        if (aligning.get(name) or {}).get("running"):
            raise InstrumentBusy(f"{name} is already aligning")
        if slot.claimed_by is not None:
            raise InstrumentBusy(f"{name} is reserved by {slot.claimed_by}")
        st = aligning[name] = {"running": True, "started": time.time(), "elapsed_s": 0,
                               "result": None, "error": None}
        owner = f"align:{name}"

        def run():
            try:
                with mgr.claim([name], owner), mgr.use(name, owner, timeout=30) as inst:
                    st["result"] = inst.align(progress=lambda s: st.update(elapsed_s=round(s)))
                log.info("%s alignment done: %s", name, st["result"])
            except Exception as e:
                st["error"] = f"{type(e).__name__}: {e}"
                log.error("%s alignment failed: %s", name, st["error"])
            finally:
                st["elapsed_s"] = round(time.time() - st["started"])
                st["running"] = False

        log.warning("%s alignment started", name)
        threading.Thread(target=run, name=f"align-{name}", daemon=True).start()
        return st

    @app.post("/api/{name}/reconnect")
    def reconnect(name: str):
        known(name)
        ok = mgr.connect(name)
        return {"connected": ok, **mgr.status()[name]}

    @app.post("/api/{name}/scpi")
    def raw_scpi(name: str, command: str = Body(..., embed=True)):
        """Raw command for debugging. Bypasses limits, so every use is logged.
        Commands containing '?' are sent as queries."""
        known(name)
        mgr.check_halt()
        log.warning("RAW %s << %s", name, command)
        with mgr.use(name, owner="api:scpi") as inst:
            if not hasattr(inst.t, "query"):
                raise InstrumentError(f"{name} does not take raw commands")
            try:
                if "?" in command:
                    return {"command": command, "response": inst.t.query(command).strip()}
                inst.t.write(command)
                return {"command": command, "response": None}
            finally:
                inst.forget_cached_state()      # the command may have changed settings

    return app


def tailscale_ip() -> str:
    out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
    ip = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    if not ip:
        raise SystemExit(f"could not get Tailscale IP: {out.stderr.strip()}")
    return ip


def resolve_host(host: str) -> str:
    """'all' -> every interface (LAN + Tailscale + localhost), 'tailscale' -> the
    Tailscale IP only, 'local' -> this machine only; anything else is used as given."""
    return {"all": "0.0.0.0", "local": "127.0.0.1"}.get(host) or (tailscale_ip() if host == "tailscale" else host)


def setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file = logging.FileHandler(log_dir / f"server-{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    file.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file)
    root.addHandler(console)


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sim", action="store_true", help="use simulators instead of instruments")
    parser.add_argument("--host", help="all | tailscale | local | an IP (default: server.host in config)")
    parser.add_argument("--port", type=int)
    parser.add_argument("--config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    host = resolve_host(args.host or cfg["server"].get("host", "all"))
    port = args.port or cfg["server"]["port"]
    setup_logging(Path(cfg["data_dir"]).parent / "logs")
    where = {"0.0.0.0": "all networks", "127.0.0.1": "this machine only"}.get(host, host)
    log.info("starting (sim=%s) on port %d, %s  %s", args.sim, port, where, time.strftime("%Y-%m-%d %H:%M:%S"))

    app = create_app(cfg, sim=args.sim)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_config=None))
    app.state.uvicorn_server = server
    server.run()


if __name__ == "__main__":
    main()
