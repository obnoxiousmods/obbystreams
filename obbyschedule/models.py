"""Typed value objects for the UFC auto-schedule feature.

Everything in this module is pure data: frozen dataclasses, enums, and the
config-coercion classmethods that turn raw YAML into validated settings. There
is no I/O here, so every policy decision built on these types is unit-testable
without a network, an event loop, or ffmpeg.
"""

from __future__ import annotations

import re
import unicodedata
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

#: Tokens that carry no identifying power when matching a provider channel title
#: against an ESPN card. "UFC" is on every candidate by construction (it is a
#: required keyword upstream), and the connective words show up in every
#: "A vs. B" title, so treating them as event terms would match last week's card
#: just as happily as tonight's.
_STOPWORD_TERMS = frozenset(
    {
        "ufc",
        "fight",
        "night",
        "vs",
        "and",
        "the",
        "de",
        "da",
        "dos",
        "van",
        "von",
        "jr",
        "sr",
        "st",
        "main",
        "card",
        "prelims",
        "early",
    }
)

#: Surnames shorter than this are too collision-prone to use as a match term
#: (e.g. "Li", "Vo") — the event number and the other fighter still carry it.
MIN_TERM_LENGTH = 4


def normalize_match_text(text: str) -> str:
    """Fold text to lowercase ASCII for provider-title matching.

    Provider playlists are ASCII and inconsistently punctuated, while ESPN
    returns proper diacritics ("Medić", "Procházka"). Stripping combining marks
    is what makes ``medic`` match ``Medić`` instead of silently failing the
    event gate and holding the stream down through a whole card.
    """
    folded = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).strip()


def event_number_from(name: str) -> str | None:
    """The numbered-card ordinal in an event name ("UFC 330: ..." -> "330")."""
    match = re.search(r"\bufc\s+(\d{2,4})\b", normalize_match_text(name))
    return match.group(1) if match else None


def surname_terms(*names: str) -> tuple[str, ...]:
    """Distinctive surname tokens from fighter/event names, in first-seen order."""
    terms: list[str] = []
    for raw in names:
        for token in normalize_match_text(raw).split():
            if token in _STOPWORD_TERMS or len(token) < MIN_TERM_LENGTH or token.isdigit():
                continue
            if token not in terms:
                terms.append(token)
    return tuple(terms)


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
    #: "Gamrot vs. Salkilld" for each bout on this segment, in card order (the
    #: headliner last, as ESPN returns them). Empty for events parsed before this
    #: existed, so every consumer must treat it as optional.
    bouts: tuple[str, ...] = ()

    @property
    def all_final(self) -> bool:
        return self.bout_count > 0 and self.completed_bouts >= self.bout_count

    @property
    def key(self) -> str:
        """Stable dedupe key for the card-start notification."""
        return self.start.isoformat()


