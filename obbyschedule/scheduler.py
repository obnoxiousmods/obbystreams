"""The auto-schedule loop: arm the encode for a UFC card, stand it down after.

The policy lives in two **pure, synchronous** methods — :meth:`UfcScheduler.decide`
and :meth:`UfcScheduler.due_milestones` — so the whole state machine is testable
without a network, an event loop, or ffmpeg. :meth:`UfcScheduler.run` is the thin
async shell that observes ESPN, persists state, and calls back into the cockpit.

The cockpit's own primitives (config load/save, the operator Stop switch, the
process lock) are **injected as callbacks**. This module never imports ``app``,
which keeps the dependency one-way and the import graph acyclic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .espn import EspnScheduleProvider
from .models import (
    CalendarEntry,
    Decision,
    EventPhase,
    Milestone,
    MilestoneKind,
    NotifySettings,
    SchedulerAction,
    ScheduleSettings,
    UfcEvent,
)
from .notify import DiscordNotifier
from .protocols import Notifier, ScheduleProvider
from .state import ScheduleState, ScheduleStateStore

logger = logging.getLogger("obbystreams.schedule")

ConfigLoader = Callable[[], Mapping[str, Any]]
StreamAction = Callable[[str], Awaitable[bool]]
RefreshAction = Callable[[str], Awaitable[None]]
EventLogger = Callable[[str, str], None]

#: How long after an event's scheduled start we still consider it "current"
#: when picking a target off the calendar.
CALENDAR_LOOKBACK = timedelta(hours=12)
#: Extra slack beyond the furthest warning, so the event detail is loaded before
#: the first countdown is due.
DETAIL_LEAD_SLACK = timedelta(minutes=60)
#: ESPN's calendar ``startDate`` is not always the first bout. For the 2026-07-25
#: card it reads 16:00Z (main card) while the prelims open at 13:00Z. Countdowns
#: anchor on the real first-bout time from the event detail, so the detail has to
#: be loaded well before the calendar time to avoid missing the 24h warning.
CALENDAR_SKEW = timedelta(hours=12)


class UfcScheduler:
    """Watches the UFC calendar and drives the managed encode + Discord notices."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        load_config: ConfigLoader,
        start_stream: StreamAction,
        stop_stream: StreamAction,
        refresh_sources: RefreshAction | None = None,
        event_log: EventLogger | None = None,
    ) -> None:
        self._client = client
        self._load_config = load_config
        self._start_stream = start_stream
        self._stop_stream = stop_stream
        self._refresh_sources = refresh_sources
        self._event_log = event_log
        self._settings = ScheduleSettings.from_config({})
        self._store = ScheduleStateStore(self._settings.state_path)
        self._state = ScheduleState()
        # Typed as protocols so the ESPN/Discord classes are swappable (and
        # stubbable in tests) without the scheduler knowing the difference.
        self._provider: ScheduleProvider = EspnScheduleProvider(client, self._settings)
        self._notifier: Notifier = DiscordNotifier(client, self._settings.notify)
        self._event: UfcEvent | None = None
        self._last_decision = Decision(SchedulerAction.IDLE, "not started yet")
        self._loaded = False
        # The API endpoints call ensure_loaded() too, so guard the first load
        # against interleaving with the loop's — a second load would otherwise
        # overwrite in-memory state that had not been persisted yet.
        self._load_lock = asyncio.Lock()

    # ---- accessors -----------------------------------------------------
    @property
    def settings(self) -> ScheduleSettings:
        return self._settings

    @property
    def state(self) -> ScheduleState:
        return self._state

    @property
    def notifier(self) -> Notifier:
        return self._notifier

    @property
    def last_decision(self) -> Decision:
        return self._last_decision

    @property
    def event(self) -> UfcEvent | None:
        """The card currently being tracked, if its detail has been loaded."""
        return self._event

    def bind(self, *, provider: ScheduleProvider | None = None, notifier: Notifier | None = None) -> None:
        """Swap a collaborator in place.

        The seam the replay tool and the tests use to run the real loop against
        recorded payloads and a dry-run notifier instead of ESPN and Discord.
        """
        if provider is not None:
            self._provider = provider
        if notifier is not None:
            self._notifier = notifier

    def _log(self, message: str, level: str = "info") -> None:
        logger.info("schedule: %s", message)
        if self._event_log is not None:
            with contextlib.suppress(Exception):
                self._event_log(f"schedule: {message}", level)

    # ---- settings plumbing ---------------------------------------------
    def reload_settings(self) -> ScheduleSettings:
        """Re-read the ``schedule:`` section, rebinding collaborators on change."""
        settings = ScheduleSettings.from_config(self._load_config().get("schedule"))
        if settings == self._settings:
            return settings
        if settings.state_path != self._settings.state_path:
            self._store = ScheduleStateStore(settings.state_path)
        self._settings = settings
        self._provider = self._provider.with_settings(settings)
        self._notifier = self._notifier.with_settings(settings.notify)
        return settings

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            self.reload_settings()
            self._store = ScheduleStateStore(self._settings.state_path)
            self._state = await self._store.load()
            self._loaded = True

    async def persist(self) -> None:
        await self._store.save(self._state)

    # ---- pure policy ---------------------------------------------------
    @staticmethod
    def select_target(calendar: tuple[CalendarEntry, ...], now: datetime) -> CalendarEntry | None:
        """The card we should currently care about: the one in progress, else the next one."""
        for entry in calendar:
            if entry.start + CALENDAR_LOOKBACK >= now:
                return entry
        return None

    @staticmethod
    def next_after(calendar: tuple[CalendarEntry, ...], moment: datetime) -> CalendarEntry | None:
        for entry in calendar:
            if entry.start > moment:
                return entry
        return None

    def decide(
        self,
        now: datetime,
        event: UfcEvent | None,
        state: ScheduleState,
        settings: ScheduleSettings,
        *,
        stream_running: bool,
    ) -> Decision:
        """Pure verdict for one tick — no I/O, no mutation.

        Ordering matters: the stop paths are evaluated before the start paths so
        an event that finished while we were mid-tick is stood down rather than
        immediately re-armed.
        """
        if not settings.enabled:
            return Decision(SchedulerAction.IDLE, "auto-schedule disabled")
        if event is None:
            return Decision(SchedulerAction.IDLE, "no event in range")

        first = event.first_card_start
        if first is None:
            return Decision(SchedulerAction.IDLE, "event has no published card times", event_id=event.event_id)

        phase = event.phase(now, settings)
        moment = now.timestamp()

        # --- stand-down paths: only ever unwind what this scheduler armed -----
        if state.started_by_scheduler and state.current_event_id == event.event_id:
            if state.started_at is not None and moment - state.started_at >= settings.max_runtime_hours * 3600:
                return Decision(
                    SchedulerAction.STOP,
                    f"max runtime failsafe ({settings.max_runtime_hours}h) reached",
                    phase,
                    event.event_id,
                )
            if event.is_final and state.final_seen_at is not None:
                grace = settings.end_grace_minutes * 60
                if moment - state.final_seen_at >= grace:
                    return Decision(SchedulerAction.STOP, "card finished and grace period elapsed", phase, event.event_id)
                return Decision(SchedulerAction.IDLE, "card finished; holding for post-fight grace", phase, event.event_id)
            if not stream_running:
                # The encode went down mid-card (a crash the watchdog could not
                # recover, or a scraper stand-down). Being level-triggered, the
                # scheduler re-arms rather than sitting idle until the card ends.
                return Decision(SchedulerAction.START, "re-arming: encode is down mid-card", phase, event.event_id)
            return Decision(SchedulerAction.IDLE, "event in progress", phase, event.event_id)

        # --- arming paths ----------------------------------------------------
        if event.event_id == state.handled_event_id:
            return Decision(SchedulerAction.IDLE, "event already stood down", phase, event.event_id)
        if event.event_id == state.suppressed_event_id:
            return Decision(SchedulerAction.IDLE, "operator stopped this event manually", phase, event.event_id)
        if event.is_final:
            return Decision(SchedulerAction.IDLE, "event already finished", EventPhase.FINISHED, event.event_id)
        if stream_running:
            return Decision(SchedulerAction.IDLE, "stream already running (not scheduler-owned)", phase, event.event_id)
        if now < first - timedelta(minutes=settings.lead_minutes):
            return Decision(SchedulerAction.IDLE, "waiting for the pre-roll window", phase, event.event_id)
        if moment - first.timestamp() >= settings.max_runtime_hours * 3600:
            return Decision(SchedulerAction.IDLE, "event window has already lapsed", phase, event.event_id)
        return Decision(SchedulerAction.START, f"{settings.lead_minutes}m pre-roll for {event.short_name}", phase, event.event_id)

    def should_adopt(
        self,
        now: datetime,
        event: UfcEvent,
        state: ScheduleState,
        settings: ScheduleSettings,
        *,
        stream_running: bool,
    ) -> bool:
        """Whether to take ownership of an already-running stream.

        Without this the feature only works if the operator remembers to press
        Stop first: a stream that was already up when the card began would never
        be scheduler-owned, so the stand-down — the whole point — would never
        fire and the encode would keep burning after the card, which is exactly
        the 24/7 behaviour this replaces.

        Adoption is deliberately narrow: only once the card is actually in its
        window, never for a card the operator vetoed or one already stood down.
        Turning auto-schedule off opts out entirely.
        """
        if not settings.enabled or not stream_running or state.started_by_scheduler:
            return False
        if event.is_final:
            return False
        if event.event_id in {state.suppressed_event_id, state.handled_event_id}:
            return False
        return event.phase(now, settings) in {EventPhase.PRE_ROLL, EventPhase.LIVE}

    def due_milestones(
        self,
        now: datetime,
        event: UfcEvent,
        state: ScheduleState,
        settings: ScheduleSettings,
    ) -> list[Milestone]:
        """Notifications whose moment has arrived and which have not been sent.

        A milestone more than ``max_late_minutes`` past due is dropped, not
        fired: after a redeploy or an outage we want silence, not a burst of
        stale countdowns.
        """
        notify: NotifySettings = settings.notify
        if not notify.active:
            return []

        candidates: list[Milestone] = []
        first = event.first_card_start
        if first is not None and not event.is_final:
            candidates.extend(
                Milestone(
                    kind=MilestoneKind.WARNING,
                    key=f"warn:{minutes}",
                    due=first - timedelta(minutes=minutes),
                    label=str(minutes),
                    minutes=minutes,
                )
                for minutes in notify.warn_minutes
            )
        if notify.notify_card_start:
            candidates.extend(
                Milestone(
                    kind=MilestoneKind.CARD_START,
                    key=f"card:{card.key}",
                    due=card.start,
                    label=card.label,
                    card=card,
                )
                for card in event.cards
            )
        if notify.notify_event_end and event.is_final and state.final_seen_at is not None:
            candidates.append(
                Milestone(
                    kind=MilestoneKind.EVENT_END,
                    key="end",
                    due=datetime.fromtimestamp(state.final_seen_at, UTC),
                    label="ended",
                )
            )

        cutoff = timedelta(minutes=notify.max_late_minutes)
        due = [
            milestone
            for milestone in candidates
            if milestone.due <= now and now - milestone.due <= cutoff and not state.has_fired(event.event_id, milestone.key)
        ]
        due.sort(key=lambda milestone: milestone.due)
        return due

    def sweep_stale(self, now: datetime, event: UfcEvent, state: ScheduleState, settings: ScheduleSettings) -> None:
        """Mark long-past milestones as fired so they can never fire late.

        Without this, a milestone that passed while the service was down would
        stay 'unfired' forever and the >max_late guard would re-evaluate it on
        every single tick.
        """
        notify = settings.notify
        cutoff = timedelta(minutes=notify.max_late_minutes)
        first = event.first_card_start
        if first is None:
            return
        for minutes in notify.warn_minutes:
            due = first - timedelta(minutes=minutes)
            if now - due > cutoff:
                state.mark_fired(event.event_id, f"warn:{minutes}")
        for card in event.cards:
            if now - card.start > cutoff:
                state.mark_fired(event.event_id, f"card:{card.key}")

    # ---- async orchestration -------------------------------------------
    async def refresh_calendar(self, *, force: bool = False) -> tuple[CalendarEntry, ...]:
        """Re-fetch the season calendar when the cache is stale."""
        age = time.time() - self._state.calendar_fetched_at
        if not force and self._state.calendar and age < self._settings.calendar_refresh_seconds:
            return self._state.calendar
        calendar = await self._provider.fetch_calendar()
        if calendar:
            self._state.calendar = calendar
            self._state.calendar_fetched_at = time.time()
            await self.persist()
        elif not self._state.calendar:
            logger.warning("espn calendar empty and no cached copy available")
        return self._state.calendar

    async def load_event(self, target: CalendarEntry) -> UfcEvent | None:
        """Fetch bout-level detail for a calendar entry.

        ESPN buckets the scoreboard by US-Eastern day, but a late card's UTC date
        rolls over, so both candidate days are tried and the closest match wins.
        """
        eastern_day = target.start.astimezone(self._settings.eastern_zone).date()
        days = [eastern_day]
        utc_day = target.start.astimezone(UTC).date()
        if utc_day != eastern_day:
            days.append(utc_day)
        for day in days:
            event = await self._provider.fetch_event(day)
            if event is None:
                continue
            first = event.first_card_start
            if first is None or abs((first - target.start).total_seconds()) <= 36 * 3600:
                return event
        return None

    async def announce_coming_up(self, event: UfcEvent, *, force: bool = False) -> bool:
        """Post the 'Coming up' card once per event.

        Fired when a card is first tracked — well ahead of the 24h countdown —
        so the channel learns what is next and at what local time. ``force``
        bypasses the once-per-event ledger for an operator-triggered send.
        """
        if not self._notifier.active:
            return False
        if not force and not self._settings.notify.notify_coming_up:
            return False
        if not force and self._state.has_fired(event.event_id, MilestoneKind.COMING_UP.value):
            return False
        embed = self._notifier.builder.coming_up(event)
        if not await self._notifier.send_embed(embed):
            return False
        self._state.mark_fired(event.event_id, MilestoneKind.COMING_UP.value)
        await self.persist()
        self._log(f"announced coming up: {event.name}", "ok")
        return True

    async def load_upcoming_event(self, now: datetime | None = None) -> UfcEvent | None:
        """Fetch bout detail for the next card on demand, regardless of the poll window.

        The loop only loads detail once a card is ~37h out; the cockpit's manual
        "Coming up" button needs it at any distance.
        """
        await self.ensure_loaded()
        self.reload_settings()
        moment = now or datetime.now(UTC)
        calendar = await self.refresh_calendar()
        target = self.select_target(calendar, moment)
        if target is None:
            return None
        # Always fetch fresh: this is an operator-triggered action, so one extra
        # request is cheaper than reasoning about whether the cache is stale.
        return await self.load_event(target)

    async def notify_due(self, now: datetime, event: UfcEvent) -> None:
        """Send every milestone that has come due, recording each as it lands."""
        for milestone in self.due_milestones(now, event, self._state, self._settings):
            next_entry = self.next_after(self._state.calendar, event.first_card_start or now)
            embed = self._notifier.builder.for_milestone(event, milestone, next_entry.label if next_entry else None)
            if await self._notifier.send_embed(embed):
                self._state.mark_fired(event.event_id, milestone.key)
                self._log(f"notified {milestone.kind.value} ({milestone.key}) for {event.short_name}")
                await self.persist()
            else:
                logger.warning("discord milestone %s for %s not delivered; will retry", milestone.key, event.event_id)

    async def apply(self, decision: Decision, event: UfcEvent) -> None:
        """Carry out a START/STOP verdict against the cockpit."""
        if decision.action is SchedulerAction.START:
            if await self._start_stream(f"auto-schedule: {decision.reason}"):
                self._state.begin_event(event.event_id, by_scheduler=True)
                self._state.suppressed_event_id = None
                await self.persist()
                self._log(f"armed for {event.name} ({decision.reason})", "ok")
                if self._refresh_sources is not None:
                    # The pre-roll is the one window where the spare provider
                    # connection is free, so kick source discovery immediately
                    # instead of waiting up to 15 minutes for the next sweep.
                    with contextlib.suppress(Exception):
                        await self._refresh_sources("auto-schedule pre-roll")
            else:
                logger.warning("auto-schedule start failed for %s", event.event_id)
        elif decision.action is SchedulerAction.STOP:
            if await self._stop_stream(f"auto-schedule: {decision.reason}"):
                self._state.finish_event(event.event_id)
                await self.persist()
                self._log(f"stood down after {event.name} ({decision.reason})", "ok")
            else:
                logger.warning("auto-schedule stop failed for %s", event.event_id)

    async def tick(self, *, stream_running: bool, now: datetime | None = None) -> float:
        """One full cycle. Returns how long to sleep before the next one."""
        await self.ensure_loaded()
        settings = self.reload_settings()
        moment = now or datetime.now(UTC)

        if not settings.enabled:
            self._last_decision = Decision(SchedulerAction.IDLE, "auto-schedule disabled")
            self._event = None
            return float(settings.live_poll_seconds)

        calendar = await self.refresh_calendar()
        target = self.select_target(calendar, moment)
        if target is None:
            self._event = None
            self._last_decision = Decision(SchedulerAction.IDLE, "no upcoming UFC event on the calendar")
            return float(settings.calendar_refresh_seconds)

        warn_lead = timedelta(minutes=max(settings.notify.warn_minutes, default=1440)) + DETAIL_LEAD_SLACK + CALENDAR_SKEW
        if moment < target.start - warn_lead:
            self._event = None
            self._last_decision = Decision(SchedulerAction.IDLE, f"next card is {target.label}")
            remaining = (target.start - warn_lead - moment).total_seconds()
            return max(60.0, min(float(settings.calendar_refresh_seconds), remaining))

        event = await self.load_event(target)
        self._event = event
        if event is None:
            self._last_decision = Decision(SchedulerAction.IDLE, f"ESPN has no bout detail for {target.label} yet")
            return float(settings.live_poll_seconds)

        # Point state at this card (a no-op after the first tick) so the operator
        # Stop path and the grace stamp always have an event to attach to.
        if self._state.track(event.event_id):
            await self.persist()
            # A newly tracked card is the moment to tell the channel what's next.
            if not event.is_final:
                await self.announce_coming_up(event)

        # Stamp the moment the card was first observed as decided; the post-fight
        # grace period is measured from here, not from the last bout's timestamp.
        if event.is_final and self._state.final_seen_at is None:
            self._state.final_seen_at = moment.timestamp()
            await self.persist()

        # Take ownership of a stream that was already up when the card started,
        # so the post-card stand-down still happens.
        if self.should_adopt(moment, event, self._state, settings, stream_running=stream_running):
            self._state.begin_event(event.event_id, by_scheduler=True)
            await self.persist()
            self._log(f"adopted the running stream for {event.name}; will stand down when it ends", "warn")

        self.sweep_stale(moment, event, self._state, settings)
        await self.notify_due(moment, event)

        decision = self.decide(moment, event, self._state, settings, stream_running=stream_running)
        self._last_decision = decision
        if decision.acts:
            await self.apply(decision, event)
        return self.next_wakeup(moment, event, settings)

    def next_wakeup(self, now: datetime, event: UfcEvent, settings: ScheduleSettings) -> float:
        """Sleep until the next thing that actually matters.

        The detail window opens ~37h out to absorb the calendar/first-bout skew,
        which is far too early to poll every two minutes. Once the pre-roll
        opens we switch to the tight cadence; before that we idle until the next
        unfired countdown is due.
        """
        first = event.first_card_start
        if first is None:
            return float(settings.live_poll_seconds)
        # Nothing left to watch for on a card that is done and stood down. Without
        # this we would keep polling ESPN every two minutes for the rest of the
        # lookback window — hundreds of pointless requests after every event.
        if event.is_final and self._state.handled_event_id == event.event_id:
            return float(settings.calendar_refresh_seconds)
        pre_roll = first - timedelta(minutes=settings.lead_minutes)
        if now >= pre_roll:
            return float(settings.live_poll_seconds)

        moments = [pre_roll]
        moments.extend(
            first - timedelta(minutes=minutes)
            for minutes in settings.notify.warn_minutes
            if not self._state.has_fired(event.event_id, f"warn:{minutes}")
        )
        upcoming = [(moment - now).total_seconds() for moment in moments if moment > now]
        if not upcoming:
            return float(settings.live_poll_seconds)
        return max(60.0, min(float(settings.calendar_refresh_seconds), min(upcoming)))

    async def run(self, stream_running: Callable[[], bool]) -> None:
        """Background loop; cancelled by the ASGI lifespan on shutdown."""
        while True:
            delay = 60.0
            try:
                delay = await self.tick(stream_running=stream_running())
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("schedule tick failed: %s", exc)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    # ---- introspection for the cockpit API ------------------------------
    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """Compact state for ``GET /api/schedule`` and the status payload."""
        moment = now or datetime.now(UTC)
        event = self._event
        settings = self._settings
        target = self.select_target(self._state.calendar, moment)
        next_entry = self.next_after(self._state.calendar, moment)

        payload: dict[str, Any] = {
            "enabled": settings.enabled,
            "notify_enabled": settings.notify.active,
            "lead_minutes": settings.lead_minutes,
            "end_grace_minutes": settings.end_grace_minutes,
            "phase": EventPhase.IDLE.value,
            "action": self._last_decision.action.value,
            "reason": self._last_decision.reason,
            "started_by_scheduler": self._state.started_by_scheduler,
            "suppressed_event_id": self._state.suppressed_event_id,
            "notifications_sent": self._state.fired_count(event.event_id if event else None),
            "calendar_size": len(self._state.calendar),
            "event": None,
            "next_event": None,
            "countdown_seconds": None,
            # True while the countdown is measured against the calendar's start
            # rather than the real first bout, which ESPN only exposes in the
            # event detail. The two can differ by hours, so the UI says so.
            "countdown_is_estimate": True,
        }

        upcoming = target or next_entry
        if upcoming is not None:
            payload["next_event"] = {
                "label": upcoming.label,
                "start": upcoming.start.isoformat(),
            }
            payload["countdown_seconds"] = max(0, int((upcoming.start - moment).total_seconds()))

        if event is not None:
            payload["phase"] = event.phase(moment, settings).value
            first = event.first_card_start
            payload["event"] = {
                "id": event.event_id,
                "name": event.name,
                "short_name": event.short_name,
                "venue": event.venue,
                "city": event.city,
                "is_final": event.is_final,
                "main_event": event.main_event_bout,
                "winner": event.main_event_winner,
                "first_card_start": first.isoformat() if first else None,
                "cards": [
                    {
                        "label": card.label,
                        "start": card.start.isoformat(),
                        "bouts": card.bout_count,
                        "completed": card.completed_bouts,
                        "all_final": card.all_final,
                    }
                    for card in event.cards
                ],
            }
            if first is not None:
                payload["countdown_seconds"] = max(0, int((first - moment).total_seconds()))
                payload["countdown_is_estimate"] = False
        return payload
