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
from .identify import IdentifyError, deep_check, identify, identify_quick

logger = logging.getLogger(__name__)

_REJECT_SR = "Žao mi je, ovaj bot je privatan. Nemate dozvolu da ga koristite."
_LIMIT_SR = "Dnevni limit ({limit}) je dostignut. Pokušajte sutra."
_HELP_SR = (
    "Pošaljite mi sliku biljke, životinje, predmeta ili spomenika i pokušaću "
    "da je identifikujem. Odgovaram na srpskom (latinica)."
)
_PLACEHOLDER_SR = "🔎 tražim…"
_ERROR_SR = "Došlo je do greške pri identifikaciji. Pokušajte ponovo."
_DEEP_ERROR_SR = "Identifikacija je gotova, ali web pretraga nije uspela."


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


def _save_replies(responses_dir: Path, **named: str) -> Path:
    """Save one file per named reply under responses/<UTC-ts>/."""
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_dir = responses_dir / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in named.items():
        (out_dir / f"{name}.txt").write_text(text, encoding="utf-8")
    return out_dir


def _is_allowed(update: Update, cfg: Config) -> bool:
    user = update.effective_user
    return bool(user and user.id in cfg.allowed_user_ids)


def _ms(t0: float, t1: float) -> int:
    return int((t1 - t0) * 1000)


async def _start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP_SR)


async def _help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP_SR)


async def _non_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = ctx.application.bot_data["cfg"]
    if not _is_allowed(update, cfg):
        return
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
        logger.info("photo received user_id=%s msg_id=%s", user.id, update.message.message_id)

        await update.message.reply_text(_PLACEHOLDER_SR)
        t_after_ack = time.monotonic()
        logger.info("placeholder sent ack_ms=%d", _ms(t_start, t_after_ack))

        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        img_bytes = bytes(await tg_file.download_as_bytearray())
        t_after_dl = time.monotonic()
        logger.info(
            "photo downloaded bytes=%d download_ms=%d",
            len(img_bytes), _ms(t_after_ack, t_after_dl),
        )

        loop = asyncio.get_running_loop()
        if cfg.use_web_search:
            await _handle_two_stage(
                update, cfg, user, img_bytes, loop,
                t_start=t_start, t_after_ack=t_after_ack, t_after_dl=t_after_dl,
            )
        else:
            await _handle_single_stage(
                update, cfg, user, img_bytes, loop,
                t_start=t_start, t_after_ack=t_after_ack, t_after_dl=t_after_dl,
            )

    return handler


async def _handle_single_stage(update, cfg, user, img_bytes, loop, *,
                               t_start, t_after_ack, t_after_dl):
    logger.info("calling claude (single-stage, vision-only) model=%s", cfg.model)
    try:
        reply = await loop.run_in_executor(
            None,
            lambda: identify(
                img_bytes,
                claude_bin=cfg.claude_bin,
                model=cfg.model,
                language=cfg.language,
                timeout_seconds=cfg.claude_timeout_seconds,
            ),
        )
    except IdentifyError as e:
        t_err = time.monotonic()
        logger.error(
            "result=error stage=single user_id=%s bytes=%d "
            "ack_ms=%d download_ms=%d identify_ms=%d total_ms=%d err=%s",
            user.id, len(img_bytes),
            _ms(t_start, t_after_ack), _ms(t_after_ack, t_after_dl),
            _ms(t_after_dl, t_err), _ms(t_start, t_err), e,
        )
        await update.message.reply_text(_ERROR_SR)
        return
    t_after_id = time.monotonic()
    logger.info("claude reply received chars=%d identify_ms=%d",
                len(reply), _ms(t_after_dl, t_after_id))

    await update.message.reply_text(reply)
    t_after_send = time.monotonic()
    logger.info("reply sent send_ms=%d", _ms(t_after_id, t_after_send))

    _save_replies(cfg.responses_dir, reply=reply)
    t_after_save = time.monotonic()

    logger.info(
        "result=ok stage=single user_id=%s bytes=%d model=%s search=False "
        "ack_ms=%d download_ms=%d identify_ms=%d send_ms=%d save_ms=%d total_ms=%d",
        user.id, len(img_bytes), cfg.model,
        _ms(t_start, t_after_ack), _ms(t_after_ack, t_after_dl),
        _ms(t_after_dl, t_after_id), _ms(t_after_id, t_after_send),
        _ms(t_after_send, t_after_save), _ms(t_start, t_after_save),
    )


