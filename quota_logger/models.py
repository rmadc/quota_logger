"""Shared value types used by provider adapters and the database layer."""
from dataclasses import dataclass


@dataclass(frozen=True)
class QuotaWindow:
    key: str
    label: str
    used_pct: float | None
    reset_at_epoch: int | None
    window_seconds: int | None
