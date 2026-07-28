"""ESPN scoreboard client — the cockpit's source of truth for the UFC calendar.

``https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard`` is public,
unauthenticated, and reachable from lucy. Two shapes matter:

* ``leagues[0].calendar`` — every card of the season as ``{label, startDate, endDate}``.
  ``startDate`` is the *earliest* segment (prelims), which is exactly the anchor
  the 15-minute pre-roll is measured against.
* ``events[].competitions[]`` — one row per bout, each with its own ``date`` and
  ``status.type.completed``. Distinct dates are the broadcast segments, and the
  completion flags are how we know the card is over.

Every parser here is a pure ``@staticmethod`` so the tests can run against
recorded JSON with no network.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import UTC, date, datetime
from typing import Any

import httpx

from .models import CARD_LABELS, CalendarEntry, CardSegment, ScheduleSettings, UfcEvent

logger = logging.getLogger("obbystreams.schedule.espn")

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
    async def fetch_scoreboard(self, day: date | None = None) -> dict[str, Any]:
        """GET the scoreboard, optionally for a specific day. Returns {} on any failure."""
        params = {"dates": day.strftime("%Y%m%d")} if day is not None else None
        try:
            response = await self._client.get(self._settings.scoreboard_url, params=params, timeout=15.0)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("espn scoreboard fetch failed (%s): %s", day or "current", exc)
            return {}
        return _mapping(payload)

    async def fetch_calendar(self) -> tuple[CalendarEntry, ...]:
        """The season calendar, filtered to the cards we actually stream."""
        return self.parse_calendar(await self.fetch_scoreboard(), self._settings)

    async def fetch_event(self, day: date) -> UfcEvent | None:
        """The card on ``day``, with per-bout status. None when ESPN has nothing matching."""
        return self.parse_events(await self.fetch_scoreboard(day), self._settings)

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
        )
