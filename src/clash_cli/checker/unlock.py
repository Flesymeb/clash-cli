"""AI service unlock detection (based on clash-verge-rev media_unlock_checker)."""

from __future__ import annotations

import asyncio
import re

import httpx

from clash_cli.api.models import UnlockStatus

CLAUDE_BLOCKED = frozenset({"AF", "BY", "CN", "CU", "HK", "IR", "KP", "MO", "RU", "SY"})
GEMINI_BLOCKED = frozenset({"CHN", "RUS", "BLR", "CUB", "IRN", "PRK", "SYR", "HKG", "MAC"})

GEMINI_MARKER = ',2,1,200,"'


async def check_claude(proxy_url: str) -> str:
    """Check Claude unlock via cdn-cgi/trace. Returns 'ok:XX', 'blocked:XX', or 'fail'."""
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=8.0) as client:
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
        async with httpx.AsyncClient(proxy=proxy_url, timeout=8.0) as client:
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
        async with httpx.AsyncClient(proxy=proxy_url, timeout=8.0) as client:
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


async def check_all(proxy_url: str) -> UnlockStatus:
    """Run all unlock checks concurrently."""
    claude, chatgpt, gemini = await asyncio.gather(
        check_claude(proxy_url),
        check_chatgpt(proxy_url),
        check_gemini(proxy_url),
    )
    return UnlockStatus(claude=claude, chatgpt=chatgpt, gemini=gemini)
