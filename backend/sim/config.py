"""Paths and settings. Everything is resolved relative to the project root."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _find_root() -> Path:
    env = os.environ.get("TSIM_HOME")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "config").is_dir():
            return parent
    return Path.cwd()


ROOT = _find_root()


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines). Real environment variables win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    data_dir: Path = Path(os.environ.get("TSIM_DATA_DIR", ROOT / "data"))
    config_dir: Path = ROOT / "config"
    strategies_dir: Path = Path(os.environ.get("TSIM_STRATEGIES_DIR", ROOT / "strategies"))
    frontend_dist: Path = ROOT / "frontend" / "dist"
    host: str = os.environ.get("TSIM_HOST", "127.0.0.1")
    port: int = int(os.environ.get("TSIM_PORT", "8000"))
    api_key: str = os.environ.get("TSIM_API_KEY", "")

    @property
    def bars_dir(self) -> Path:
        return self.data_dir / "bars"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sim.db"


settings = Settings()
