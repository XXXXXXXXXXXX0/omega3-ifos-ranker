"""Load config.yaml and resolve the handful of derived paths."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


class Config(dict):
    """Dict with attribute access; nested dicts are wrapped on read."""

    def __getattr__(self, key: str) -> Any:
        try:
            value = self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc
        return Config(value) if isinstance(value, dict) else value

    # --- derived paths -----------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self.get("_root", ROOT))

    @property
    def data_dir(self) -> Path:
        return self.root / self["paths"]["data_dir"]

    @property
    def reports_dir(self) -> Path:
        return self.root / self["paths"]["reports_dir"]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "omega3.sqlite"

    @property
    def today(self) -> dt.date:
        """Date the run is evaluated against (expiry checks, changelog name)."""
        raw = self.get("as_of_date")
        if raw:
            return raw if isinstance(raw, dt.date) else dt.date.fromisoformat(str(raw))
        return dt.datetime.now(dt.timezone.utc).date()


def load_config(path: Path | str | None = None, root: Path | str | None = None) -> Config:
    """Read the YAML config. `root` relocates data/ and reports/ (used by tests)."""
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as fh:
        cfg = Config(yaml.safe_load(fh))
    if root is not None:
        cfg["_root"] = str(root)
    return cfg
