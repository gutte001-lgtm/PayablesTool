"""
dates.py -- business-day arithmetic for the Phase 3.5 follow-up workspace.

Pure functions, no DB. Used for SLA/staleness aging (days since last activity,
yellow at 5 BD, red at 10 BD).

SIMPLIFICATION (Phase 3.5): "business day" = Monday-Friday. There is NO holiday
calendar yet -- a bank holiday counts as a business day. When a holiday table
lands (likely alongside Phase 4 pay-run scheduling), only business_days_between
needs to learn about it; every caller goes through this one function, so adding
a holiday set here will not surprise anyone downstream.
"""

from datetime import date, datetime


def _as_date(v):
    """Coerce a date/datetime/ISO-string ('YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS')
    to a date. Returns None if it can't be parsed."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])   # leading 'YYYY-MM-DD' of any ISO ts
    except ValueError:
        return None


def business_days_between(start, end):
    """Count business days (Mon-Fri) in the half-open interval [start, end) --
    start counts, end does not. Same convention as numpy.busday_count.

    Accepts dates, datetimes, or ISO strings. Returns 0 if end <= start or
    either side is unparseable. So Friday -> Monday is 1 (only Friday is in
    [Fri, Mon)), and same-day is 0.
    """
    start = _as_date(start)
    end = _as_date(end)
    if start is None or end is None or end <= start:
        return 0
    days = (end - start).days
    full_weeks, extra = divmod(days, 7)
    bd = full_weeks * 5
    wd = start.weekday()                     # Mon=0 .. Sun=6
    for i in range(extra):
        if (wd + i) % 7 < 5:                 # Mon-Fri
            bd += 1
    return bd


def business_days_ago(ts, today=None):
    """Business days from a past timestamp `ts` up to `today` (default: today).
    Convenience wrapper over business_days_between for "days since last
    activity". Returns 0 for today/future or unparseable input."""
    return business_days_between(ts, today or date.today())
