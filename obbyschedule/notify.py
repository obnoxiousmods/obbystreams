"""Discord webhook delivery for schedule milestones.

:class:`EmbedBuilder` is pure (dict in, dict out) so embed shapes are asserted in
tests without touching the network. :class:`DiscordNotifier` owns delivery and is
deliberately failure-swallowing: a Discord outage must never take down the
scheduler loop or, worse, the live encode.

Card times are rendered with Discord's dynamic timestamp markup
(``<t:epoch:f>`` / ``<t:epoch:R>``) so every reader sees them in their own
timezone instead of a hardcoded ET/PT string.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfoNotFoundError

import httpx

from .models import CardSegment, Milestone, MilestoneKind, NotifySettings, UfcEvent, load_zone, zone_exists

logger = logging.getLogger("obbystreams.schedule.notify")

COLOR_WARNING: Final = 0xF5A623
COLOR_LIVE: Final = 0xED4245
COLOR_ENDED: Final = 0x2B2D31
COLOR_TEST: Final = 0x5865F2
COLOR_COMING_UP: Final = 0x3BA55D

WEBHOOK_USERNAME: Final = "ObbyStreams"
MAX_ATTEMPTS: Final = 3


def discord_timestamp(moment: datetime, style: str = "f") -> str:
    """Render a Discord dynamic timestamp, localised per-viewer by the client."""
    return f"<t:{int(moment.timestamp())}:{style}>"


def zone_abbrev(moment: datetime) -> str:
    """A readable zone abbreviation for a localised datetime.

    Many zones have no letter abbreviation in tzdata and ``%Z`` yields a bare
    offset like ``+04``; render those as ``UTC+4`` rather than a naked number.
    """
    abbrev = moment.strftime("%Z") or ""
    sign = abbrev[:1]
    if sign in {"+", "-"}:
        digits = abbrev[1:]
        if ":" in digits:
            hours, _, minutes = digits.partition(":")
        elif len(digits) == 4:
            # "+0545" (Kathmandu) — no separator, four digits.
            hours, minutes = digits[:2], digits[2:]
        else:
            hours, minutes = digits, ""
        stripped = hours.lstrip("0") or "0"
        return f"UTC{sign}{stripped}" + (f":{minutes}" if minutes and minutes != "00" else "")
    return abbrev


def timezone_table(moment: datetime, zones: Sequence[tuple[str, str]]) -> str:
    """Render one card time across several zones as an aligned code block.

    Discord's dynamic timestamps only localise for the person *reading* the
    message in Discord. A static table survives being quoted, screenshotted, or
    read by someone in a different region, which is the point of "Coming up".
    """
    rows: list[tuple[str, str]] = []
    for name, label in zones:
        # load_zone falls back to UTC for an unknown name, which would print a
        # confidently wrong time under the right city label. Skip instead.
        if not zone_exists(name):
            continue
        try:
            local = moment.astimezone(load_zone(name))
        except (ZoneInfoNotFoundError, ValueError):
            continue
        rows.append((label, f"{local.strftime('%a %d %b, %I:%M %p').replace(' 0', ' ')} {zone_abbrev(local)}".strip()))
    if not rows:
        return ""
    width = max(len(label) for label, _ in rows)
    body = "\n".join(f"{label.ljust(width)}  {when}" for label, when in rows)
    return f"```\n{body}\n```"


def humanize_minutes(minutes: int) -> str:
    """'1440' -> '24 hours', '30' -> '30 minutes'.

    Days only kick in past 48h: the headline warning is the 24-hour one, and
    "24 hours away" reads far better than "1 day away".
    """
    if minutes >= 2880 and minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day{'s' if days != 1 else ''}"
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


class EmbedBuilder:
    """Builds the rich embeds posted to the fight channel."""

    def __init__(self, settings: NotifySettings) -> None:
        self._settings = settings

    @property
    def watcher_url(self) -> str:
        return self._settings.watcher_url

    def _watch_field(self) -> dict[str, Any]:
        return {
            "name": "Watch",
            "value": f"**[fight.nswfiles.com]({self.watcher_url})** — free, no signup",
            "inline": False,
        }

    @staticmethod
    def _bout_lines(card: CardSegment, limit: int = 6) -> str:
        """The matchups on a segment, headliner first, bounded for embed width.

        ESPN returns bouts in running order, so the headliner is last; readers
        care about it first. Discord caps a field at 1024 characters and long
        cards would blow past that, so the tail is summarised rather than cut
        silently.
        """
        if not card.bouts:
            return ""
        ordered = list(reversed(card.bouts))
        shown = ordered[:limit]
        lines = [f"**{shown[0]}**", *shown[1:]] if shown else []
        remaining = len(ordered) - len(shown)
        if remaining > 0:
            lines.append(f"_+{remaining} more_")
        return "\n".join(lines)

    def _card_fields(self, event: UfcEvent) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = []
        for card in event.cards:
            value = f"{discord_timestamp(card.start)}\n{discord_timestamp(card.start, 'R')}"
            bouts = self._bout_lines(card)
            if bouts:
                value += f"\n\n{bouts}"
            fields.append({"name": card.label, "value": value, "inline": True})
        return fields

    def _footer(self, event: UfcEvent) -> dict[str, str]:
        location = " · ".join(part for part in (event.venue, event.city) if part)
        return {"text": location or "ObbyStreams auto-schedule"}

    def warning(self, event: UfcEvent, minutes: int) -> dict[str, Any]:
        """The 24h / 12h / 6h / 2h / 30m countdown embed."""
        first = event.first_card_start
        lead = humanize_minutes(minutes)
        description = f"**{event.main_event_bout or event.short_name}**"
        if first is not None:
            description += f"\n\nFirst card starts {discord_timestamp(first, 'R')} — {discord_timestamp(first)}"
        return {
            "title": f"⏳ {event.name} — {lead} away",
            "url": self.watcher_url,
            "description": description,
            "color": COLOR_WARNING,
            "fields": [*self._card_fields(event), self._watch_field()],
            "footer": self._footer(event),
        }

    def coming_up(self, event: UfcEvent) -> dict[str, Any]:
        """The 'Coming up' announcement: what's next, when, in every major timezone."""
        first = event.first_card_start
        lines = [f"**{event.main_event_bout or event.short_name}**"]
        location = " · ".join(part for part in (event.venue, event.city) if part)
        if location:
            lines.append(f"📍 {location}")
        if first is not None:
            lines.append(f"\nFirst card {discord_timestamp(first, 'R')} — {discord_timestamp(first)}")

        fields: list[dict[str, Any]] = []
        for card in event.cards:
            table = timezone_table(card.start, self._settings.timezones)
            bouts = f"{card.bout_count} bout{'s' if card.bout_count != 1 else ''}"
            value = table or discord_timestamp(card.start)
            matchups = self._bout_lines(card)
            if matchups:
                value += f"\n\n{matchups}"
            fields.append(
                {
                    "name": f"{card.label} · {bouts}",
                    "value": value,
                    "inline": False,
                }
            )
        fields.append(self._watch_field())

        return {
            "title": f"📅 Coming up — {event.name}",
            "url": self.watcher_url,
            "description": "\n".join(lines),
            "color": COLOR_COMING_UP,
            "fields": fields,
            "footer": {"text": "Times shown in your local zone by Discord, plus the table above."},
        }

    def card_start(self, event: UfcEvent, card: CardSegment) -> dict[str, Any]:
        """Fired the moment a broadcast segment goes live."""
        bouts = f"{card.bout_count} bout{'s' if card.bout_count != 1 else ''}"
        description = f"**{event.name}**\n{bouts} on this segment."
        if event.main_event_bout and card.label == "Main card":
            description += f"\n\n🥊 Main event: **{event.main_event_bout}**"
        matchups = self._bout_lines(card)
        if matchups:
            description += f"\n\n{matchups}"
        return {
            "title": f"🔴 LIVE NOW — {card.label}",
            "url": self.watcher_url,
            "description": description,
            "color": COLOR_LIVE,
            "fields": [self._watch_field()],
            "footer": self._footer(event),
            "timestamp": card.start.isoformat(),
        }

    def event_end(self, event: UfcEvent, next_event_label: str | None = None) -> dict[str, Any]:
        """The wrap-up embed, including the main-event result when ESPN has it."""
        description = "That's a wrap. The stream is returning to standby and will wake itself up for the next card."
        if event.main_event_winner:
            description = f"🏆 **{event.main_event_winner}** takes the main event.\n\n{description}"
        elif event.main_event_bout:
            description = f"Main event: **{event.main_event_bout}**\n\n{description}"
        fields: list[dict[str, Any]] = []
        if next_event_label:
            fields.append({"name": "Up next", "value": next_event_label, "inline": False})
        fields.append(self._watch_field())
        return {
            "title": f"🏁 {event.name} has ended",
            "url": self.watcher_url,
            "description": description,
            "color": COLOR_ENDED,
            "fields": fields,
            "footer": self._footer(event),
        }

    def for_acquisition_failure(self, event: UfcEvent, attempts: int) -> dict[str, Any]:
        """Operator alert: the card is under way and no feed has been verified.

        The cockpit deliberately holds rather than putting an unidentified feed
        on air, so this is the signal that a human needs to add a source — the
        one failure mode the hold-and-retry policy cannot fix by itself.
        """
        return {
            "title": f"⚠️ No verified source for {event.short_name}",
            "url": self.watcher_url,
            "description": (
                f"The card is live and the cockpit has tried {attempts} times to find a feed that "
                "matches it. Nothing is on air — the stream is holding rather than broadcasting an "
                "unidentified channel. A source may need to be added by hand."
            ),
            "color": COLOR_WARNING,
            "fields": [self._watch_field()],
            "footer": self._footer(event),
        }

    def for_milestone(self, event: UfcEvent, milestone: Milestone, next_event_label: str | None = None) -> dict[str, Any]:
        """Dispatch a milestone to its embed shape."""
        if milestone.kind is MilestoneKind.COMING_UP:
            return self.coming_up(event)
        if milestone.kind is MilestoneKind.CARD_START and milestone.card is not None:
            return self.card_start(event, milestone.card)
        if milestone.kind is MilestoneKind.EVENT_END:
            return self.event_end(event, next_event_label)
        return self.warning(event, milestone.minutes or 0)

    def test(self, next_event_label: str | None = None) -> dict[str, Any]:
        """A one-off embed for the operator's 'does the webhook work' check."""
        return {
            "title": "✅ ObbyStreams auto-schedule connected",
            "url": self.watcher_url,
            "description": (
                "This channel will get a heads-up 24h, 12h, 6h, 2h and 30m before every UFC card, "
                "a ping when each card goes live, and a wrap-up when it ends."
            ),
            "color": COLOR_TEST,
            "fields": [
                {"name": "Up next", "value": next_event_label or "Loading calendar…", "inline": False},
                self._watch_field(),
            ],
        }


