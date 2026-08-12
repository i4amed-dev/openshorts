"""Time: when Autopilot looks for content, and when finished clips go out.

Two schedules that are deliberately independent. Discovery is about compute (one
long video at a time on one Mac); publishing is about audience (three good slots
a day). Collapsing them means either the machine idles or the feed bursts.

Everything is computed with timezone-aware datetimes and stored as UTC. The
configured IANA zone is the *only* place local time exists, and it is applied at
the boundary. Naive ``datetime.now()`` arithmetic is what silently shifts a
whole content calendar by an hour twice a year.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def get_zone(name: str):
    if ZoneInfo is None:  # pragma: no cover
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def parse_hhmm(text: str) -> time:
    hour, minute = str(text).split(":")
    return time(int(hour), int(minute))


def local_slot_to_utc(day: date, hhmm: str, tz) -> datetime:
    """Turn a local wall-clock slot into the UTC instant it happens at.

    ``fold=0`` resolves the autumn repeated hour to its *first* occurrence, so a
    01:30 slot on a fall-back day fires once rather than twice. Spring-forward
    gaps are handled by the caller's de-duplication: a nonexistent local time
    normalises onto a real instant, which may coincide with the neighbouring
    slot, and the same instant must never be scheduled twice.
    """
    naive = datetime.combine(day, parse_hhmm(hhmm))
    local = naive.replace(tzinfo=tz, fold=0)
    return local.astimezone(timezone.utc).replace(microsecond=0)


def _enabled_days(config: Dict[str, Any]) -> set:
    days = (config.get("schedule") or {}).get("days_of_week") or list(range(7))
    return {int(d) for d in days if 0 <= int(d) <= 6}


def upcoming_slots(config: Dict[str, Any], times: Sequence[str], *,
                   after: datetime, limit: int = 50,
                   horizon_days: Optional[int] = None) -> List[datetime]:
    """The next ``limit`` UTC instants matching ``times`` on the enabled days.

    Strictly after ``after`` — a slot exactly at "now" belongs to the tick that
    already ran, and re-emitting it is how a restart double-fires a schedule.
    """
    if not times:
        return []
    tz = get_zone(config.get("timezone") or "UTC")
    days = _enabled_days(config)
    schedule = config.get("schedule") or {}
    horizon = int(horizon_days if horizon_days is not None else schedule.get("horizon_days", 14))

    after = after.astimezone(timezone.utc)
    local_today = after.astimezone(tz).date()
    out: List[datetime] = []
    seen: set = set()

    for offset in range(0, max(1, horizon) + 1):
        day = local_today + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        for hhmm in sorted(times):
            instant = local_slot_to_utc(day, hhmm, tz)
            if instant <= after or instant in seen:
                continue
            seen.add(instant)
            out.append(instant)
        if len(out) >= limit * 2:  # plenty; sort and trim below
            break

    out.sort()
    return out[:limit]


def next_discovery_time(config: Dict[str, Any], *, after: datetime) -> Optional[datetime]:
    times = (config.get("schedule") or {}).get("discovery_times") or []
    slots = upcoming_slots(config, times, after=after, limit=1)
    return slots[0] if slots else None


def discovery_is_due(config: Dict[str, Any], *, now: datetime,
                     last_discovery_at: Optional[datetime]) -> bool:
    """True when a discovery slot has passed since the last successful run.

    Missing a slot (the Mac was asleep) must not queue up several discoveries —
    one catch-up run is enough, and the next scheduled slot resumes the rhythm.
    """
    times = (config.get("schedule") or {}).get("discovery_times") or []
    if not times:
        return False
    reference = last_discovery_at
    if reference is None:
        # First ever run: only fire if a slot passed in the recent past, rather
        # than immediately on boot — enabling Autopilot at 14:00 should not kick
        # off a 03:00 run right away.
        reference = now - timedelta(hours=2)
    if reference >= now:
        return False
    # Was there a slot in (reference, now]? Walk forward from the reference.
    slots = upcoming_slots(config, times, after=reference, limit=5, horizon_days=3)
    return any(slot <= now for slot in slots)


def allocate_publish_slots(
    config: Dict[str, Any],
    *,
    count: int,
    now: datetime,
    taken: Sequence[datetime],
    earliest: Optional[datetime] = None,
) -> List[datetime]:
    """Pick the next ``count`` free publishing slots, in UTC.

    Respects, in order:
      * the configured publish times on enabled days,
      * ``max_posts_per_day`` counted in the operator's local timezone,
      * ``min_spacing_minutes`` between consecutive posts,
      * slots already held by another publish attempt.

    Returns fewer than ``count`` slots when the horizon runs out — the caller
    then leaves the remaining clips PENDING rather than inventing a slot.
    """
    schedule = config.get("schedule") or {}
    times = schedule.get("publish_times") or []
    if not times or count <= 0:
        return []

    tz = get_zone(config.get("timezone") or "UTC")
    max_per_day = int(schedule.get("max_posts_per_day", 3))
    spacing = timedelta(minutes=int(schedule.get("min_spacing_minutes", 0)))
    horizon = int(schedule.get("horizon_days", 14))

    floor = max(now, earliest or now)
    candidates = upcoming_slots(config, times, after=floor, limit=count * len(times) + 60,
                                horizon_days=horizon)

    taken_set = {dt.astimezone(timezone.utc).replace(microsecond=0) for dt in taken}
    per_day: Dict[date, int] = {}
    for dt in taken_set:
        per_day[dt.astimezone(tz).date()] = per_day.get(dt.astimezone(tz).date(), 0) + 1

    committed = sorted(taken_set)
    chosen: List[datetime] = []

    for slot in candidates:
        if len(chosen) >= count:
            break
        if slot in taken_set:
            continue
        local_day = slot.astimezone(tz).date()
        if per_day.get(local_day, 0) >= max_per_day:
            continue
        if spacing > timedelta(0) and any(abs(slot - other) < spacing for other in committed):
            continue
        chosen.append(slot)
        taken_set.add(slot)
        committed.append(slot)
        committed.sort()
        per_day[local_day] = per_day.get(local_day, 0) + 1

    return chosen


def apply_catch_up(config: Dict[str, Any], *, missed_slot: datetime,
                   now: datetime, taken: Sequence[datetime]) -> Optional[datetime]:
    """Decide what happens to a slot that passed while the machine was down.

    ``next_slot`` (default) re-queues into the next free slot — the point of a
    schedule is spacing, and dumping three overdue posts at once is exactly the
    burst it exists to prevent. ``skip`` drops it. ``immediate`` is opt-in and
    still spaced by ``min_spacing_minutes``.
    """
    policy = str((config.get("schedule") or {}).get("catch_up_policy") or "next_slot")
    if missed_slot > now:
        return missed_slot
    if policy == "skip":
        return None
    if policy == "immediate":
        spacing = timedelta(minutes=int((config.get("schedule") or {})
                                        .get("min_spacing_minutes", 0)))
        instant = now.astimezone(timezone.utc).replace(microsecond=0)
        committed = sorted(dt.astimezone(timezone.utc) for dt in taken)
        while any(abs(instant - other) < spacing for other in committed):
            instant = instant + spacing
        return instant
    slots = allocate_publish_slots(config, count=1, now=now, taken=taken)
    return slots[0] if slots else None


def local_day_bounds(config: Dict[str, Any], *, now: datetime) -> tuple:
    """UTC bounds of "today" in the operator's timezone.

    Daily caps are a human notion ("three posts a day"), so they must be counted
    against the local calendar day, not a UTC one that rolls over mid-evening.
    """
    tz = get_zone(config.get("timezone") or "UTC")
    local_now = now.astimezone(tz)
    start_local = datetime.combine(local_now.date(), time(0, 0), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc))


def describe_local(dt: Optional[datetime], config: Dict[str, Any]) -> Optional[str]:
    """ISO-8601 in the operator's timezone, for display only."""
    if dt is None:
        return None
    tz = get_zone(config.get("timezone") or "UTC")
    return dt.astimezone(tz).replace(microsecond=0).isoformat()
