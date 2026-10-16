"""Numeric bounds from the range labels data providers use: "51-200", "10,001+", "$1M - $10M".

The Saved tab compares the provider's size and revenue vocabulary against the numbers workspace
records store (``employee_count``, ``annual_revenue``). A label this cannot read returns ``None``
and is not applied, rather than guessed into a bound that would hide records.
"""
from __future__ import annotations

import re

_SUFFIX = {"k": 1_000, "m": 1_000_000, "mm": 1_000_000, "b": 1_000_000_000,
           "bn": 1_000_000_000, "t": 1_000_000_000_000}
_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|mm|m|bn|b|t|thousand|million|billion)?", re.I)
_WORDS = {"thousand": "k", "million": "m", "billion": "b"}


def _numbers(text: str) -> list[int]:
    out = []
    for digits, suffix in _NUMBER.findall(text):
        value = float(digits.replace(",", ""))
        unit = (suffix or "").lower()
        unit = _WORDS.get(unit, unit)
        out.append(int(round(value * _SUFFIX.get(unit, 1))))
    return out


def parse_range(label: str) -> tuple[int, int | None] | None:
    """``(low, high)`` inclusive; ``high`` is ``None`` for an open top ("10,001+")."""
    text = (label or "").strip().lower()
    if not text:
        return None
    numbers = _numbers(text)
    if not numbers:
        return None
    if re.search(r"(under|less than|below|<|up to)", text):
        return 0, numbers[0]
    if text.endswith("+") or re.search(r"(over|more than|above|plus|>)", text):
        return numbers[0], None
    if len(numbers) >= 2:
        low, high = sorted(numbers[:2])
        return low, high
    return numbers[0], numbers[0]