class DiscordNotifier:
    """Posts embeds to a Discord webhook, with retries and rate-limit handling."""

    def __init__(self, client: httpx.AsyncClient, settings: NotifySettings) -> None:
        self._client = client
        self._settings = settings
        self.builder = EmbedBuilder(settings)

    def with_settings(self, settings: NotifySettings) -> DiscordNotifier:
        """Rebind to hot-reloaded settings, reusing the shared client."""
        return DiscordNotifier(self._client, settings)

    @property
    def active(self) -> bool:
        return self._settings.active

    async def send_embed(self, embed: dict[str, Any]) -> bool:
        return await self.send({"username": WEBHOOK_USERNAME, "embeds": [embed]})

    async def send(self, payload: dict[str, Any]) -> bool:
        """POST to the webhook. Returns success; never raises into the caller."""
        if not self.active:
            logger.debug("discord notify skipped: webhook disabled or unset")
            return False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(self._settings.webhook_url, json=payload, timeout=10.0)
            except httpx.HTTPError as exc:
                logger.warning("discord webhook attempt %d failed: %s", attempt, exc)
                await self._backoff(attempt)
                continue
            if response.status_code == 429:
                await asyncio.sleep(self._retry_after(response))
                continue
            if 200 <= response.status_code < 300:
                return True
            if 400 <= response.status_code < 500:
                # A malformed embed or a revoked webhook will never succeed; do
                # not burn retries on it.
                logger.error("discord webhook rejected (%s): %s", response.status_code, response.text[:300])
                return False
            logger.warning("discord webhook attempt %d got %s", attempt, response.status_code)
            await self._backoff(attempt)
        return False

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(min(8.0, 2.0**attempt))

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                return min(30.0, max(1.0, float(payload.get("retry_after", 1.0))))
        except ValueError:
            pass
        return 1.0
