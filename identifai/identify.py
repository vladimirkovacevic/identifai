from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_RULES_FULL_SR = (
    "Identifikuj glavni subjekat na slici (npr. biljka, životinja, predmet, spomenik). "
    "Daj srpski naziv (latinica) i latinski naziv ako postoji. "
    "Dodaj 2-4 kratke činjenice (stanište, jestivost/otrovnost, briga, itd.). "
    "Odgovori isključivo na srpskom (latinica), najviše ~150 reči. "
    "Ako nisi siguran, jasno napiši: \"nisam siguran\". "
    "Ne izmišljaj činjenice."
)

_QUICK_TAG = "__SUBJECT__:"
_SUBJECT_LINE_RE = re.compile(rf"^{re.escape(_QUICK_TAG)}\s*(.+?)\s*$", re.MULTILINE)


class IdentifyError(RuntimeError):
    """Raised when the underlying claude CLI call fails."""


def _run_claude(
    *,
    claude_bin: str,
    model: str,
    allowed_tools: str,
    prompt: str,
    timeout_seconds: int,
) -> str:
    cmd = [
        claude_bin,
        "-p", prompt,
        "--allowedTools", allowed_tools,
        "--output-format", "text",
        "--model", model,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    if proc.returncode != 0:
        logger.error("claude exit=%s stderr=%s", proc.returncode, proc.stderr[:500].strip())
        raise IdentifyError(f"claude exited {proc.returncode}")
    reply = proc.stdout.strip()
    if not reply:
        raise IdentifyError("claude produced empty output")
    return reply


def _with_tmp_image(image_bytes: bytes, suffix: str):
    """Write image to a temp file under XDG_RUNTIME_DIR; caller is responsible for cleanup."""
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "identifAI"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(prefix="img-", suffix=suffix, dir=runtime_dir)
    with os.fdopen(fd, "wb") as f:
        f.write(image_bytes)
    return Path(tmp_path_str)


def identify(
    image_bytes: bytes,
    *,
    claude_bin: str,
    model: str,
    timeout_seconds: int,
    suffix: str = ".jpg",
) -> str:
    """Single-call vision-only identification.

    Used when use_web_search is disabled. Returns one Serbian message with
    name + 2-4 facts. No sources.
    """
    tmp = _with_tmp_image(image_bytes, suffix)
    try:
        prompt = f"Pročitaj sliku na putanji: {tmp}\n\n{_RULES_FULL_SR}"
        return _run_claude(
            claude_bin=claude_bin,
            model=model,
            allowed_tools="Read",
            prompt=prompt,
            timeout_seconds=timeout_seconds,
        )
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def identify_quick(
    image_bytes: bytes,
    *,
    claude_bin: str,
    model: str,
    timeout_seconds: int,
    suffix: str = ".jpg",
) -> tuple[str, str | None]:
    """Two-stage step 1: fast vision-only identification.

    Returns (user_facing_text, subject_for_search_or_None).
    The user_facing_text is what we send to Telegram; subject is the
    string passed to `search_facts` in stage 2 (None if uncertain).
    """
    tmp = _with_tmp_image(image_bytes, suffix)
    try:
        prompt = (
            f"Pročitaj sliku na putanji: {tmp}\n\n"
            "Identifikuj glavni subjekat. Odgovori u dva dela:\n"
            "1) Kratak srpski opis (latinica), 1-2 rečenice. Spomeni srpski "
            "i latinski naziv ako postoji. Bez izvora i bez markdown formatiranja.\n"
            "2) Na samoj poslednjoj liniji, BEZ ikakve dekoracije, napiši:\n"
            f"   {_QUICK_TAG} <Latinski naziv ili NEPOZNATO>\n\n"
            f"Ne dodaj ništa nakon {_QUICK_TAG} linije. "
            "Ako nisi siguran u identifikaciju, napiši \"nisam siguran\" u "
            f"opisu i NEPOZNATO posle {_QUICK_TAG}."
        )
        raw = _run_claude(
            claude_bin=claude_bin,
            model=model,
            allowed_tools="Read",
            prompt=prompt,
            timeout_seconds=timeout_seconds,
        )
        return _parse_quick(raw)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _parse_quick(raw: str) -> tuple[str, str | None]:
    m = _SUBJECT_LINE_RE.search(raw)
    subject: str | None = None
    if m:
        candidate = m.group(1).strip()
        if candidate and candidate.upper() != "NEPOZNATO":
            subject = candidate
        user_text = raw[: m.start()].rstrip()
    else:
        # Model didn't follow the format; fall back to showing whole reply.
        logger.warning("identify_quick: %s line missing; falling back", _QUICK_TAG)
        user_text = raw.strip()
    if not user_text:
        user_text = raw.strip()
    return user_text, subject


def deep_check(
    image_bytes: bytes,
    *,
    subject: str | None,
    quick_summary: str,
    claude_bin: str,
    model: str,
    timeout_seconds: int,
    suffix: str = ".jpg",
) -> str:
    """Two-stage step 2: re-read the image + web search + cross-check stage 1.

    The model gets the image again (`Read`) plus stage 1's identification
    as context, runs `WebSearch` against authoritative sources, and is
    asked to either confirm the stage 1 ID or flag an inconsistency.
    Returns Serbian text with a sources line.
    """
    tmp = _with_tmp_image(image_bytes, suffix)
    try:
        seed = subject or "(stage 1 nije bio siguran)"
        prompt = (
            f"Pročitaj sliku na putanji: {tmp}\n\n"
            f"Stage 1 (samo vizuelno) je identifikovao: \"{seed}\".\n"
            f"Stage 1 opis: {quick_summary}\n\n"
            "Tvoj zadatak:\n"
            "1) Pogledaj sliku i pretraži web koristeći najpouzdanije izvore "
            "   (Wikipedia, akademski sajtovi, etablirane baze podataka). "
            "   Sakupi top rezultate.\n"
            "2) Proveri konzistentnost: da li to što web kaže odgovara onome "
            "   što vidiš na slici? Ako se ne slaže sa Stage 1 identifikacijom, "
            "   jasno napomeni: \"Napomena: pretraga i slika ukazuju na ... "
            "   umesto Stage 1 identifikacije ...\".\n"
            "3) Prikupi 3-5 dodatnih, zanimljivih činjenica (stanište, "
            "   jestivost/otrovnost, briga, istorija, kuriozitet). Ne ponavljaj "
            "   sadržaj iz Stage 1 opisa.\n"
            "4) Odgovori isključivo na srpskom (latinica), do 180 reči.\n"
            "5) Na samoj poslednjoj liniji navedi izvore u formatu:\n"
            "   \"Izvori: <url1>; <url2>; <url3>\".\n\n"
            "Ako pretraga ne vrati pouzdane rezultate, napiši: "
            "\"Nisam pronašao dodatne pouzdane informacije.\" — bez izmišljanja."
        )
        return _run_claude(
            claude_bin=claude_bin,
            model=model,
            allowed_tools="Read,WebSearch",
            prompt=prompt,
            timeout_seconds=timeout_seconds,
        )
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
