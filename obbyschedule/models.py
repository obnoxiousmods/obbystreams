"""Typed value objects for the UFC auto-schedule feature.

Everything in this module is pure data: frozen dataclasses, enums, and the
config-coercion classmethods that turn raw YAML into validated settings. There
is no I/O here, so every policy decision built on these types is unit-testable
without a network, an event loop, or ffmpeg.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
DEFAULT_WATCHER_URL = "https://fight.nswfiles.com/"
DEFAULT_INCLUDE_PATTERN = r"^UFC\s+(\d+|Fight Night)"
DEFAULT_EXCLUDE_PATTERN = r"Contender Series|Road to UFC"
DEFAULT_WARN_MINUTES: tuple[int, ...] = (1440, 720, 360, 120, 30)
DEFAULT_DISPLAY_TZ = "Canada/Pacific"
DEFAULT_STATE_PATH = "/etc/obbystreams/schedule_state.json"

#: Zones listed in the "Coming up" announcement, as (IANA name, display label).
#: Viewers are spread across North America, Europe and APAC, and Discord's
#: dynamic timestamps only localise for the *reader* — a static table is what
#: makes the post useful when someone quotes or screenshots it.
DEFAULT_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("America/Los_Angeles", "Los Angeles"),
    ("America/Denver", "Denver"),
    ("America/Chicago", "Chicago"),
    ("America/New_York", "New York"),
    ("Europe/London", "London"),
    ("Europe/Paris", "Paris"),
    ("Asia/Dubai", "Dubai"),
    ("Australia/Sydney", "Sydney"),
)

#: Card-segment names, applied by ordinal once the bouts are grouped by start time.
CARD_LABELS: dict[int, tuple[str, ...]] = {
    1: ("Main card",),
    2: ("Prelims", "Main card"),
    3: ("Early prelims", "Prelims", "Main card"),
}

EASTERN = "America/New_York"


@lru_cache(maxsize=32)
def compile_pattern(pattern: str) -> re.Pattern[str] | None:
    """Compile a config-supplied regex, returning None when it is empty or invalid.

    Cached because the scheduler recompiles the same include/exclude patterns on
    every tick. A bad pattern must never take the loop down, so this swallows
    ``re.error`` and the caller treats None as "no filter".
    """
    text = (pattern or "").strip()
    if not text:
        return None
    try:
        return re.compile(text, re.IGNORECASE)
    except re.error:
        return None


@lru_cache(maxsize=8)
def load_zone(name: str) -> ZoneInfo:
    """Resolve an IANA zone name, falling back to UTC when the tzdata entry is missing."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _as_int(raw: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _as_str(raw: Any, default: str = "") -> str:
    if raw is None:
        return default
    text = str(raw).strip()
    return text or default


def _as_int_tuple(raw: Any, default: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return default
    values: list[int] = []
    for item in raw:
        try:
            minutes = int(item)
        except (TypeError, ValueError):
            continue
        if minutes > 0:
            values.append(minutes)
    if not values:
        return default
    # Descending: the furthest-out warning fires first.
    return tuple(sorted(set(values), reverse=True))


def _section(raw: Any) -> Mapping[str, Any]:
    return raw if isinstance(raw, Mapping) else {}


def zone_exists(name: str) -> bool:
    """Whether an IANA zone resolves on this host's tzdata."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


def _as_timezones(raw: Any, default: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """Coerce the configured timezone list to (zone, label) pairs.

    Accepts either bare IANA strings or ``{zone, label}`` mappings. Unknown zones
    are dropped rather than raised — a typo in the config must not take the
    notification path down.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return default
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, Mapping):
            zone = _as_str(item.get("zone"))
            label = _as_str(item.get("label")) or zone.rsplit("/", 1)[-1].replace("_", " ")
        else:
            zone = _as_str(item)
            label = zone.rsplit("/", 1)[-1].replace("_", " ")
        if not zone or not zone_exists(zone):
            continue
        pairs.append((zone, label))
    return tuple(pairs) if pairs else default


class EventPhase(StrEnum):
    """Where the cockpit currently sits relative to the tracked event."""

    IDLE = "idle"
    PENDING = "pending"
    PRE_ROLL = "pre_roll"
    LIVE = "live"
    WRAPPING = "wrapping"
    FINISHED = "finished"


