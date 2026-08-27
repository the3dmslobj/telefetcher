"""Credentials and on-disk locations.

api_id / api_hash come from https://my.telegram.org -> API development tools.
They identify the *app*, not the account; the account is the saved session.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

HOME_DIR = Path(os.environ.get("TELEFETCHER_HOME", Path.home() / ".telefetcher"))
CONFIG_PATH = HOME_DIR / "config.json"
DEFAULT_SESSION = "default"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str

    @property
    def masked_hash(self) -> str:
        return f"{self.api_hash[:4]}…{self.api_hash[-4:]}"


def session_path(name: str = DEFAULT_SESSION) -> Path:
    """Session files hold a live login. Keep them outside the repo by default."""
    if os.sep in name or name.endswith(".session"):
        return Path(name).expanduser().with_suffix(".session")
    return HOME_DIR / f"{name}.session"


def load_config() -> Config:
    """Env wins over the saved config file, so CI/one-offs can override."""
    load_dotenv(Path.cwd() / ".env", override=False)

    api_id = os.environ.get("TG_API_ID") or os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TG_API_HASH") or os.environ.get("TELEGRAM_API_HASH")

    if not (api_id and api_hash) and CONFIG_PATH.exists():
        saved = json.loads(CONFIG_PATH.read_text())
        api_id = api_id or saved.get("api_id")
        api_hash = api_hash or saved.get("api_hash")

    if not api_id or not api_hash:
        raise ConfigError(
            "No API credentials found.\n"
            "  1. Sign in at https://my.telegram.org -> 'API development tools'\n"
            "  2. Create an app and copy api_id + api_hash\n"
            "  3. Run:  tf login\n"
            "     (or set TG_API_ID / TG_API_HASH in the environment or a .env file)"
        )

    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        raise ConfigError(f"TG_API_ID must be a number, got {api_id!r}") from None

    return Config(api_id=api_id, api_hash=str(api_hash).strip())


def save_config(api_id: int, api_hash: str) -> Path:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"api_id": int(api_id), "api_hash": api_hash}, indent=2))
    CONFIG_PATH.chmod(0o600)
    return CONFIG_PATH
