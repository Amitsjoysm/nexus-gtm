"""Asyncio helpers that must behave identically across the Python versions we support.

`pyproject.toml` declares ``requires-python = ">=3.10"`` and CI tests that floor, while the
production image runs 3.11. Three call sites used ``asyncio.timeout``, which only exists from 3.11,
so on 3.10 they raised ``AttributeError: module 'asyncio' has no attribute 'timeout'``. Production
never saw it; CI did, on its first run that exercised the backend (2026-09-10).

The obvious fix — swap to ``asyncio.wait_for`` — has a trap that would have shipped silently. Every
call site catches the BUILTIN ``TimeoutError``. ``wait_for`` raises ``asyncio.TimeoutError``, and on
3.10 those are DIFFERENT classes; they were only unified in 3.11. So the timeout branch would be
skipped, the call would land in the site's broad ``except Exception``, and the log would read
"actor failed" instead of "exceeded its budget" — the wrong cause, reported with no error.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def run_with_timeout(aw: Awaitable[T], seconds: float) -> T:
    """Await ``aw`` for at most ``seconds``; on expiry raise the BUILTIN ``TimeoutError``.

    On 3.11+ this is exactly ``async with asyncio.timeout(seconds): return await aw`` — the awaitable
    runs in the CURRENT task, so production behaviour is unchanged by this helper existing. That is
    deliberate: the only goal here is to give 3.10 an equivalent, not to alter what 3.11 does.

    Takes an awaitable rather than being a context manager on purpose. A correct block-scoped timeout
    on 3.10 has to cancel the current task and then tell its own cancellation apart from somebody
    else's, and without ``Task.uncancel`` (3.11+) a timer that fires just as the block finishes leaves
    a stray cancellation to detonate at the caller's next ``await``. ``wait_for`` is the stdlib's
    tested answer to exactly that problem, and it needs an awaitable.
    """
    if sys.version_info >= (3, 11):
        async with asyncio.timeout(seconds):
            return await aw
    try:
        return await asyncio.wait_for(aw, seconds)
    except asyncio.TimeoutError:
        # Normalise to the builtin so `except TimeoutError:` means the same thing on every version.
        raise TimeoutError from None
