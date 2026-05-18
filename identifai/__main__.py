from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .bot import build_app
from .config import load

LOG_DIR = Path.home() / ".local" / "state" / "identifAI"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_h = logging.handlers.RotatingFileHandler(
        LOG_DIR / "identifai.log",
        maxBytes=10_000_000, backupCount=3, encoding="utf-8",
    )
    file_h.setFormatter(fmt)
    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_h)
    root.addHandler(stream_h)


def main() -> None:
    _setup_logging()
    cfg = load()
    logging.info(
        "identifAI starting: model=%s search=%s users=%d limit/day=%d",
        cfg.model, cfg.use_web_search, len(cfg.allowed_user_ids), cfg.daily_image_limit,
    )
    app = build_app(cfg)
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