@dataclass(frozen=True, slots=True)
class EventContext:
    """What the source scraper needs to recognise *this* card in a provider playlist.

    The cockpit's private-IPTV scraper historically matched on the literal word
    "UFC" plus a ±30h date window, which cannot tell tonight's card from the one
    it selected last Saturday. Handing it the tracked event's identifying terms
    and real segment times is what makes "is this the right stream?" answerable.
    """

    event_id: str
    name: str
    short_name: str
    event_number: str | None
    terms: tuple[str, ...]
    #: (segment start, label) in UTC, straight from ESPN — replaces the scraper's
    #: hardcoded 5pm/7pm/9pm ET phase guesses while a card is tracked.
    segments: tuple[tuple[datetime, str], ...]
    first_card_start: datetime | None
    last_card_start: datetime | None
    phase: EventPhase = EventPhase.IDLE
    is_final: bool = False

    @property
    def active(self) -> bool:
        """Whether the card is close enough that source discovery should be event-gated."""
        return self.phase in {EventPhase.PRE_ROLL, EventPhase.LIVE, EventPhase.WRAPPING}

    def matches(self, text: str) -> tuple[bool, list[str]]:
        """Whether a provider title identifies this card, and which terms hit."""
        haystack = normalize_match_text(text)
        if not haystack:
            return False, []
        hits = [term for term in self.terms if re.search(rf"\b{re.escape(term)}\b", haystack)]
        if self.event_number and re.search(rf"\bufc\s+{self.event_number}\b", haystack):
            hits.append(f"ufc {self.event_number}")
        return bool(hits), hits

    def current_segment(self, now: datetime) -> tuple[datetime, str] | None:
        """The segment that has most recently started, if any."""
        started = [segment for segment in self.segments if segment[0] <= now]
        return started[-1] if started else None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection for ``/api/schedule`` and the cockpit banner."""
        return {
            "event_id": self.event_id,
            "name": self.name,
            "short_name": self.short_name,
            "event_number": self.event_number,
            "terms": list(self.terms),
            "segments": [{"start": start.isoformat(), "label": label} for start, label in self.segments],
            "first_card_start": self.first_card_start.isoformat() if self.first_card_start else None,
            "last_card_start": self.last_card_start.isoformat() if self.last_card_start else None,
            "phase": str(self.phase),
            "is_final": self.is_final,
        }


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
    #: Every athlete on the card, so a provider title naming the co-main still matches.
    fighters: tuple[str, ...] = ()

    @property
    def first_card_start(self) -> datetime | None:
        return self.cards[0].start if self.cards else None

    @property
    def last_card_start(self) -> datetime | None:
        return self.cards[-1].start if self.cards else None

    @property
    def progress_signature(self) -> str:
        """Fingerprint of how far the card has got, for stall detection."""
        return "|".join(f"{card.key}:{card.completed_bouts}/{card.bout_count}" for card in self.cards)

    def match_terms(self) -> tuple[str, ...]:
        """Distinctive tokens a provider title would carry for this card.

        Built from the event name (already surnames-only: "UFC Fight Night:
        Medić vs. Rodriguez") plus the surname of each competitor. First names
        are deliberately excluded — they collide across cards far more often
        than they help.
        """
        names = [self.name, self.short_name]
        for bout in (self.main_event_bout or "", *self.fighters):
            for side in re.split(r"\bvs\.?\b", bout, flags=re.IGNORECASE):
                tokens = normalize_match_text(side).split()
                if tokens:
                    names.append(tokens[-1])
        return surname_terms(*names)

    def context(self, now: datetime, settings: ScheduleSettings) -> EventContext:
        """Freeze what the source scraper needs to identify this card right now."""
        return EventContext(
            event_id=self.event_id,
            name=self.name,
            short_name=self.short_name,
            event_number=event_number_from(self.name) or event_number_from(self.short_name),
            terms=self.match_terms(),
            segments=tuple((card.start, card.label) for card in self.cards),
            first_card_start=self.first_card_start,
            last_card_start=self.last_card_start,
            phase=self.phase(now, settings),
            is_final=self.is_final,
        )

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
    #: Tighter cadence while the card is in its window but nothing is on air, so
    #: a feed that only appears at the bell is picked up in a minute, not five.
    acquisition_poll_seconds: int = 60
    #: Stand-down backstop for a card ESPN never marks final: this long after the
    #: last segment started with no bout completing, the card is over in practice.
    stall_hours: int = 6
    stall_idle_minutes: int = 45
    #: Whether source discovery must positively identify the tracked card. Off is
    #: the pre-2026-08 behaviour (any "UFC" channel in the date window will do).
    require_event_match: bool = True
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
            acquisition_poll_seconds=_as_int(section.get("acquisition_poll_seconds"), 60, minimum=30, maximum=900),
            stall_hours=_as_int(section.get("stall_hours"), 6, minimum=1, maximum=24),
            stall_idle_minutes=_as_int(section.get("stall_idle_minutes"), 45, minimum=5, maximum=720),
            require_event_match=_as_bool(section.get("require_event_match"), True),
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
            "acquisition_poll_seconds": self.acquisition_poll_seconds,
            "stall_hours": self.stall_hours,
            "stall_idle_minutes": self.stall_idle_minutes,
            "require_event_match": self.require_event_match,
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
