"""Provider adapter contract and common quota value parsing."""
from __future__ import annotations

import datetime as dt
import math
from typing import Protocol

from ..models import QuotaWindow


class ProviderAdapter(Protocol):
    provider_key: str
    display_name: str

    def fetch_usage(self) -> dict: ...

    def parse_response(self, payload: dict) -> list[QuotaWindow]: ...


def first_dict(*values):
    return next((value for value in values if isinstance(value, dict)), None)


def first_value(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def numeric_percentage(value, provider: str, label: str) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{provider} {label} utilization was not numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{provider} {label} utilization was not finite")
    return result


def reset_epoch(value) -> int | None:
    """Normalize seconds, milliseconds, numeric strings, or ISO reset times."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return int(number / 1000) if number > 1e12 else int(number)
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
            if not math.isfinite(number):
                return None
            return int(number / 1000) if number > 1e12 else int(number)
        except ValueError:
            try:
                parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return int(parsed.timestamp())
    return None


def positive_duration(value) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if seconds > 0 else None