class SchedulerAction(StrEnum):
    """What the scheduler wants done to the managed encode this tick."""

    IDLE = "idle"
    START = "start"
    STOP = "stop"


class MilestoneKind(StrEnum):
    """Which flavour of Discord notification a milestone produces."""

    COMING_UP = "coming_up"
    WARNING = "warning"
    CARD_START = "card_start"
    EVENT_END = "event_end"


class StopReason(StrEnum):
    """Why the stream is currently down — drives the cockpit banner wording."""

    MANUAL = "manual"
    SCHEDULE = "schedule"


@dataclass(frozen=True, slots=True)
class CardSegment:
    """One broadcast segment of an event (early prelims / prelims / main card)."""

    start: datetime
    label: str
    bout_count: int
    completed_bouts: int

    @property
    def all_final(self) -> bool:
        return self.bout_count > 0 and self.completed_bouts >= self.bout_count

    @property
    def key(self) -> str:
        """Stable dedupe key for the card-start notification."""
        return self.start.isoformat()


@dataclass(frozen=True, slots=True)
class UfcEvent:
    """A single UFC card as reported by ESPN, reduced to what the scheduler needs."""

    event_id: str
    name: str
    short_name: str
    venue: str
    city: str
    cards: tuple[CardSegment, ...]
    is_final: bool
    main_event_bout: str | None = None
    main_event_winner: str | None = None

    @property
    def first_card_start(self) -> datetime | None:
        return self.cards[0].start if self.cards else None

    @property
    def last_card_start(self) -> datetime | None:
        return self.cards[-1].start if self.cards else None

    def phase(self, now: datetime, settings: ScheduleSettings) -> EventPhase:
        """Classify the event relative to ``now``.

        WRAPPING means every bout has been decided but the post-fight grace
        period has not elapsed yet, so the stream deliberately stays up.
        """
        first = self.first_card_start
        if first is None:
            return EventPhase.PENDING
        if self.is_final:
            return EventPhase.WRAPPING
        if now >= first:
            return EventPhase.LIVE
        if now >= first - timedelta(minutes=settings.lead_minutes):
            return EventPhase.PRE_ROLL
        return EventPhase.PENDING


@dataclass(frozen=True, slots=True)
class CalendarEntry:
    """A season-calendar row: enough to know when to start paying attention."""

    label: str
    start: datetime
    end: datetime | None

    @property
    def slug(self) -> str:
        return self.start.date().isoformat()


@dataclass(frozen=True, slots=True)
class Milestone:
    """A single, once-only notification to deliver."""

    kind: MilestoneKind
    key: str
    due: datetime
    label: str
    card: CardSegment | None = None
    #: Set for WARNING milestones — how far ahead of the first card they fire.
    minutes: int | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """The scheduler's verdict for one tick."""

    action: SchedulerAction
    reason: str
    phase: EventPhase = EventPhase.IDLE
    event_id: str | None = None

    @property
    def acts(self) -> bool:
        return self.action is not SchedulerAction.IDLE


@dataclass(frozen=True, slots=True)
class NotifySettings:
    """Discord webhook configuration."""

    enabled: bool = True
    webhook_url: str = ""
    watcher_url: str = DEFAULT_WATCHER_URL
    warn_minutes: tuple[int, ...] = DEFAULT_WARN_MINUTES
    notify_card_start: bool = True
    notify_event_end: bool = True
    notify_coming_up: bool = True
    timezones: tuple[tuple[str, str], ...] = DEFAULT_TIMEZONES
    #: A milestone whose moment passed longer ago than this is dropped rather
    #: than fired, so a redeploy or a long outage cannot spam a stale backlog.
    max_late_minutes: int = 20

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.webhook_url)

    @classmethod
    def from_config(cls, raw: Any) -> Self:
        section = _section(raw)
        return cls(
            enabled=_as_bool(section.get("enabled"), True),
            webhook_url=_as_str(section.get("discord_webhook_url")),
            watcher_url=_as_str(section.get("watcher_url"), DEFAULT_WATCHER_URL),
            warn_minutes=_as_int_tuple(section.get("warn_minutes"), DEFAULT_WARN_MINUTES),
            notify_card_start=_as_bool(section.get("notify_card_start"), True),
            notify_event_end=_as_bool(section.get("notify_event_end"), True),
            notify_coming_up=_as_bool(section.get("notify_coming_up"), True),
            timezones=_as_timezones(section.get("timezones"), DEFAULT_TIMEZONES),
            max_late_minutes=_as_int(section.get("max_late_minutes"), 20, minimum=1, maximum=720),
        )

    def to_config(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "discord_webhook_url": self.webhook_url,
            "watcher_url": self.watcher_url,
            "warn_minutes": list(self.warn_minutes),
            "notify_card_start": self.notify_card_start,
            "notify_event_end": self.notify_event_end,
            "notify_coming_up": self.notify_coming_up,
            "timezones": [{"zone": zone, "label": label} for zone, label in self.timezones],
            "max_late_minutes": self.max_late_minutes,
        }


