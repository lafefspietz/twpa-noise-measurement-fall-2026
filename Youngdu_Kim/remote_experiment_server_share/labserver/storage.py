"""JSON records in the same layout the notebooks use.

    <data_dir>/DD-MM-YYYY/<epoch>-<name>.json

with timestamp/date/time/timezone fields at the top level (as in vna-trace.ipynb),
indent=4, and a README.md per day folder ("#  DD-MM-YYYY Plots") like the notebooks
create. Files are written to a temp name first and renamed, so a crash never leaves
half a JSON file.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np


def _default(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, complex):
        return {"real": o.real, "imag": o.imag}
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    return s or "record"


def save_record(data_dir: str | Path, name: str, record: dict, now: datetime | None = None) -> Path:
    now = (now or datetime.now()).astimezone()
    folder = Path(data_dir) / now.strftime("%d-%m-%Y")
    folder.mkdir(parents=True, exist_ok=True)
    readme = folder / "README.md"
    if not readme.exists():
        readme.write_text(f"#  {now.strftime('%d-%m-%Y')} Plots\n\n", encoding="utf-8")

    epoch = int(now.timestamp())
    header = {"timestamp": epoch, "date": now.strftime("%d-%m-%Y"),
              "time": now.strftime("%H:%M:%S"), "timezone": now.tzname()}
    path = folder / f"{epoch}-{slug(name)}.json"
    n = 1
    while path.exists():
        path = folder / f"{epoch}-{slug(name)}-{n}.json"
        n += 1
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({**header, **record}, f, indent=4, default=_default)
    os.replace(tmp, path)
    return path
