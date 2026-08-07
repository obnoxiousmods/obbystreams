"""ESPN scoreboard client — the cockpit's source of truth for the UFC calendar.

``https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard`` is public,
unauthenticated, and reachable from lucy. Two shapes matter:

* ``leagues[0].calendar`` — every card of the season as ``{label, startDate, endDate}``.
  ``startDate`` is the *earliest* segment (prelims), which is exactly the anchor
  the 10-minute pre-roll is measured against.
* ``events[].competitions[]`` — one row per bout, each with its own ``date`` and
  ``status.type.completed``. Distinct dates are the broadcast segments, and the
  completion flags are how we know the card is over.

Every parser here is a pure ``@staticmethod`` so the tests can run against
recorded JSON with no network.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import UTC, date, datetime
from typing import Any

import httpx

from .models import CARD_LABELS, CalendarEntry, CardSegment, ScheduleSettings, UfcEvent

logger = logging.getLogger("obbystreams.schedule.espn")

#: ESPN's public API is fetched with the library's own User-Agent rather than the
#: cockpit's.
#:
#: The shared httpx client sends a Firefox string because the stream-source
#: scrapers need one, and on 2026-08-04 ESPN began answering **403** to
#: browser-like agents on this endpoint while still serving plain library ones.
#: That silently killed every UFC alert: no scoreboard means no tracked card, no
#: milestones and no Discord post, and nothing else in the cockpit depends on
#: ESPN, so the only symptom was silence.
#:
#: Verified against the live endpoint: "curl/8.16.0" and "python-httpx/..." return
#: 200, while a Firefox UA, "obbystreams/1.0 (+https://s.obby.ca)" and any agent
#: carrying a URL return 403. It is also the honest thing to send - the scheduler
#: is a script reading a public JSON API, not a browser.
_ESPN_HEADERS = {"User-Agent": f"python-httpx/{httpx.__version__}", "Accept": "application/json"}

#: Agents to fall back through when ESPN refuses one. All three were verified
#: against the live endpoint; browser strings and anything carrying a URL are
#: refused, which is exactly what broke this in the first place.
_ESPN_USER_AGENTS: tuple[str, ...] = (
    f"python-httpx/{httpx.__version__}",
    "curl/8.16.0",
    "Wget/1.25.0",
)

#: Access refusals can differ by agent; transient gateway/rate-limit failures
#: also deserve bounded retries before the persisted event cache takes over.
_RETRYABLE_STATUS = frozenset({401, 403, 408, 425, 429, 502, 503, 504})


def _preferred_user_agents() -> tuple[str, ...]:
    """The agent that worked last, then the others."""
    current = FEED_HEALTH.user_agent
    if current and current in _ESPN_USER_AGENTS:
        return (current, *(a for a in _ESPN_USER_AGENTS if a != current))
    return _ESPN_USER_AGENTS


class FeedHealth:
    """Process-local health of the ESPN dependency."""

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.last_attempt_at: float | None = None
        self.last_success_at: float | None = None
        self.last_status_code: int | None = None
        self.last_error = ""
        #: The agent ESPN last accepted, so the next fetch tries it first.
        self.user_agent = ""

    def success(self, status_code: int, user_agent: str = "") -> None:
        self.consecutive_failures = 0
        self.last_attempt_at = time.time()
        self.last_success_at = self.last_attempt_at
        self.last_status_code = status_code
        self.last_error = ""
        if user_agent:
            self.user_agent = user_agent

    def failure(self, error: str, status_code: int | None) -> None:
        self.consecutive_failures += 1
        self.last_attempt_at = time.time()
        self.last_status_code = status_code
        self.last_error = error

    def snapshot(self) -> dict[str, Any]:
        stale = None if self.last_success_at is None else max(0, time.time() - self.last_success_at)
        return {
            "consecutive_failures": self.consecutive_failures,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "last_status_code": self.last_status_code,
            "last_error": self.last_error[:240] or None,
            "stale_seconds": round(stale) if stale is not None else None,
            "user_agent": self.user_agent,
        }


FEED_HEALTH = FeedHealth()

#: Bouts in these states never complete (scratched from the card), so they must
#: not hold the event open forever.
DEAD_STATUSES = frozenset({"STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_POSTPONED", "STATUS_ABANDONED"})


def parse_iso(value: Any) -> datetime | None:
    """Parse an ESPN timestamp into an aware UTC datetime.

    ESPN emits ``2026-07-25T13:00Z`` — no seconds — which ``fromisoformat``
    handles on 3.11+. Naive results are assumed UTC.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(raw: Any) -> str:
    return str(raw).strip() if raw is not None else ""