@dataclass(frozen=True, slots=True)
class ScheduleSettings:
    """The ``schedule:`` config section, coerced and bounded."""

    enabled: bool = True
    scoreboard_url: str = DEFAULT_SCOREBOARD_URL
    include_pattern: str = DEFAULT_INCLUDE_PATTERN
    exclude_pattern: str = DEFAULT_EXCLUDE_PATTERN
    lead_minutes: int = 15
    end_grace_minutes: int = 20
    max_runtime_hours: int = 8
    calendar_refresh_seconds: int = 3600
    live_poll_seconds: int = 120
    display_timezone: str = DEFAULT_DISPLAY_TZ
    state_path: str = DEFAULT_STATE_PATH
    notify: NotifySettings = field(default_factory=NotifySettings)

    @classmethod
    def from_config(cls, raw: Any) -> Self:
        section = _section(raw)
        return cls(
            enabled=_as_bool(section.get("enabled"), True),
            scoreboard_url=_as_str(section.get("espn_scoreboard_url"), DEFAULT_SCOREBOARD_URL),
            include_pattern=_as_str(section.get("include_pattern"), DEFAULT_INCLUDE_PATTERN),
            exclude_pattern=_as_str(section.get("exclude_pattern"), DEFAULT_EXCLUDE_PATTERN),
            lead_minutes=_as_int(section.get("lead_minutes"), 15, minimum=0, maximum=720),
            end_grace_minutes=_as_int(section.get("end_grace_minutes"), 20, minimum=0, maximum=720),
            max_runtime_hours=_as_int(section.get("max_runtime_hours"), 8, minimum=1, maximum=24),
            calendar_refresh_seconds=_as_int(section.get("calendar_refresh_seconds"), 3600, minimum=300, maximum=86400),
            live_poll_seconds=_as_int(section.get("live_poll_seconds"), 120, minimum=30, maximum=3600),
            display_timezone=_as_str(section.get("display_timezone"), DEFAULT_DISPLAY_TZ),
            state_path=_as_str(section.get("state_path"), DEFAULT_STATE_PATH),
            notify=NotifySettings.from_config(section.get("notify")),
        )

    def to_config(self) -> dict[str, Any]:
        """Round-trip back to plain YAML-safe types for ``normalize_config``."""
        return {
            "enabled": self.enabled,
            "espn_scoreboard_url": self.scoreboard_url,
            "include_pattern": self.include_pattern,
            "exclude_pattern": self.exclude_pattern,
            "lead_minutes": self.lead_minutes,
            "end_grace_minutes": self.end_grace_minutes,
            "max_runtime_hours": self.max_runtime_hours,
            "calendar_refresh_seconds": self.calendar_refresh_seconds,
            "live_poll_seconds": self.live_poll_seconds,
            "display_timezone": self.display_timezone,
            "state_path": self.state_path,
            "notify": self.notify.to_config(),
        }

    @property
    def display_zone(self) -> ZoneInfo:
        return load_zone(self.display_timezone)

    @property
    def eastern_zone(self) -> ZoneInfo:
        return load_zone(EASTERN)

    def matches(self, label: str) -> bool:
        """Whether a calendar label is an event we stream (UFC cards, not Contender Series)."""
        include = compile_pattern(self.include_pattern)
        exclude = compile_pattern(self.exclude_pattern)
        if exclude is not None and exclude.search(label):
            return False
        if include is None:
            return True
        return bool(include.search(label))
