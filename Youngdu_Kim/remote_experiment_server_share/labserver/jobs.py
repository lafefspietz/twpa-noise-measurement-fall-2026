"""Runs one experiment at a time in a background thread.

The experiment's run() reserves its instruments (mgr.claim), so the UI and the live
view can't touch them until it ends. It calls job.check() between steps: that raises
JobStopped after the stop button and OutputsOff after "outputs off". Whatever is in
job.record when run() returns or raises is saved as JSON (with "status"), so a stopped
or failed sweep keeps the points it measured.

The UI polls info(since=k): status, progress and the result rows from k on, so each
poll only carries the rows it hasn't seen.
"""
from __future__ import annotations

import logging
import threading
import time

from .manager import InstrumentBusy, InstrumentManager, OutputsOff
from .storage import save_record

log = logging.getLogger("labserver.jobs")


class JobStopped(Exception):
    pass


class Job:
    def __init__(self, exp, params: dict, mgr: InstrumentManager):
        self.exp = exp
        self.params = params
        self.mgr = mgr
        self.id = f"{int(time.time() * 1000)}-{exp.id}"
        self.owner = f"job:{exp.id}"
        self.status = "starting"        # starting | running | done | stopped | outputs_off | failed
        self.message = ""
        self.notes: list[str] = []      # problems worth showing that didn't end the run
        self.points_done = self.points_total = 0
        self.traces_done = self.traces_total = 0
        self.axis: dict | None = None   # {"x": [...], "x_unit": "Hz"} for the result rows
        self.rows: list[dict] = []
        self.record: dict = {}
        self.started = time.time()
        self.finished: float | None = None
        self.file: str | None = None
        self.url: str | None = None
        self.html_url: str | None = None      # the interactive HTML page of the saved record
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    # ---- called by the experiment ----
    def check(self):
        if self._stop.is_set():
            raise JobStopped("stopped by user")
        self.mgr.check_halt()

    def sleep(self, seconds: float):
        end = time.monotonic() + seconds
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 0.05))

    def progress(self, message: str = "", **counts):
        for k, v in counts.items():
            setattr(self, k, v)
        if message:
            self.message = message

    def add_row(self, row: dict):
        self.rows.append(row)

    # ---- for the API ----
    @property
    def running(self) -> bool:
        return self.status in ("starting", "running")

    def stop(self):
        self._stop.set()

    def info(self, since: int | None = None) -> dict:
        now = self.finished or time.time()
        elapsed = now - self.started
        eta = None
        if self.running and self.traces_done and self.traces_total:
            eta = elapsed / self.traces_done * (self.traces_total - self.traces_done)
        out = {"id": self.id, "experiment": self.exp.id, "title": self.exp.title, "status": self.status,
               "message": self.message, "notes": self.notes, "points_done": self.points_done, "points_total": self.points_total,
               "traces_done": self.traces_done, "traces_total": self.traces_total,
               "started": self.started, "elapsed_s": round(elapsed, 1),
               "eta_s": None if eta is None else round(eta, 1), "file": self.file, "url": self.url,
               "html_url": self.html_url,
               "n_rows": len(self.rows)}
        if since is not None:
            out["params"] = self.params
            out["axis"] = self.axis
            out["since"] = since
            out["rows"] = self.rows[since:]
        return out


class JobRunner:
    def __init__(self, mgr: InstrumentManager, data_dir: str):
        self.mgr = mgr
        self.data_dir = data_dir
        self.job: Job | None = None         # the running job, or the last one
        self._lock = threading.Lock()

    def start(self, exp, params: dict) -> Job:
        with self._lock:
            if self.job is not None and self.job.running:
                raise InstrumentBusy(f"{self.job.exp.title} is running; stop it first")
            self.mgr.check_halt()
            job = Job(exp, params, self.mgr)
            job.thread = threading.Thread(target=self._run, args=(job,), name=f"job-{exp.id}", daemon=True)
            self.job = job
        log.info("job %s started: %s", job.id, params)
        job.thread.start()
        return job

    @property
    def busy(self) -> bool:
        job = self.job
        return job is not None and job.running

    def get(self, job_id: str) -> Job | None:
        job = self.job
        return job if job is not None and job.id == job_id else None

    def _run(self, job: Job):
        job.status = "running"
        message = ""
        try:
            job.exp.run(job.params, job)
            status = "done"
        except JobStopped as e:
            status, message = "stopped", str(e)
        except OutputsOff:
            status, message = "outputs_off", "stopped by outputs off (the 'when done' step was skipped)"
        except Exception as e:
            status, message = "failed", f"{type(e).__name__}: {e}"
            log.exception("job %s failed", job.id)
        job.finished = time.time()
        if message:
            job.message = message
        if job.record:          # saved before the status changes, so a finished job always has its file
            try:
                record = {**job.record, "status": status, "message": job.message, "notes": job.notes,
                          "start_time": int(job.started), "finish_time": int(job.finished),
                          "sweep_time": round(job.finished - job.started, 1)}
                path = save_record(self.data_dir, job.params.get("label") or job.exp.id, record)
                job.file, job.url = path.name, f"/api/data/{path.parent.name}/{path.name}"
                job.html_url = f"/api/data/{path.parent.name}/{path.stem}.html"
                log.info("job %s saved %s", job.id, path)
            except Exception as e:
                job.message += f"; SAVE FAILED: {type(e).__name__}: {e}"
                log.exception("job %s: save failed", job.id)
        job.status = status
        self.mgr.end_halt()     # after outputs off: the job is over, commands are allowed again
        log.info("job %s %s: %s", job.id, status, job.message)

    def stop_all(self, timeout: float = 30.0):
        job = self.job
        if job is not None and job.running:
            job.stop()
            if job.thread is not None:
                job.thread.join(timeout)
