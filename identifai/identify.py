from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 90

_RULES_SR = (
    "Identifikuj glavni subjekat na slici (npr. biljka, životinja, predmet, spomenik). "
    "Daj srpski naziv (latinica) i latinski naziv ako postoji. "
    "Dodaj 2-4 kratke činjenice (stanište, jestivost/otrovnost, briga, itd. — što je relevantno). "
    "Odgovori isključivo na srpskom (latinica), najviše ~150 reči. "
    "Ako nisi siguran, jasno napiši: \"nisam siguran\". "
    "Ne izmišljaj činjenice."
)

_SEARCH_INSTR_SR = (
    "Koristi WebSearch da potvrdiš identitet i prikupiš činjenice iz pouzdanih izvora. "
    "U poslednjem redu obavezno navedi: \"Izvori: <url1>; <url2>\"."
)


class IdentifyError(RuntimeError):
    """Raised when the underlying claude CLI call fails."""


def identify(
    image_bytes: bytes,
    *,
    claude_bin: str,
    model: str,
    use_web_search: bool,
    suffix: str = ".jpg",
) -> str:
    """Run `claude -p` on the image and return the reply text."""
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "identifAI"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    fd, tmp_path_str = tempfile.mkstemp(prefix="img-", suffix=suffix, dir=runtime_dir)
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(image_bytes)

        allowed = "Read,WebSearch" if use_web_search else "Read"
        prompt_parts = [
            f"Pročitaj sliku na putanji: {tmp_path}",
            _RULES_SR,
        ]
        if use_web_search:
            prompt_parts.append(_SEARCH_INSTR_SR)
        prompt = "\n\n".join(prompt_parts)

        cmd = [
            claude_bin,
            "-p", prompt,
            "--allowedTools", allowed,
            "--output-format", "text",
            "--model", model,
        ]
        logger.debug("running %s", cmd[:5])
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            logger.error(
                "claude exit=%s stderr=%s",
                proc.returncode, proc.stderr[:500].strip(),
            )
            raise IdentifyError(f"claude exited {proc.returncode}")
        reply = proc.stdout.strip()
        if not reply:
            raise IdentifyError("claude produced empty output")
        return reply
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
