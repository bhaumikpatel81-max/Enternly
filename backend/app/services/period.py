"""
Period -> SQL date range helper.

Single source of truth — previously duplicated verbatim in reports_api.py
and kpi_api.py.
"""
from datetime import date, timedelta


def period_start(period: str, year: int) -> date:
    today = date.today()
    p = period.lower()
    if p == "weekly":
        return today - timedelta(days=today.weekday())
    if p == "monthly":
        return date(year, today.month, 1)
    if p == "quarterly":
        m = today.month
        qs = 1 if m <= 3 else 4 if m <= 6 else 7 if m <= 9 else 10
        return date(year, qs, 1)
    if p in ("half_yearly", "half-yearly"):
        return date(year, 4, 1) if 4 <= today.month <= 9 else date(year, 10, 1)
    # yearly / default
    return date(year, 1, 1)
