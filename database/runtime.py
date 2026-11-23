"""Trusted scenario time shared by synchronous tools and their transactions."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Organization

_time = ContextVar("restoka_event_time", default=None)

@contextmanager
def event_time(now: datetime):
    """Bind an authenticated runtime timestamp; never expose this to LLM tools."""

    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("event_time requires a timezone-aware timestamp")

    token = _time.set(now)
    try:
        yield
    finally:
        _time.reset(token)


def current_event_time() -> datetime:
    """Require runtime evidence instead of consulting the machine clock."""
    now = _time.get()

    if now is None:
        raise ValueError("Bind trusted event_time(now) before writing business events")

    return now


def event_date(session, organization_id, now):
    return now.astimezone(ZoneInfo(session.get(Organization, organization_id).timezone)).date()
