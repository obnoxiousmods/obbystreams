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

from .espn import FEED_HEALTH, EspnScheduleProvider
from .models import (
    CalendarEntry,
    Decision,
    EventContext,
    EventPhase,
    Milestone,
    MilestoneKind,
    NotifySettings,
    SchedulerAction,
    ScheduleSettings,
    StartResult,
    StartStatus,
    UfcEvent,
    normalize_match_text,
)
from .notify import DiscordNotifier
from .protocols import Notifier, ScheduleProvider, SourceResolver
from .state import ScheduleState, ScheduleStateStore

#: How dark the feed must go before it is worth waking anyone: two consecutive
#: failures of the hourly calendar refresh plus half an hour of staleness is a
#: real outage rather than one flaky request.
FEED_DARK_FAILURES = 2
FEED_DARK_SECONDS = 1800

logger = logging.getLogger("obbystreams.schedule")

ConfigLoader = Callable[[], Mapping[str, Any]]
StreamAction = Callable[[str], Awaitable[bool | StartResult]]
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
#: Arming attempts that ended with the encode still down before we tell the
#: channel that no usable feed has been found for a card that is under way.
ACQUISITION_ALERT_ATTEMPTS = 3


class UfcScheduler:
    """Watches the UFC calendar and drives the managed encode + Discord notices."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        load_config: ConfigLoader,
        start_stream: StreamAction,
        stop_stream: StreamAction,
        sources: SourceResolver | None = None,
        event_log: EventLogger | None = None,
    ) -> None:
        self._client = client
        self._load_config = load_config
        self._start_stream = start_stream
        self._stop_stream = stop_stream
        self._sources = sources
        self._event_log = event_log
        self._context: EventContext | None = None
        # Consecutive arming attempts that left the encode down — the card is in
        # its window but no feed has been verified as *this* event yet.
        self._arm_attempts = 0
        self._acquisition_alerted: str | None = None
        self._settings = ScheduleSettings.from_config({})
        self._store = ScheduleStateStore(self._settings.state_path)
        self._state = ScheduleState()
        # Typed as protocols so the ESPN/Discord classes are swappable (and
        # stubbable in tests) without the scheduler knowing the difference.
        self._provider: ScheduleProvider = EspnScheduleProvider(client, self._settings)
        self._notifier: Notifier = DiscordNotifier(client, self._settings.notify)
        self._feed_alert_sent = False
        self._event: UfcEvent | None = None
        self._using_cached_event = False
        self._last_start_result: StartResult | None = None
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
        moment = now.timestamp()
        # Ownership and the absolute event deadline are persisted precisely so
        # an ESPN outage cannot strand ffmpeg online forever.
        derived_hard_stop = (
            event.first_card_start + timedelta(hours=settings.max_runtime_hours)
        ).timestamp() if event is not None and event.first_card_start is not None else None
        hard_stop_at = state.hard_stop_at or derived_hard_stop
        if state.started_by_scheduler and hard_stop_at is not None and moment >= hard_stop_at:
            event_id = event.event_id if event is not None else state.current_event_id
            phase = event.phase(now, settings) if event is not None else EventPhase.LIVE
            return Decision(
                SchedulerAction.STOP,
                f"max runtime failsafe: absolute event-window limit ({settings.max_runtime_hours}h) reached",
                phase,
                event_id,
            )
        if event is None:
            return Decision(SchedulerAction.IDLE, "ESPN unavailable; retaining last lifecycle state")

        first = event.first_card_start
        if first is None:
            return Decision(SchedulerAction.IDLE, "event has no published card times", event_id=event.event_id)

        phase = event.phase(now, settings)
        # --- stand-down paths: only ever unwind what this scheduler armed -----
        if state.started_by_scheduler and state.current_event_id == event.event_id:
            if event.is_final and state.final_seen_at is not None:
                grace = settings.end_grace_minutes * 60
                if moment - state.final_seen_at >= grace:
                    return Decision(SchedulerAction.STOP, "card finished and grace period elapsed", phase, event.event_id)
                return Decision(SchedulerAction.IDLE, "card finished; holding for post-fight grace", phase, event.event_id)
            if self.card_stalled(now, event, state, settings):
                return Decision(
                    SchedulerAction.STOP,
                    f"card stalled: no bout decided for {settings.stall_idle_minutes}m and ESPN never marked it final",
                    phase,
                    event.event_id,
                )
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

    @staticmethod
    def card_stalled(
        now: datetime,
        event: UfcEvent,
        state: ScheduleState,
        settings: ScheduleSettings,
    ) -> bool:
        """Whether a card ESPN never marked final has clearly finished anyway.

        The 8h ``max_runtime_hours`` failsafe already exists, but it is far too
        blunt to be the only backstop: a card that ends at the four-hour mark
        with a stuck scoreboard would keep the encode (and the provider slot)
        burning for another four. Requires *both* a long time since the last
        segment opened and a quiet scoreboard, so a genuinely slow card with
        fights still landing is never cut off mid-broadcast.
        """
        last = event.last_card_start
        if event.is_final or last is None or state.progress_seen_at is None:
            return False
        if now < last + timedelta(hours=settings.stall_hours):
            return False
        return now.timestamp() - state.progress_seen_at >= settings.stall_idle_minutes * 60

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
    async def _alert_if_feed_is_dark(self) -> None:
        """Say something in Discord when the scoreboard stops answering.

        The 2026-08-04 blackout lasted three days because it was *silent*: ESPN
        began refusing the cockpit's user-agent, nothing else here reads ESPN, and
        the only trace was a state file that quietly stopped being written. A
        scheduler that cannot see the calendar cannot warn anyone about a card, so
        it has to be able to warn about itself.

        Fires once per dark spell and re-arms only after a successful read, so a
        broken feed cannot become its own spam.
        """
        health = FEED_HEALTH.snapshot()
        failures = int(health.get("consecutive_failures") or 0)
        stale = health.get("stale_seconds")
        if failures == 0:
            self._feed_alert_sent = False
            return
        dark = failures >= FEED_DARK_FAILURES and (stale is None or stale >= FEED_DARK_SECONDS)
        if not dark or self._feed_alert_sent or not self._notifier.active:
            return
        self._feed_alert_sent = True
        when = "never since boot" if stale is None else f"{stale / 3600:.1f}h ago"
        await self._notifier.send_embed(
            {
                "title": "\u26a0\ufe0f UFC schedule feed is down",
                "description": (
                    f"The ESPN scoreboard has refused **{failures}** consecutive "
                    f"requests. Last successful read: **{when}**.\n\n"
                    "Card alerts and the auto-schedule are blind until this "
                    "recovers. Sent once per outage."
                ),
                "color": 0xE74C3C,
                "fields": [
                    {
                        "name": "Last error",
                        "value": f"```{str(health.get('last_error') or 'unknown')[:400]}```",
                        "inline": False,
                    }
                ],
            }
        )

    async def refresh_calendar(self, *, force: bool = False) -> tuple[CalendarEntry, ...]:
        """Re-fetch the season calendar when the cache is stale."""
        age = time.time() - self._state.calendar_fetched_at
        if not force and self._state.calendar and age < self._settings.calendar_refresh_seconds:
            return self._state.calendar
        calendar = await self._provider.fetch_calendar()
        if isinstance(self._provider, EspnScheduleProvider):
            await self._alert_if_feed_is_dark()
            health = FEED_HEALTH.snapshot()
            self._state.espn_last_attempt_at = float(health.get("last_attempt_at") or time.time())
            self._state.espn_consecutive_failures = int(health.get("consecutive_failures") or 0)
            self._state.espn_last_error = str(health.get("last_error") or "") or None
            if health.get("last_success_at"):
                self._state.espn_last_success_at = float(health["last_success_at"])
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
        candidates: list[UfcEvent] = []
        for day in days:
            fetch_many = getattr(self._provider, "fetch_events", None)
            if callable(fetch_many):
                candidates.extend(await fetch_many(day))
            else:
                event = await self._provider.fetch_event(day)
                if event is not None:
                    candidates.append(event)
        if candidates:
            target_terms = set(normalize_match_text(target.label).split())

            def rank(event: UfcEvent) -> tuple[int, float]:
                event_terms = set(normalize_match_text(event.name).split())
                overlap = len(target_terms & event_terms)
                first = event.first_card_start or target.start
                return (-overlap, abs((first - target.start).total_seconds()))

            return min(candidates, key=rank)
        return None

    def cached_event_for(self, target: CalendarEntry, now: datetime) -> UfcEvent | None:
        """A recent, matching event that may bridge an ESPN outage."""
        cached = self._state.cached_event
        if cached is None or not self._state.event_fetched_at:
            return None
        age = now.timestamp() - self._state.event_fetched_at
        if age < 0 or age > self._settings.cache_max_age_hours * 3600:
            return None
        target_terms = set(normalize_match_text(target.label).split())
        cached_terms = set(normalize_match_text(cached.name).split())
        if len(target_terms & cached_terms) < 2:
            return None
        return cached

    @staticmethod
    def segment_for(event: UfcEvent, now: datetime) -> Any:
        """Current broadcast segment, or the first segment during pre-roll."""
        started = [card for card in event.cards if card.start <= now]
        return started[-1] if started else (event.cards[0] if event.cards else None)

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

    async def apply(self, decision: Decision, event: UfcEvent, context: EventContext | None = None, *, now: datetime | None = None) -> None:
        """Carry out a START/STOP verdict against the cockpit.

        Discovery runs *before* the start, not after it. The other order is what
        put last week's channels on air for a whole card: the cockpit would come
        up on whatever stale links were on disk, and the refresh that followed
        then declined to touch a stream that was — by ffmpeg's measure — healthy.
        """
        if decision.action is SchedulerAction.START:
            raw_result = await self._start_stream(f"auto-schedule: {decision.reason}")
            result = raw_result if isinstance(raw_result, StartResult) else StartResult(
                StartStatus.STARTED if raw_result else StartStatus.FAILED
            )
            self._last_start_result = result
            stamp = (now or datetime.now(UTC)).timestamp()
            hard_stop_at = (
                event.first_card_start + timedelta(hours=self._settings.max_runtime_hours)
            ).timestamp() if event.first_card_start else None
            if result.accepted:
                if result.status is StartStatus.STARTED:
                    self._state.begin_event(
                        event.event_id,
                        by_scheduler=True,
                        moment=stamp,
                        hard_stop_at=hard_stop_at,
                    )
                else:
                    self._state.arm_event(event.event_id, moment=stamp, hard_stop_at=hard_stop_at)
                self._state.suppressed_event_id = None
                await self.persist()
                level = "ok" if result.status is StartStatus.STARTED else "warn"
                self._log(f"armed for {event.name} ({result.detail or decision.reason})", level)
            else:
                logger.warning("auto-schedule start failed for %s: %s", event.event_id, result.detail)
        elif decision.action is SchedulerAction.STOP:
            if await self._stop_stream(f"auto-schedule: {decision.reason}"):
                self._state.finish_event(event.event_id)
                await self.persist()
                self._log(f"stood down after {event.name} ({decision.reason})", "ok")
                if self._sources is not None:
                    # Drop the card's context so a later background sweep cannot
                    # re-adopt this event's feeds once it is over.
                    with contextlib.suppress(Exception):
                        self._sources.publish(None)
            else:
                logger.warning("auto-schedule stop failed for %s", event.event_id)

    async def note_arming_progress(self, event: UfcEvent, *, stream_running: bool, now: datetime) -> None:
        """Track (and eventually announce) a card that is under way with nothing on air.

        The system deliberately holds rather than streaming an unverified feed,
        so "armed but silent" is a legitimate state — but only for a few minutes.
        Past that it is an operator problem, and staying quiet about it is how a
        card gets missed entirely.
        """
        if stream_running or event.is_final:
            self._arm_attempts = 0
            return
        self._arm_attempts += 1
        first = event.first_card_start
        if (
            self._arm_attempts < ACQUISITION_ALERT_ATTEMPTS
            or first is None
            or now < first
            or self._acquisition_alerted == event.event_id
        ):
            return
        self._acquisition_alerted = event.event_id
        self._log(f"no verified source for {event.short_name} — the card is live and nothing is on air", "bad")
        embed = self._notifier.builder.for_acquisition_failure(event, self._arm_attempts)
        with contextlib.suppress(Exception):
            await self._notifier.send_embed(embed)

    async def tick(self, *, stream_running: bool, now: datetime | None = None) -> float:
        """One full cycle. Returns how long to sleep before the next one."""
        await self.ensure_loaded()
        settings = self.reload_settings()
        moment = now or datetime.now(UTC)

        if not settings.enabled:
            self._last_decision = Decision(SchedulerAction.IDLE, "auto-schedule disabled")
            self._event = None
            self.publish_context(None)
            return float(settings.live_poll_seconds)

        calendar = await self.refresh_calendar()
        target = self.select_target(calendar, moment)
        if target is None:
            self._event = None
            self._last_decision = Decision(SchedulerAction.IDLE, "no upcoming UFC event on the calendar")
            self.publish_context(None)
            return float(settings.calendar_refresh_seconds)

        warn_lead = timedelta(minutes=max(settings.notify.warn_minutes, default=1440)) + DETAIL_LEAD_SLACK + CALENDAR_SKEW
        if moment < target.start - warn_lead:
            self._event = None
            self._last_decision = Decision(SchedulerAction.IDLE, f"next card is {target.label}")
            remaining = (target.start - warn_lead - moment).total_seconds()
            return max(60.0, min(float(settings.calendar_refresh_seconds), remaining))

        event = await self.load_event(target)
        self._using_cached_event = False
        if isinstance(self._provider, EspnScheduleProvider):
            health = FEED_HEALTH.snapshot()
            self._state.espn_last_attempt_at = float(health.get("last_attempt_at") or moment.timestamp())
            self._state.espn_consecutive_failures = int(health.get("consecutive_failures") or 0)
            self._state.espn_last_error = str(health.get("last_error") or "") or None
        if event is not None:
            self._state.cached_event = event
            self._state.event_fetched_at = moment.timestamp()
            if isinstance(self._provider, EspnScheduleProvider):
                health = FEED_HEALTH.snapshot()
                self._state.espn_last_attempt_at = float(health.get("last_attempt_at") or moment.timestamp())
                self._state.espn_last_success_at = float(health.get("last_success_at") or moment.timestamp())
                self._state.espn_consecutive_failures = int(health.get("consecutive_failures") or 0)
                self._state.espn_last_error = str(health.get("last_error") or "") or None
            await self.persist()
        else:
            event = self.cached_event_for(target, moment)
            self._using_cached_event = event is not None
        self._event = event
        if event is None:
            # Keep an existing published context and owned hard deadline intact;
            # clearing both is what made a feed outage disable shutdown too.
            self._last_decision = self.decide(moment, None, self._state, settings, stream_running=stream_running)
            if (
                self._last_decision.action is SchedulerAction.STOP
                and self._state.current_event_id
                and await self._stop_stream(f"auto-schedule: {self._last_decision.reason}")
            ):
                self._state.finish_event(self._state.current_event_id)
                await self.persist()
            return float(settings.live_poll_seconds)

        # Hand the card's identity to the source scraper on every tick, so the
        # background sweeps between ticks match on tonight's fighters too.
        context = event.context(moment, settings)
        self.publish_context(context)

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

        # Remember when the scoreboard last moved, so a card ESPN forgets to
        # close out can still be stood down (see card_stalled).
        if self._state.note_progress(event.progress_signature, moment=moment.timestamp()):
            await self.persist()

        # Segment boundaries are explicit acquisition boundaries. While a feed
        # is absent or below the resolver's high-grade threshold, re-scrape on
        # the configured three-minute cadence. A healthy exact/current-segment
        # feed makes this a cheap no-op until the next boundary.
        segment = self.segment_for(event, moment)
        segment_key = segment.key if segment is not None else None
        segment_changed = bool(segment_key and segment_key != self._state.active_segment_key)
        source_satisfied = False
        if self._sources is not None:
            checker = getattr(self._sources, "is_satisfied", None)
            if callable(checker):
                with contextlib.suppress(Exception):
                    source_satisfied = bool(checker(context))
        refresh_due = moment.timestamp() - self._state.last_source_refresh_at >= settings.acquisition_poll_seconds
        in_window = event.phase(moment, settings) in {EventPhase.PRE_ROLL, EventPhase.LIVE}
        acquisition_allowed = (
            not event.is_final
            and event.event_id not in {self._state.handled_event_id, self._state.suppressed_event_id}
        )
        if (
            self._sources is not None
            and acquisition_allowed
            and in_window
            and (segment_changed or (not source_satisfied and refresh_due))
        ):
            if segment_changed and segment is not None:
                reason = (
                    f"pre-roll acquisition: {segment.label}"
                    if moment < segment.start
                    else f"segment boundary: {segment.label}"
                )
            else:
                reason = "source quality acquisition"
            try:
                await self._sources.refresh(reason, context)
            except Exception as exc:
                logger.warning("scheduled source refresh failed: %s", exc)
            self._state.last_source_refresh_at = moment.timestamp()
            self._state.active_segment_key = segment_key
            await self.persist()

        # Take ownership of a stream that was already up when the card started,
        # so the post-card stand-down still happens.
        if self.should_adopt(moment, event, self._state, settings, stream_running=stream_running):
            hard_stop_at = (
                event.first_card_start + timedelta(hours=settings.max_runtime_hours)
            ).timestamp() if event.first_card_start else None
            self._state.begin_event(
                event.event_id,
                by_scheduler=True,
                moment=moment.timestamp(),
                hard_stop_at=hard_stop_at,
            )
            await self.persist()
            self._log(f"adopted the running stream for {event.name}; will stand down when it ends", "warn")

        self.sweep_stale(moment, event, self._state, settings)
        await self.notify_due(moment, event)

        decision = self.decide(moment, event, self._state, settings, stream_running=stream_running)
        self._last_decision = decision
        if decision.acts:
            await self.apply(decision, event, context, now=moment)
        if self._state.started_by_scheduler and stream_running and self._state.note_encoder_started(moment=moment.timestamp()):
            await self.persist()
        if decision.action is SchedulerAction.START:
            await self.note_arming_progress(event, stream_running=stream_running, now=moment)
        elif stream_running:
            self._arm_attempts = 0
        return self.next_wakeup(moment, event, settings)

    def publish_context(self, context: EventContext | None) -> None:
        """Push the tracked card's identity to the source scraper (None clears it)."""
        if context == self._context:
            return
        self._context = context
        if self._sources is None:
            return
        with contextlib.suppress(Exception):
            self._sources.publish(context)

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
            # Wake exactly when the three-minute acquisition deadline is due;
            # simply sleeping min(180, 120) drifts refreshes to four minutes.
            context = event.context(now, settings)
            checker = getattr(self._sources, "is_satisfied", None) if self._sources is not None else None
            satisfied = False
            if callable(checker):
                with contextlib.suppress(Exception):
                    satisfied = bool(checker(context))
            if not satisfied:
                elapsed = max(0.0, now.timestamp() - self._state.last_source_refresh_at)
                until_refresh = max(1.0, settings.acquisition_poll_seconds - elapsed)
                return min(float(settings.live_poll_seconds), until_refresh)
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
            "acquisition_poll_seconds": settings.acquisition_poll_seconds,
            "phase": EventPhase.IDLE.value,
            "action": self._last_decision.action.value,
            "run_state": "standby",
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
            "data_health": {
                "status": "stale-cache" if self._using_cached_event else (
                    "unavailable" if self._state.espn_consecutive_failures else "healthy"
                ),
                "using_cached_event": self._using_cached_event,
                "last_success_at": self._state.espn_last_success_at or None,
                "last_attempt_at": self._state.espn_last_attempt_at or None,
                "consecutive_failures": self._state.espn_consecutive_failures,
                "last_error": self._state.espn_last_error,
                "event_cache_age_seconds": max(0, int(moment.timestamp() - self._state.event_fetched_at)) if self._state.event_fetched_at else None,
            },
            "lifecycle": {
                "armed_at": self._state.armed_at,
                "encoder_started_at": self._state.started_at,
                "hard_stop_at": self._state.hard_stop_at,
                "active_segment_key": self._state.active_segment_key,
                "last_source_refresh_at": self._state.last_source_refresh_at or None,
                "start_status": self._last_start_result.status.value if self._last_start_result else None,
                "start_detail": self._last_start_result.detail if self._last_start_result else None,
            },
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
            if event.is_final:
                payload["run_state"] = "wrapping" if self._state.started_by_scheduler else "stopped"
            elif self._last_start_result and self._last_start_result.status is StartStatus.AWAITING_SOURCE:
                payload["run_state"] = "acquiring"
            elif self._state.started_by_scheduler and self._state.started_at:
                payload["run_state"] = "live"
            elif event.phase(moment, settings) is EventPhase.PRE_ROLL:
                payload["run_state"] = "acquiring"
            else:
                payload["run_state"] = "pending"
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
                        "bouts_list": list(card.bouts),
                    }
                    for card in event.cards
                ],
            }
            if first is not None:
                payload["countdown_seconds"] = max(0, int((first - moment).total_seconds()))
                payload["countdown_is_estimate"] = False
        # What the source scraper is matching against, and whether the card is
        # armed-but-silent while it hunts for a feed it can identify.
        payload["context"] = self._context.to_dict() if self._context else None
        payload["awaiting_source"] = bool(self._arm_attempts)
        payload["arm_attempts"] = self._arm_attempts
        return payload