def _mapping(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _sequence(raw: Any) -> list[Any]:
    return raw if isinstance(raw, list) else []


def _bout_label(bout: dict[str, Any]) -> str:
    """"Gamrot vs. Salkilld" for one bout, or "" when ESPN has no names yet.

    Prefers the surname to keep an embed field readable, but ESPN does not always
    populate ``lastName`` on this endpoint - on the 2026-08-08 card it was absent
    for every athlete - so it falls back to the full display name rather than
    dropping the bout.
    """
    names: list[str] = []
    for entry in _sequence(_mapping(bout).get("competitors")):
        athlete = _mapping(_mapping(entry).get("athlete"))
        short = _text(athlete.get("lastName")) or _text(athlete.get("displayName"))
        if short:
            names.append(short)
    return " vs. ".join(names) if len(names) >= 2 else ""


def card_labels_for(count: int) -> tuple[str, ...]:
    """Name each broadcast segment; the final one is always the main card."""
    known = CARD_LABELS.get(count)
    if known is not None:
        return known
    if count <= 0:
        return ()
    return (*(f"Card {index + 1}" for index in range(count - 1)), "Main card")


class EspnScheduleProvider:
    """Fetches and parses the ESPN UFC scoreboard over a shared httpx client."""

    def __init__(self, client: httpx.AsyncClient, settings: ScheduleSettings) -> None:
        self._client = client
        self._settings = settings

    def with_settings(self, settings: ScheduleSettings) -> EspnScheduleProvider:
        """Return a provider bound to hot-reloaded settings, reusing the client."""
        return EspnScheduleProvider(self._client, settings)

    # ---- network -------------------------------------------------------
    #: ESPN's public API is fetched with the library's own User-Agent rather than
    #: the cockpit's.
    #:
    #: The shared httpx client sends a Firefox string because the stream-source
    #: scrapers need one, and on 2026-08-04 ESPN began answering **403** to
    #: browser-like agents on this endpoint while still serving plain library
    #: ones. That silently killed every UFC alert: no scoreboard means no tracked
    #: card, no milestones and no Discord post, and nothing else in the cockpit
    #: depends on ESPN so the only symptom was silence.
    #:
    #: Verified against the live endpoint: "curl/8.16.0" and "python-httpx/..."
    #: return 200, while a Firefox UA, "obbystreams/1.0 (+https://s.obby.ca)" and
    #: any agent carrying a URL return 403. This is also the honest thing to send
    #: - the scheduler is a script reading a public JSON API, not a browser.
    async def fetch_scoreboard(self, day: date | None = None) -> dict[str, Any]:
        """GET the scoreboard, optionally for a specific day. Returns {} on any failure."""
        params = {"dates": day.strftime("%Y%m%d")} if day is not None else None
        status_code: int | None = None
        last_error = ""
        # Try whichever agent worked last, then the rest. ESPN's agent filtering
        # is the one thing here that changes without warning - it is what caused
        # the 2026-08-04 blackout - so a refusal costs one retried request rather
        # than an outage nobody notices for three days.
        for agent in _preferred_user_agents():
            try:
                response = await self._client.get(
                    self._settings.scoreboard_url,
                    params=params,
                    timeout=15.0,
                    headers={**_ESPN_HEADERS, "User-Agent": agent},
                )
                status_code = response.status_code
                response.raise_for_status()
                payload = _mapping(response.json())
                if not payload or not any(key in payload for key in ("leagues", "events")):
                    raise ValueError("response did not contain ESPN scoreboard fields")
            except httpx.HTTPStatusError as exc:
                last_error = str(exc)
                if exc.response.status_code in _RETRYABLE_STATUS:
                    continue
                break
            except httpx.HTTPError as exc:
                last_error = str(exc)
                continue
            except ValueError as exc:
                last_error = str(exc)
                break
            else:
                if agent != FEED_HEALTH.user_agent:
                    logger.warning("espn scoreboard: using user-agent %r", agent)
                FEED_HEALTH.success(status_code, agent)
                return payload
        FEED_HEALTH.failure(last_error, status_code)
        logger.warning("espn scoreboard fetch failed (%s): %s", day or "current", last_error)
        return {}

    async def fetch_calendar(self) -> tuple[CalendarEntry, ...]:
        """The season calendar, filtered to the cards we actually stream."""
        return self.parse_calendar(await self.fetch_scoreboard(), self._settings)

    async def fetch_event(self, day: date) -> UfcEvent | None:
        """The card on ``day``, with per-bout status. None when ESPN has nothing matching."""
        return self.parse_events(await self.fetch_scoreboard(day), self._settings)

    async def fetch_events(self, day: date) -> tuple[UfcEvent, ...]:
        """Return every matching event so the scheduler can correlate exactly."""
        return self.parse_all_events(await self.fetch_scoreboard(day), self._settings)

    # ---- pure parsers --------------------------------------------------
    @staticmethod
    def parse_calendar(payload: dict[str, Any], settings: ScheduleSettings) -> tuple[CalendarEntry, ...]:
        """Extract ``leagues[0].calendar``, dropping rows the include/exclude patterns reject."""
        leagues = _sequence(payload.get("leagues"))
        if not leagues:
            return ()
        entries: list[CalendarEntry] = []
        for row in _sequence(_mapping(leagues[0]).get("calendar")):
            item = _mapping(row)
            label = _text(item.get("label"))
            start = parse_iso(item.get("startDate"))
            if not label or start is None or not settings.matches(label):
                continue
            entries.append(CalendarEntry(label=label, start=start, end=parse_iso(item.get("endDate"))))
        entries.sort(key=lambda entry: entry.start)
        return tuple(entries)

    @staticmethod
    def parse_events(payload: dict[str, Any], settings: ScheduleSettings) -> UfcEvent | None:
        """Pick the first matching event out of a scoreboard payload and parse it."""
        for row in _sequence(payload.get("events")):
            item = _mapping(row)
            name = _text(item.get("name")) or _text(item.get("shortName"))
            if not name or not settings.matches(name):
                continue
            event = EspnScheduleProvider.parse_event(item)
            if event is not None:
                return event
        return None

    @staticmethod
    def parse_all_events(payload: dict[str, Any], settings: ScheduleSettings) -> tuple[UfcEvent, ...]:
        events: list[UfcEvent] = []
        for row in _sequence(payload.get("events")):
            item = _mapping(row)
            name = _text(item.get("name")) or _text(item.get("shortName"))
            if not name or not settings.matches(name):
                continue
            event = EspnScheduleProvider.parse_event(item)
            if event is not None:
                events.append(event)
        return tuple(events)

    @staticmethod
    def parse_event(payload: dict[str, Any]) -> UfcEvent | None:
        """Reduce one ``events[]`` entry to a :class:`UfcEvent`.

        Bouts are grouped by start time into broadcast segments. The event counts
        as final when every bout that can still happen has completed — a bout
        scratched from the card (``STATUS_CANCELED``) is ignored rather than
        holding the event open forever. As a backstop, a completed main event
        also ends the card.
        """
        competitions = _sequence(payload.get("competitions"))
        if not competitions:
            return None

        buckets: OrderedDict[datetime, list[dict[str, Any]]] = OrderedDict()
        live_total = 0
        live_done = 0
        for row in competitions:
            bout = _mapping(row)
            start = parse_iso(bout.get("date"))
            if start is None:
                continue
            buckets.setdefault(start, []).append(bout)
            status = _mapping(_mapping(bout.get("status")).get("type"))
            if _text(status.get("name")).upper() in DEAD_STATUSES:
                continue
            live_total += 1
            if bool(status.get("completed")):
                live_done += 1
        if not buckets:
            return None

        ordered = sorted(buckets.items(), key=lambda pair: pair[0])
        labels = card_labels_for(len(ordered))
        cards = tuple(
            CardSegment(
                start=start,
                label=labels[index],
                bout_count=len(bouts),
                completed_bouts=sum(1 for bout in bouts if bool(_mapping(_mapping(bout.get("status")).get("type")).get("completed"))),
                bouts=tuple(_bout_label(bout) for bout in bouts if _bout_label(bout)),
            )
            for index, (start, bouts) in enumerate(ordered)
        )

        main_bout = ordered[-1][1][-1]
        main_status = _mapping(_mapping(main_bout.get("status")).get("type"))
        main_completed = bool(main_status.get("completed"))
        is_final = (live_total > 0 and live_done >= live_total) or main_completed

        venue = _mapping(main_bout.get("venue"))
        address = _mapping(venue.get("address"))
        city_parts = [_text(address.get("city")), _text(address.get("country"))]

        names: list[str] = []
        winner = ""
        for entry in _sequence(main_bout.get("competitors")):
            competitor = _mapping(entry)
            athlete = _text(_mapping(competitor.get("athlete")).get("displayName"))
            if not athlete:
                continue
            names.append(athlete)
            if competitor.get("winner"):
                winner = athlete

        # Every athlete on the card, not just the headliners: provider channels
        # for the prelims blocks are often titled after a co-main or a featured
        # undercard bout, and those titles are what the source matcher reads.
        fighters: list[str] = []
        for row in competitions:
            for entry in _sequence(_mapping(row).get("competitors")):
                athlete = _text(_mapping(_mapping(entry).get("athlete")).get("displayName"))
                if athlete and athlete not in fighters:
                    fighters.append(athlete)

        return UfcEvent(
            event_id=_text(payload.get("id")),
            name=_text(payload.get("name")),
            short_name=_text(payload.get("shortName")) or _text(payload.get("name")),
            venue=_text(venue.get("fullName")),
            city=", ".join(part for part in city_parts if part),
            cards=cards,
            is_final=is_final,
            main_event_bout=" vs. ".join(names) if names else None,
            main_event_winner=winner or None,
            fighters=tuple(fighters),
        )
