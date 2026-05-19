from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

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


def _with_tmp_image(image_bytes: bytes, suffix: str) -> Path:
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
    language: str,
    timeout_seconds: int,
    suffix: str = ".jpg",
) -> str:
    """Single-call vision-only identification.

    Used when use_web_search is disabled. Returns one message in `language`
    with name + 2-4 facts. No sources.
    """
    tmp = _with_tmp_image(image_bytes, suffix)
    try:
        prompt = (
            f"Read the image at: {tmp}\n\n"
            "Identify the primary subject (e.g. plant, animal, object, landmark). "
            f"Give the common name and the latin name if applicable. "
            "Add 2-4 short, useful facts (habitat, edibility/toxicity, care, history — "
            "whatever fits the subject). "
            f"Reply ONLY in {language}, at most ~150 words. "
            "If you are not certain, say so plainly in the reply — do not invent facts."
        )
        return _run_claude(
            claude_bin=claude_bin, model=model, allowed_tools="Read",
            prompt=prompt, timeout_seconds=timeout_seconds,
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
    language: str,
    timeout_seconds: int,
    suffix: str = ".jpg",
) -> tuple[str, str | None]:
    """Two-stage step 1: fast vision-only identification.

    Returns (user_facing_text, subject_for_search_or_None). The text is
    in `language`; the subject (latin name) is plain ASCII for searching.
    """
    tmp = _with_tmp_image(image_bytes, suffix)
    try:
        prompt = (
            f"Read the image at: {tmp}\n\n"
            "Identify the primary subject. Reply in two parts:\n"
            f"1) A brief description in {language}, 1-2 sentences. Mention the "
            "common name in that language and the latin name if applicable. "
            "No sources, no markdown formatting.\n"
            "2) On the very last line, with no decoration, write:\n"
            f"   {_QUICK_TAG} <Latin name, or UNKNOWN>\n\n"
            f"Do not add anything after the {_QUICK_TAG} line. "
            f"If you are not sure of the identification, say so in {language} "
            f"in the description and write UNKNOWN after {_QUICK_TAG}."
        )
        raw = _run_claude(
            claude_bin=claude_bin, model=model, allowed_tools="Read",
            prompt=prompt, timeout_seconds=timeout_seconds,
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
        if candidate and candidate.upper() not in ("UNKNOWN", "NEPOZNATO"):
            subject = candidate
        user_text = raw[: m.start()].rstrip()
    else:
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
    language: str,
    timeout_seconds: int,
    suffix: str = ".jpg",
) -> str:
    """Two-stage step 2: re-read the image + web search + cross-check stage 1.

    The model gets the image again (`Read`) plus stage 1's identification
    as context, runs `WebSearch` against authoritative sources, and is
    asked to either confirm the stage 1 ID or flag an inconsistency.
    Reply is in `language`.
    """
    tmp = _with_tmp_image(image_bytes, suffix)
    try:
        seed = subject or "(stage 1 was not sure)"
        prompt = (
            f"Read the image at: {tmp}\n\n"
            f"Stage 1 (vision only) identified: \"{seed}\".\n"
            f"Stage 1 description: {quick_summary}\n\n"
            "Your task:\n"
            "1) Look at the image and search the web using the most reliable "
            "   sources (Wikipedia, academic sites, established databases). "
            "   Gather the top results.\n"
            "2) Check consistency: does what the web says match what you see "
            "   in the image? If it disagrees with Stage 1, clearly flag it "
            f"   in {language} (e.g. natural-language equivalent of "
            "   \"Note: search and image suggest this is X rather than Y\").\n"
            "3) Collect 3-5 additional interesting facts (habitat, edibility, "
            "   care, history, curiosities). Do not repeat content from the "
            "   Stage 1 description.\n"
            f"4) Reply ONLY in {language}, at most 180 words.\n"
            "5) On the very last line, list sources prefixed with the natural "
            f"   word for \"Sources\" in {language} (e.g. \"Izvori:\" for "
            "   Serbian, \"Sources:\" for English), then the URLs separated "
            "   by \"; \".\n\n"
            "If the search does not return reliable results, say so plainly "
            f"in {language} — do not invent."
        )
        return _run_claude(
            claude_bin=claude_bin, model=model, allowed_tools="Read,WebSearch",
            prompt=prompt, timeout_seconds=timeout_seconds,
        )
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
