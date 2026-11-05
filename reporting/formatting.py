from __future__ import annotations

from datetime import datetime, timezone

from analytics.derived import INSUFFICIENT_HISTORY, Comparison, DerivedValue


def fmt_int(value: int | float) -> str:
    return f"{round(value):,}"


def fmt_signed(value: int | float, decimals: int = 0) -> str:
    return f"{value:+,.{decimals}f}"


def fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def fmt_ratio_pct(ratio: float) -> str:
    """Raw ratio as a percentage for presentation only, e.g. 0.5 -> '50.00%'."""
    return f"{ratio * 100:.2f}%"


def fmt_pp(ratio_change: float) -> str:
    """Change between two ratios in percentage points, e.g. 0.0032 -> '+0.32 pp'."""
    return f"{ratio_change * 100:+.2f} pp"


def fmt_metric_value(value: int | float, value_format: str) -> str:
    return fmt_ratio_pct(value) if value_format == "percent" else fmt_int(value)


def fmt_metric_change(change: int | float, value_format: str) -> str:
    return fmt_pp(change) if value_format == "percent" else fmt_signed(change)


def fmt_time(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    return moment.astimezone(timezone.utc).strftime("%b %d, %Y %H:%M UTC")


def direction(value: int | float) -> str:
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


ARROWS = {"up": "↑", "down": "↓", "flat": "→"}


def fmt_comparison(comparison: Comparison) -> str:
    """One-line text, e.g. '+6,388 (+0.81%)' or 'Insufficient history'."""
    if not comparison.available:
        return comparison.change.reason or INSUFFICIENT_HISTORY
    text = fmt_signed(comparison.change.value)
    if comparison.change_pct.available:
        text += f" ({fmt_pct(comparison.change_pct.value)})"
    return text


def fmt_derived(value: DerivedValue) -> str:
    if not value.available:
        return value.reason or INSUFFICIENT_HISTORY
    if value.unit == "timestamp":
        return fmt_time(value.value)
    if value.unit == "people_per_day":
        return f"{fmt_signed(value.value, 1)} / day"
    if value.unit == "percent":
        return fmt_pct(value.value)
    return fmt_int(value.value)
