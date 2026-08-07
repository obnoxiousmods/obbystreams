"""Structural interfaces for the scheduler's collaborators.

:class:`~obbyschedule.scheduler.UfcScheduler` depends on *what* a provider and a
notifier do, not on the concrete ESPN/Discord classes. Typing the attributes as
protocols keeps the seam honest under ``ty`` and lets tests substitute stubs
without casts or ignores.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from .models import CalendarEntry, EventContext, NotifySettings, ScheduleSettings, UfcEvent
from .notify import EmbedBuilder


class ScheduleProvider(Protocol):
    """Source of the UFC calendar and per-card bout detail."""

    def with_settings(self, settings: ScheduleSettings) -> ScheduleProvider: ...

    async def fetch_calendar(self) -> tuple[CalendarEntry, ...]: ...

    async def fetch_event(self, day: date) -> UfcEvent | None: ...

class SourceResolver(Protocol):
    """The cockpit's source-discovery side, seen from the scheduler.

    ``refresh`` runs one discovery sweep for the given card; ``publish`` hands
    over (or clears, with ``None``) the context the scraper matches against so
    the background sweeps in between ticks stay event-aware too.
    """

    async def refresh(self, reason: str, context: EventContext | None = None) -> None: ...

    def publish(self, context: EventContext | None) -> None: ...


class Notifier(Protocol):
    """Delivers a built embed somewhere a human will see it."""

    builder: EmbedBuilder

    @property
    def active(self) -> bool: ...

    def with_settings(self, settings: NotifySettings) -> Notifier: ...

    async def send_embed(self, embed: dict[str, Any]) -> bool: ...
