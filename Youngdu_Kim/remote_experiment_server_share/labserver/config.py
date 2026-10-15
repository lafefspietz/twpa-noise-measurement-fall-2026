from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | Path | None = None) -> dict:
    """config.yaml as is. The settings-tab changes (settings.local.yaml, named by
    settings_file) are applied over it by settings.Settings when the server starts."""
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("sim", {})
    cfg.setdefault("instruments", {})
    cfg["data_dir"] = str((path.parent / cfg.get("data_dir", "data")).resolve())
    cfg["settings_file"] = str((path.parent / cfg.get("settings_file", "settings.local.yaml")).resolve())
    return cfg
