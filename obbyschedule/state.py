"""Durable scheduler state.

Kept in a small JSON file next to the config so that a service restart — which
the cockpit needs for any code deploy — never re-fires a notification that
already went out, and never forgets that it was the scheduler (rather than a
human) that brought the stream up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from .espn import parse_iso
from .models import CalendarEntry

logger = logging.getLogger("obbystreams.schedule.state")

#: Keep the notification ledger from growing without bound.
MAX_TRACKED_EVENTS = 24


def _text_or_none(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _float_or_none(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class ScheduleState:
    """Everything the scheduler must remember across ticks and restarts."""

    calendar: tuple[CalendarEntry, ...] = ()
    calendar_fetched_at: float = 0.0
    #: Event the scheduler is currently tracking (armed, live, or wrapping).
    current_event_id: str | None = None
    #: True only when the scheduler itself started the encode — a manually
    #: started stream is never auto-stopped.
    started_by_scheduler: bool = False
    started_at: float | None = None
    #: When the card was first observed as decided; the grace period runs from here.
    final_seen_at: float | None = None
    #: Event the operator manually Stopped — do not re-arm it, but do arm the next one.
    suppressed_event_id: str | None = None
    #: Event the scheduler has already put back to standby.
    handled_event_id: str | None = None
    notified: dict[str, list[str]] = field(default_factory=dict)
    #: Bout-completion fingerprint of the tracked card and when it last changed.
    #: ESPN occasionally never flips a card's final flag; without a record of
    #: when the scoreboard last moved there is nothing to distinguish "the card
    #: is still going" from "the feed ended two hours ago and nobody told us".
    progress_signature: str | None = None
    progress_seen_at: float | None = None

    def note_progress(self, signature: str, *, moment: float) -> bool:
        """Record the card's completion fingerprint; True when it changed."""
        if self.progress_signature == signature and self.progress_seen_at is not None:
            return False
        self.progress_signature = signature
        self.progress_seen_at = moment
        return True

    def has_fired(self, event_id: str, key: str) -> bool:
        return key in self.notified.get(event_id, [])

    def mark_fired(self, event_id: str, key: str) -> None:
        fired = self.notified.setdefault(event_id, [])
        if key not in fired:
            fired.append(key)
        self._prune()

    def fired_count(self, event_id: str | None) -> int:
        if not event_id:
            return 0
        return len(self.notified.get(event_id, []))

    def track(self, event_id: str) -> bool:
        """Point state at an event without claiming ownership of the encode.

        Returns True when the tracked event actually changed, which is the
        signal to clear the per-event bookkeeping (grace stamp, suppression).
        """
        if self.current_event_id == event_id:
            return False
        # If we still own a running encode, carry that ownership onto the new id
        # rather than dropping it. Losing it would orphan the stream: the
        # stand-down branch only fires for events the scheduler started, so an
        # upstream id change would leave ffmpeg running until a human noticed.
        if not self.started_by_scheduler:
            self.started_at = None
        self.current_event_id = event_id
        self.final_seen_at = None
        self.progress_signature = None
        self.progress_seen_at = None
        if self.suppressed_event_id != event_id:
            self.suppressed_event_id = None
        return True

    def begin_event(self, event_id: str, *, by_scheduler: bool) -> None:
        self.current_event_id = event_id
        self.started_by_scheduler = by_scheduler
        self.started_at = time.time()
        self.final_seen_at = None

    def finish_event(self, event_id: str) -> None:
        self.handled_event_id = event_id
        self.current_event_id = None
        self.started_by_scheduler = False
        self.started_at = None
        self.final_seen_at = None

    def _prune(self) -> None:
        if len(self.notified) <= MAX_TRACKED_EVENTS:
            return
        for key in list(self.notified)[: len(self.notified) - MAX_TRACKED_EVENTS]:
            self.notified.pop(key, None)

    # ---- serialization -------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "calendar": [
                {
                    "label": entry.label,
                    "start": entry.start.isoformat(),
                    "end": entry.end.isoformat() if entry.end else None,
                }
                for entry in self.calendar
            ],
            "calendar_fetched_at": self.calendar_fetched_at,
            "current_event_id": self.current_event_id,
            "started_by_scheduler": self.started_by_scheduler,
            "started_at": self.started_at,
            "final_seen_at": self.final_seen_at,
            "suppressed_event_id": self.suppressed_event_id,
            "handled_event_id": self.handled_event_id,
            "notified": self.notified,
            "progress_signature": self.progress_signature,
            "progress_seen_at": self.progress_seen_at,
        }

    @classmethod
    def from_json(cls, payload: Any) -> Self:
        data = payload if isinstance(payload, dict) else {}
        calendar: list[CalendarEntry] = []
        raw_calendar = data.get("calendar")
        for row in raw_calendar if isinstance(raw_calendar, list) else []:
            item = row if isinstance(row, dict) else {}
            start = parse_iso(item.get("start"))
            label = str(item.get("label") or "").strip()
            if start is None or not label:
                continue
            calendar.append(CalendarEntry(label=label, start=start, end=parse_iso(item.get("end"))))

        notified: dict[str, list[str]] = {}
        raw_notified = data.get("notified")
        if isinstance(raw_notified, dict):
            for event_id, keys in raw_notified.items():
                if isinstance(keys, list):
                    notified[str(event_id)] = [str(key) for key in keys]

        return cls(
            calendar=tuple(sorted(calendar, key=lambda entry: entry.start)),
            calendar_fetched_at=_float_or_none(data.get("calendar_fetched_at")) or 0.0,
            current_event_id=_text_or_none(data.get("current_event_id")),
            started_by_scheduler=bool(data.get("started_by_scheduler", False)),
            started_at=_float_or_none(data.get("started_at")),
            final_seen_at=_float_or_none(data.get("final_seen_at")),
            suppressed_event_id=_text_or_none(data.get("suppressed_event_id")),
            handled_event_id=_text_or_none(data.get("handled_event_id")),
            notified=notified,
            progress_signature=_text_or_none(data.get("progress_signature")),
            progress_seen_at=_float_or_none(data.get("progress_seen_at")),
        )


class ScheduleStateStore:
    """Atomic JSON persistence for :class:`ScheduleState`.

    Writes go through a temp file plus ``os.replace`` (same pattern as
    ``save_config``) and run in a worker thread so a slow disk can never stall
    the event loop.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> ScheduleState:
        return await asyncio.to_thread(self._load_sync)

    async def save(self, state: ScheduleState) -> bool:
        return await asyncio.to_thread(self._save_sync, state)

    def _load_sync(self) -> ScheduleState:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ScheduleState()
        except OSError as exc:
            logger.warning("schedule state unreadable at %s: %s", self._path, exc)
            return ScheduleState()
        try:
            return ScheduleState.from_json(json.loads(raw))
        except ValueError as exc:
            logger.warning("schedule state corrupt at %s: %s", self._path, exc)
            return ScheduleState()

    def _save_sync(self, state: ScheduleState) -> bool:
        tmp = self._path.with_suffix(f"{self._path.suffix}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(state.to_json(), indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("schedule state write failed at %s: %s", self._path, exc)
            return False
        return True
