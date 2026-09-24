"""Irrigation plan generation with IANA timezone / DST handling.

The planner repeats a fixed daily set of *sessions* (local wall-clock
start, duration, target water) over a date range and turns them into
absolute (UTC) execution batches:

* Local times are interpreted in the requested IANA zone via
  :mod:`zoneinfo`, so cross-midnight sessions are represented by true
  elapsed-time UTC intervals.
* DST **gaps** (skipped local time) are resolved by shifting forward by
  the gap size (e.g. 02:30 that does not exist -> 03:30); DST
  **ambiguities** (repeated local time) resolve to the *first*
  occurrence (``fold=0``). Both events are reported explicitly on the
  batch instead of being hidden.
* Batches are sorted by UTC start time and adjacent ones whose gap is
  ``<= merge_gap_seconds`` (default 60 s, also covering overlap) are
  merged; merging may span midnight. Water of merged batches is summed,
  never truncated.
* Daily capacity is checked against the **unmerged** per-day water
  total (water is attributed to the local calendar day the session was
  scheduled for). Over-capacity days get an explicit ``shortfall_ml``;
  batches are still returned in full.
* **Suspended days** (``suspended_dates``) are local calendar dates for
  which no sessions are generated at all: the day is still echoed in the
  per-day results and in ``suspended_dates``, but its planned water is
  zero. Suspension only suppresses sessions scheduled *on* that local
  day; a cross-midnight batch that started on the previous local day is
  not cancelled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _format_local(dt_aware: datetime) -> str:
    """ISO-8601 local wall time with UTC offset, e.g. ``...T03:30:00-04:00``."""
    return dt_aware.strftime("%Y-%m-%dT%H:%M:%S%z")


def _format_offset(delta: timedelta) -> str:
    """Render a UTC offset timedelta as ``±HH:MM``."""
    total = int(delta.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _format_utc(dt_aware: datetime) -> str:
    """Instant in RFC 3339 UTC form (``...Z``)."""
    return dt_aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _RawBatch:
    """One session occurrence on one local day, before merging."""

    day: date
    requested_time: str
    start: datetime  # timezone-aware UTC instant
    end: datetime
    water_ml: float
    duration_minutes: float
    dst_event: str  # "" | "gap" | "duplicate"
    dst_note: str
    tz: ZoneInfo
    sources: list[dict] = field(default_factory=list)

    def to_source(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "requested_time": self.requested_time,
            "local_start": _format_local(self.start.astimezone(self.tz)),
            "dst_event": self.dst_event or None,
            "dst_note": self.dst_note or None,
            "water_ml": self.water_ml,
        }


def _resolve_local(
    day: date, local_time: time, tz: ZoneInfo
) -> tuple[datetime, str, str]:
    """Resolve a naive local datetime to an aware instant.

    Returns ``(aware_dt, event, note)`` where ``event`` is ``"gap"`` for
    a non-existent local time (spring-forward), ``"duplicate"`` for an
    ambiguous local time (fall-back), or ``""`` for a normal time.

    PEP 495 rules used here:

    * gap: ``fold=0`` maps the skipped time forward past the gap
      (e.g. 02:30 missing -> 03:30 local);
    * ambiguity: ``fold=0`` selects the *first* occurrence (pre-transition
      offset); the second occurrence exists one hour later in UTC.
    """
    naive = datetime.combine(day, local_time)
    dt_fold0 = naive.replace(tzinfo=tz, fold=0)
    dt_fold1 = naive.replace(tzinfo=tz, fold=1)
    off0, off1 = dt_fold0.utcoffset(), dt_fold1.utcoffset()

    if off0 != off1:
        # Round-trip through UTC: fold=0 lands on a different wall time
        # exactly when the requested time was skipped.
        if dt_fold0.astimezone(timezone.utc).astimezone(tz).replace(
            tzinfo=None
        ) != naive:
            # Normalize: the fold=0 representation still shows the skipped
            # wall time with the old offset; the real execution instant is
            # obtained via a UTC round-trip.
            shifted = dt_fold0.astimezone(timezone.utc).astimezone(tz)
            gap = off1 - off0  # positive on spring-forward
            note = (
                f"请求的本地时间 {naive.strftime('%Y-%m-%dT%H:%M:%S')} 不存在"
                f"（夏令时跳变，缺失 {int(gap.total_seconds() // 60)} 分钟），"
                f"已顺延到 {_format_local(shifted)} 执行。"
            )
            return dt_fold0, "gap", note

        # Ambiguous time: both folds round-trip; keep the first occurrence.
        delta = off0 - off1  # positive: second occurrence is that much later
        note = (
            f"{naive.strftime('%Y-%m-%dT%H:%M:%S')} 为夏令时结束时的重复本地时间，"
            f"采用第一次出现（UTC{_format_offset(off0)}）；第二次出现对应"
            f" UTC 晚 {int(delta.total_seconds() // 60)} 分钟。"
        )
        return dt_fold0, "duplicate", note

    return dt_fold0, "", ""


def build_plan(
    tz: ZoneInfo,
    start_date: date,
    end_date: date,
    sessions: list[tuple[time, str, float, float]],
    daily_capacity_ml: float | None,
    merge_gap_seconds: int = 60,
    suspended_dates: set[date] | None = None,
) -> dict:
    """Build the full irrigation plan structure.

    ``sessions`` is a list of ``(local_time, requested_text,
    duration_minutes, water_ml)`` tuples already validated by the
    Pydantic layer.  ``daily_capacity_ml=None`` skips the capacity
    checks entirely (used by the consumption forecast, which has no
    device-capacity input).  Dates in ``suspended_dates`` generate no
    sessions of their own (a cross-midnight batch begun the previous
    local day is unaffected).
    """
    tz_key = getattr(tz, "key", None) or str(tz)
    total_days = (end_date - start_date).days + 1
    suspended = set(suspended_dates or ())

    # ---- 1. one raw batch per (day, session) ---------------------------- #
    raw: list[_RawBatch] = []
    day_water: dict[date, float] = {}
    day_gap: dict[date, bool] = {}
    day_dup: dict[date, bool] = {}

    ordered = sorted(sessions, key=lambda s: s[0])
    for i in range(total_days):
        day = start_date + timedelta(days=i)
        day_water[day] = 0.0
        day_gap[day] = False
        day_dup[day] = False
        if day in suspended:
            # No sessions scheduled for a suspended local day; batches
            # begun on the previous local day (cross-midnight) are
            # generated by that day's iteration and remain untouched.
            continue
        for local_time, requested_text, duration_minutes, water_ml in ordered:
            start_aware, event, note = _resolve_local(day, local_time, tz)
            start_utc = start_aware.astimezone(timezone.utc)
            end_utc = start_utc + timedelta(minutes=duration_minutes)
            raw.append(
                _RawBatch(
                    day=day,
                    requested_time=requested_text,
                    start=start_utc,
                    end=end_utc,
                    water_ml=water_ml,
                    duration_minutes=duration_minutes,
                    dst_event=event,
                    dst_note=note,
                    tz=tz,
                )
            )
            day_water[day] += water_ml
            day_gap[day] |= event == "gap"
            day_dup[day] |= event == "duplicate"

    raw_count = len(raw)

    # ---- 2. sort in time and merge close/overlapping batches ------------ #
    raw.sort(key=lambda b: (b.start, b.end))
    merged: list[_RawBatch] = []
    for b in raw:
        b.sources = [b.to_source()]
        if not merged:
            merged.append(b)
            continue
        prev = merged[-1]
        gap = (b.start - prev.end).total_seconds()
        if gap <= merge_gap_seconds:
            if b.end > prev.end:
                prev.end = b.end  # also handles overlap: union of intervals
            prev.water_ml += b.water_ml
            prev.sources.extend(b.sources)
            # DST events on any constituent remain visible via sources;
            # surface the most notable one on the merged batch itself.
            if not prev.dst_event and b.dst_event:
                prev.dst_event = b.dst_event
                prev.dst_note = b.dst_note
        else:
            merged.append(b)

    # ---- 3. per-day summaries (capacity checked pre-merge) -------------- #
    day_batch_counts: dict[date, int] = {d: 0 for d in day_water}
    day_merged_batch_counts: dict[date, int] = {d: 0 for d in day_water}
    for b in raw:
        day_batch_counts[b.day] += 1
    for b in merged:
        days_touched = {date.fromisoformat(s["day"]) for s in b.sources}
        for d in days_touched:
            day_merged_batch_counts[d] += 1

    days_out: list[dict] = []
    over_days: list[dict] = []
    total_planned = 0.0
    for i in range(total_days):
        day = start_date + timedelta(days=i)
        is_suspended = day in suspended
        planned = day_water[day]
        total_planned += planned
        if daily_capacity_ml is None or is_suspended:
            over = False
        else:
            over = planned > daily_capacity_ml + 1e-9
        shortfall = round(planned - daily_capacity_ml, 6) if over else 0.0
        dst_flags = []
        if day_gap[day]:
            dst_flags.append("gap")
        if day_dup[day]:
            dst_flags.append("duplicate")
        days_out.append(
            {
                "date": day.isoformat(),
                "suspended": is_suspended,
                "batch_count_before_merge": day_batch_counts[day],
                "batch_count_after_merge": day_merged_batch_counts[day],
                "planned_water_ml": planned,
                "daily_capacity_ml": daily_capacity_ml,
                "within_capacity": not over,
                "shortfall_ml": shortfall,
                "dst_events": dst_flags,
            }
        )
        if over:
            over_days.append(
                {
                    "date": day.isoformat(),
                    "planned_water_ml": planned,
                    "daily_capacity_ml": daily_capacity_ml,
                    "shortfall_ml": shortfall,
                    "message": (
                        f"{day.isoformat()} 计划用水 {planned:g} mL，"
                        f"超出设备日容量 {daily_capacity_ml:g} mL，"
                        f"缺口 {shortfall:g} mL；批次未截断，请扩容或减少当日批次。"
                    ),
                }
            )

    # ---- 4. merged batch output ----------------------------------------- #
    batches_out: list[dict] = []
    for b in merged:
        start_local = b.start.astimezone(tz)
        end_local = b.end.astimezone(tz)
        batches_out.append(
            {
                "start_utc": _format_utc(b.start),
                "end_utc": _format_utc(b.end),
                "local_start": _format_local(start_local),
                "local_end": _format_local(end_local),
                "duration_seconds": int((b.end - b.start).total_seconds()),
                "water_ml": b.water_ml,
                "merged": len(b.sources) > 1,
                "batch_count_before_merge": len(b.sources),
                "dst_event": b.dst_event or None,
                "dst_note": b.dst_note or None,
                "crosses_midnight": (
                    start_local.date() != end_local.date()
                ),
                "sources": b.sources,
            }
        )

    return {
        "timezone": tz_key,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "days": total_days,
        "suspended_dates": sorted(d.isoformat() for d in suspended),
        "batch_count_before_merge": raw_count,
        "batch_count_after_merge": len(merged),
        "merge_gap_seconds": merge_gap_seconds,
        "planned_water_ml": total_planned,
        "daily_capacity_ml": daily_capacity_ml,
        "capacity_ok": not over_days,
        "capacity_violations": over_days,
        "day_results": days_out,
        "batches": batches_out,
    }
