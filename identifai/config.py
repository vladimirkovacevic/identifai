from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

CONFIG_DIR = Path.home() / ".config" / "identifAI"
ENV_FILE = CONFIG_DIR / ".env"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_JSON = PROJECT_ROOT / "config.json"

_DEFAULTS: dict[str, object] = {
    "model": "claude-sonnet-4-6",
    "use_web_search": True,
    "language": "Serbian (latinica)",
    "daily_image_limit": 50,
    "claude_bin": "claude",
    "responses_dir": "./responses",
    "claude_timeout_seconds": 90,
}


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    daily_image_limit: int
    model: str
    use_web_search: bool
    language: str
    claude_bin: str
    responses_dir: Path
    claude_timeout_seconds: int


def _die(msg: str) -> None:
    sys.stderr.write(f"identifAI: {msg}\n")
    sys.exit(2)


def _require(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        _die(f"missing required env var {key} in {ENV_FILE}")
    return value


def _parse_user_ids(raw: str) -> frozenset[int]:
    try:
        return frozenset(int(x.strip()) for x in raw.split(",") if x.strip())
    except ValueError:
        _die(f"ALLOWED_TELEGRAM_USER_IDS must be comma-separated integers, got: {raw!r}")
        raise  # unreachable


def _load_json_config() -> dict:
    if not CONFIG_JSON.exists():
        return {}
    try:
        data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(f"{CONFIG_JSON}: invalid JSON: {e}")
    if not isinstance(data, dict):
        _die(f"{CONFIG_JSON}: top-level value must be an object")
    unknown = set(data) - set(_DEFAULTS)
    if unknown:
        _die(f"{CONFIG_JSON}: unknown keys: {sorted(unknown)}")
    return data


def _verify_claude(claude_bin: str) -> None:
    try:
        subprocess.run(
            [claude_bin, "--version"],
            check=True, capture_output=True, timeout=10,
        )
    except FileNotFoundError:
        _die(
            f"'{claude_bin}' not found on PATH. "
            "Install Claude Code and log in once, then retry."
        )
    except subprocess.CalledProcessError as e:
        _die(f"'{claude_bin} --version' failed: {e.stderr!r}")
    except subprocess.TimeoutExpired:
        _die(f"'{claude_bin} --version' timed out")


def _resolve(key: str, js: dict) -> object:
    """Precedence: env var > config.json > built-in default."""
    env_v = os.environ.get(key.upper(), "").strip()
    if env_v:
        return env_v
    if key in js:
        return js[key]
    return _DEFAULTS[key]


def _as_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def load() -> Config:
    load_dotenv(ENV_FILE, override=False)
    js = _load_json_config()

    allowed = _parse_user_ids(_require("ALLOWED_TELEGRAM_USER_IDS"))
    if not allowed:
        _die("ALLOWED_TELEGRAM_USER_IDS is empty")

    cfg = Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        allowed_user_ids=allowed,
        daily_image_limit=int(_resolve("daily_image_limit", js)),
        model=str(_resolve("model", js)).strip(),
        use_web_search=_as_bool(_resolve("use_web_search", js)),
        language=str(_resolve("language", js)).strip(),
        claude_bin=str(_resolve("claude_bin", js)).strip(),
        responses_dir=Path(str(_resolve("responses_dir", js))).resolve(),
        claude_timeout_seconds=int(_resolve("claude_timeout_seconds", js)),
    )
    _verify_claude(cfg.claude_bin)
    cfg.responses_dir.mkdir(parents=True, exist_ok=True)
    return cfg
