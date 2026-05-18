from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config
from .identify import IdentifyError, identify

logger = logging.getLogger(__name__)

_REJECT_SR = "Žao mi je, ovaj bot je privatan. Nemate dozvolu da ga koristite."
_LIMIT_SR = "Dnevni limit ({limit}) je dostignut. Pokušajte sutra."
_HELP_SR = (
    "Pošaljite mi sliku biljke, životinje, predmeta ili spomenika i pokušaću "
    "da je identifikujem. Odgovaram na srpskom (latinica)."
)
_PLACEHOLDER_SR = "🔎 tražim…"
_ERROR_SR = "Došlo je do greške pri identifikaciji. Pokušajte ponovo."


class _DailyCounter:
    """In-memory counter that resets at UTC midnight."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._date: dt.date | None = None
        self._count = 0

    def try_increment(self) -> bool:
        today = dt.datetime.utcnow().date()
        if today != self._date:
            self._date = today
            self._count = 0
        if self._count >= self.limit:
            return False
        self._count += 1
        return True


def _save_reply(reply: str, responses_dir: Path) -> None:
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_dir = responses_dir / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "reply.txt").write_text(reply, encoding="utf-8")


def _is_allowed(update: Update, cfg: Config) -> bool:
    user = update.effective_user
    return bool(user and user.id in cfg.allowed_user_ids)


async def _start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP_SR)


async def _help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP_SR)


def _make_photo_handler(cfg: Config, counter: _DailyCounter):
    async def handler(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not _is_allowed(update, cfg):
            logger.info("rejected user_id=%s", user.id if user else "?")
            await update.message.reply_text(_REJECT_SR)
            return

        if not counter.try_increment():
            logger.info("daily limit hit user_id=%s", user.id)
            await update.message.reply_text(_LIMIT_SR.format(limit=cfg.daily_image_limit))
            return

        t_start = time.monotonic()
        await update.message.reply_text(_PLACEHOLDER_SR)
        t_after_ack = time.monotonic()

        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        img_bytes = bytes(await tg_file.download_as_bytearray())
        t_after_dl = time.monotonic()

        try:
            reply = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: identify(
                    img_bytes,
                    claude_bin=cfg.claude_bin,
                    model=cfg.model,
                    use_web_search=cfg.use_web_search,
                ),
            )
        except IdentifyError as e:
            t_err = time.monotonic()
            logger.exception(
                "result=error user_id=%s bytes=%d "
                "ack_ms=%d download_ms=%d identify_ms=%d total_ms=%d err=%s",
                user.id, len(img_bytes),
                _ms(t_start, t_after_ack), _ms(t_after_ack, t_after_dl),
                _ms(t_after_dl, t_err), _ms(t_start, t_err), e,
            )
            await update.message.reply_text(_ERROR_SR)
            return
        t_after_id = time.monotonic()

        await update.message.reply_text(reply)
        t_after_send = time.monotonic()

        _save_reply(reply, cfg.responses_dir)
        t_after_save = time.monotonic()

        logger.info(
            "result=ok user_id=%s bytes=%d model=%s search=%s "
            "ack_ms=%d download_ms=%d identify_ms=%d send_ms=%d save_ms=%d total_ms=%d",
            user.id, len(img_bytes), cfg.model, cfg.use_web_search,
            _ms(t_start, t_after_ack),
            _ms(t_after_ack, t_after_dl),
            _ms(t_after_dl, t_after_id),
            _ms(t_after_id, t_after_send),
            _ms(t_after_send, t_after_save),
            _ms(t_start, t_after_save),
        )

    return handler


def _ms(t0: float, t1: float) -> int:
    return int((t1 - t0) * 1000)


async def _non_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = ctx.application.bot_data["cfg"]
    if not _is_allowed(update, cfg):
        return
    await update.message.reply_text(_HELP_SR)


def build_app(cfg: Config) -> Application:
    app = Application.builder().token(cfg.telegram_bot_token).build()
    app.bot_data["cfg"] = cfg
    counter = _DailyCounter(cfg.daily_image_limit)
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(MessageHandler(filters.PHOTO, _make_photo_handler(cfg, counter)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _non_photo))
    return app
