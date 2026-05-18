from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

CONFIG_DIR = Path.home() / ".config" / "identifAI"
ENV_FILE = CONFIG_DIR / ".env"


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    daily_image_limit: int
    model: str
    use_web_search: bool
    claude_bin: str
    responses_dir: Path


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
        raise  # unreachable, satisfies type checker


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


def load() -> Config:
    load_dotenv(ENV_FILE, override=False)
    allowed = _parse_user_ids(_require("ALLOWED_TELEGRAM_USER_IDS"))
    if not allowed:
        _die("ALLOWED_TELEGRAM_USER_IDS is empty")
    cfg = Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        allowed_user_ids=allowed,
        daily_image_limit=int(os.environ.get("DAILY_IMAGE_LIMIT", "50")),
        model=os.environ.get("MODEL", "claude-sonnet-4-6").strip(),
        use_web_search=os.environ.get("USE_WEB_SEARCH", "true").strip().lower() == "true",
        claude_bin=os.environ.get("CLAUDE_BIN", "claude").strip(),
        responses_dir=Path(os.environ.get("RESPONSES_DIR", "./responses")).resolve(),
    )
    _verify_claude(cfg.claude_bin)
    cfg.responses_dir.mkdir(parents=True, exist_ok=True)
    return cfg