async def _handle_two_stage(update, cfg, user, img_bytes, loop, *,
                            t_start, t_after_ack, t_after_dl):
    # --- Stage 1: fast vision-only ID ---
    logger.info("calling claude (stage 1: vision-only quick ID) model=%s", cfg.model)
    try:
        quick_text, subject = await loop.run_in_executor(
            None,
            lambda: identify_quick(
                img_bytes,
                claude_bin=cfg.claude_bin,
                model=cfg.model,
                language=cfg.language,
                timeout_seconds=cfg.claude_timeout_seconds,
            ),
        )
    except IdentifyError as e:
        t_err = time.monotonic()
        logger.error(
            "result=error stage=quick user_id=%s bytes=%d "
            "ack_ms=%d download_ms=%d quick_ms=%d total_ms=%d err=%s",
            user.id, len(img_bytes),
            _ms(t_start, t_after_ack), _ms(t_after_ack, t_after_dl),
            _ms(t_after_dl, t_err), _ms(t_start, t_err), e,
        )
        await update.message.reply_text(_ERROR_SR)
        return
    t_after_quick = time.monotonic()
    logger.info(
        "stage 1 reply received subject=%r chars=%d quick_ms=%d",
        subject, len(quick_text), _ms(t_after_dl, t_after_quick),
    )

    await update.message.reply_text(quick_text)
    t_after_send1 = time.monotonic()
    logger.info("stage 1 sent send1_ms=%d", _ms(t_after_quick, t_after_send1))

    # --- Stage 2: re-vision + web search + cross-check ---
    logger.info("calling claude (stage 2: vision + web search) subject=%r", subject)
    deep_text: str | None = None
    try:
        deep_text = await loop.run_in_executor(
            None,
            lambda: deep_check(
                img_bytes,
                subject=subject,
                quick_summary=quick_text,
                claude_bin=cfg.claude_bin,
                model=cfg.model,
                language=cfg.language,
                timeout_seconds=cfg.claude_timeout_seconds,
            ),
        )
    except IdentifyError as e:
        logger.warning("stage 2 failed (sending degraded reply): %s", e)
        await update.message.reply_text(_DEEP_ERROR_SR)
    t_after_deep = time.monotonic()

    t_after_send2 = t_after_deep
    if deep_text is not None:
        logger.info(
            "stage 2 reply received chars=%d deep_ms=%d",
            len(deep_text), _ms(t_after_send1, t_after_deep),
        )
        await update.message.reply_text(deep_text)
        t_after_send2 = time.monotonic()
        logger.info("stage 2 sent send2_ms=%d", _ms(t_after_deep, t_after_send2))
    else:
        logger.info("stage 2 skipped deep_ms=%d", _ms(t_after_send1, t_after_deep))

    saved = {"stage1_quick": quick_text}
    if deep_text is not None:
        saved["stage2_deep"] = deep_text
    _save_replies(cfg.responses_dir, **saved)
    t_after_save = time.monotonic()

    logger.info(
        "result=ok stage=two user_id=%s bytes=%d model=%s search=True subject=%r "
        "ack_ms=%d download_ms=%d quick_ms=%d send1_ms=%d "
        "deep_ms=%d send2_ms=%d save_ms=%d total_ms=%d",
        user.id, len(img_bytes), cfg.model, subject,
        _ms(t_start, t_after_ack), _ms(t_after_ack, t_after_dl),
        _ms(t_after_dl, t_after_quick), _ms(t_after_quick, t_after_send1),
        _ms(t_after_send1, t_after_deep), _ms(t_after_deep, t_after_send2),
        _ms(t_after_send2, t_after_save), _ms(t_start, t_after_save),
    )


def build_app(cfg: Config) -> Application:
    app = Application.builder().token(cfg.telegram_bot_token).build()
    app.bot_data["cfg"] = cfg
    counter = _DailyCounter(cfg.daily_image_limit)
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(MessageHandler(filters.PHOTO, _make_photo_handler(cfg, counter)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _non_photo))
    return app
