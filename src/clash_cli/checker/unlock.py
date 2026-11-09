"""AI service unlock detection (based on clash-verge-rev media_unlock_checker)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx

from clash_cli.api.models import UnlockStatus

CLAUDE_BLOCKED = frozenset({"AF", "BY", "CN", "CU", "HK", "IR", "KP", "MO", "RU", "SY"})
GEMINI_BLOCKED = frozenset({"CHN", "RUS", "BLR", "CUB", "IRN", "PRK", "SYR", "HKG", "MAC"})

GEMINI_MARKER = ',2,1,200,"'

# Lightweight GETs; a blocked/dead endpoint must not hold status checks
# hostage for the full client timeout.
CHECK_TIMEOUT = 4.0

# Short-lived result cache so repeated status renders don't re-pay the
# dead-endpoint timeout. In-process results are mirrored to disk so short
# CLI invocations (`ccli status` twice in a minute) benefit too. Callers
# that just switched nodes must bypass it.
_CACHE_TTL = 60.0
_CACHE_FILE = Path.home() / ".cache" / "ccli" / "unlock.json"
_cache: dict[str, tuple[float, UnlockStatus]] = {}


def invalidate_unlock_cache() -> None:
    _cache.clear()
    try:
        _CACHE_FILE.unlink()
    except OSError:
        pass


def _read_disk_cache() -> dict[str, UnlockStatus]:
    try:
        raw = json.loads(_CACHE_FILE.read_text())
        ts = float(raw.get("ts", 0))
        if time.time() - ts >= _CACHE_TTL:
            return {}
        return {
            url: UnlockStatus(
                claude=item.get("claude", "fail"),
                chatgpt=item.get("chatgpt", "fail"),
                gemini=item.get("gemini", "fail"),
            )
            for url, item in raw.get("results", {}).items()
        }
    except (OSError, ValueError, TypeError):
        return {}


def _write_disk_cache(results: dict[str, tuple[float, UnlockStatus]]) -> None:
    payload = {
        "ts": time.time(),
        "results": {
            url: {"claude": s.claude, "chatgpt": s.chatgpt, "gemini": s.gemini}
            for url, (_ts, s) in results.items()
        },
    }
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(_CACHE_FILE)
    except OSError:
        pass


async def check_claude(proxy_url: str) -> str:
    """Check Claude unlock via cdn-cgi/trace. Returns 'ok:XX', 'blocked:XX', or 'fail'."""
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=CHECK_TIMEOUT) as client:
            resp = await client.get("https://claude.ai/cdn-cgi/trace")
            for line in resp.text.splitlines():
                if line.startswith("loc="):
                    loc = line[4:].strip().upper()
                    if loc in CLAUDE_BLOCKED:
                        return f"blocked:{loc}"
                    return f"ok:{loc}"
    except Exception:
        pass
    return "fail"


async def check_chatgpt(proxy_url: str) -> str:
    """Check ChatGPT unlock. Returns 'ok:XX', 'blocked:XX', or 'fail'."""
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=CHECK_TIMEOUT) as client:
            # Get region
            resp = await client.get("https://chat.openai.com/cdn-cgi/trace")
            loc = ""
            for line in resp.text.splitlines():
                if line.startswith("loc="):
                    loc = line[4:].strip().upper()
                    break

            # Check compliance
            resp2 = await client.get("https://api.openai.com/compliance/cookie_requirements")
            if "unsupported_country" in resp2.text.lower():
                return f"blocked:{loc or '??'}"

            if loc:
                return f"ok:{loc}"
    except Exception:
        pass
    return "fail"


async def check_gemini(proxy_url: str) -> str:
    """Check Gemini unlock via page source. Returns 'ok:XXX', 'blocked:XXX', or 'fail'."""
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=CHECK_TIMEOUT) as client:
            resp = await client.get("https://gemini.google.com")
            match = re.search(r',2,1,200,"([A-Z]{3})"', resp.text)
            if match:
                code = match.group(1)
                if code in GEMINI_BLOCKED:
                    return f"blocked:{code}"
                return f"ok:{code}"
    except Exception:
        pass
    return "fail"


async def check_all(
    proxy_url: str, *, use_cache: bool = True, gemini: bool = False
) -> UnlockStatus:
    """Run unlock checks concurrently.

    Gemini is opt-in (``_custom.unlock-checks.gemini`` in the mixin);
    when disabled it is neither probed nor displayed (``gemini == ""``).
    """
    cache_key = f"{proxy_url}#g={int(gemini)}"
    now = time.monotonic()
    hit = _cache.get(cache_key)
    if use_cache:
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
        for url, status in _read_disk_cache().items():
            _cache.setdefault(url, (now, status))
        hit = _cache.get(cache_key)
        if hit:
            return hit[1]
    claude, chatgpt, gemini_result = await asyncio.gather(
        check_claude(proxy_url),
        check_chatgpt(proxy_url),
        check_gemini(proxy_url) if gemini else _skip_check(),
    )
    status = UnlockStatus(
        claude=claude, chatgpt=chatgpt, gemini=gemini_result if gemini else ""
    )
    if use_cache:
        _cache[cache_key] = (now, status)
        _write_disk_cache(_cache)
    return status


async def _skip_check() -> str:
    return ""
