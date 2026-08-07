"""End-to-end exercise of ``UfcScheduler.tick``.

The pure policy is covered in ``test_schedule_policy``; this file drives the
async shell — ESPN observation, state persistence, notification delivery, and
the callbacks into the cockpit — with stubs standing in for the network.
"""

import json
import pathlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from obbyschedule import EspnScheduleProvider, ScheduleSettings, StartResult, StartStatus, UfcScheduler
from obbyschedule.models import CalendarEntry

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
WEBHOOK = "https://discord.com/api/webhooks/1/token"
CARD_START = datetime(2026, 7, 25, 13, 0, tzinfo=UTC)


def load(name):
    return json.loads((FIXTURES / f"espn_{name}.json").read_text())


class FakeProvider:
    """Stands in for :class:`EspnScheduleProvider` without touching the network."""

    def __init__(self, settings, payload_name):
        self.settings = settings
        self.payload_name = payload_name
        self.event_calls = 0

    def with_settings(self, settings):
        self.settings = settings
        return self

    async def fetch_calendar(self):
        return EspnScheduleProvider.parse_calendar(load("final"), self.settings)

    async def fetch_event(self, day):
        self.event_calls += 1
        return EspnScheduleProvider.parse_events(load(self.payload_name), self.settings)


class Recorder:
    """Captures the cockpit callbacks the scheduler fires."""

    def __init__(self):
        self.started = []
        self.stopped = []
        self.refreshed = []
        self.contexts = []
        self.published = []
        self.embeds = []
        self.satisfied = False
        self.start_result = True

    async def start(self, reason):
        self.started.append(reason)
        return self.start_result

    async def stop(self, reason):
        self.stopped.append(reason)
        return True

    async def refresh(self, reason, context=None):
        self.refreshed.append(reason)
        self.contexts.append(context)

    def publish(self, context):
        self.published.append(context)

    def is_satisfied(self, _context):
        return self.satisfied


class FakeNotifier:
    def __init__(self, recorder, builder):
        self._recorder = recorder
        self.builder = builder
        self.active = True

    def with_settings(self, settings):
        return self

    async def send_embed(self, embed):
        self._recorder.embeds.append(embed)
        return True


def build(tmp_path, payload_name, *, config=None, notify=True):
    """Wire a scheduler up to stubs, with state persisted under tmp_path."""
    section = {"state_path": str(tmp_path / "schedule_state.json")}
    if notify:
        section["notify"] = {"discord_webhook_url": WEBHOOK}
    section.update(config or {})

    recorder = Recorder()
    # The stubs below replace every collaborator that would touch the client,
    # so this instance never opens a connection.
    scheduler = UfcScheduler(
        client=httpx.AsyncClient(),
        load_config=lambda: {"schedule": section},
        start_stream=recorder.start,
        stop_stream=recorder.stop,
        sources=recorder,
    )
    settings = ScheduleSettings.from_config(section)
    scheduler.bind(
        provider=FakeProvider(settings, payload_name),
        notifier=FakeNotifier(recorder, scheduler.notifier.builder),
    )
    return scheduler, recorder


def titles(recorder):
    return [embed["title"] for embed in recorder.embeds]


@pytest.mark.asyncio
async def test_tick_arms_the_stream_inside_the_pre_roll(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")

    await scheduler.tick(stream_running=False, now=CARD_START - timedelta(minutes=10))

    assert len(recorder.started) == 1
    assert "pre-roll" in recorder.started[0]
    # The pre-roll is when the spare provider connection is free, so source
    # discovery must be kicked immediately rather than waiting for the sweep —
    # and it must happen *before* the start, or the encode comes up on whatever
    # stale links are still on disk.
    assert len(recorder.refreshed) == 1
    assert "pre-roll" in recorder.refreshed[0]
    assert scheduler.state.started_by_scheduler is True
    assert recorder.stopped == []


@pytest.mark.asyncio
async def test_the_card_identity_reaches_the_source_scraper(tmp_path):
    """The scraper cannot pick tonight's feed without knowing whose fight it is."""
    scheduler, recorder = build(tmp_path, "pre")

    await scheduler.tick(stream_running=False, now=CARD_START - timedelta(minutes=10))

    published = [context for context in recorder.published if context is not None]
    assert published, "the tracked card must be published to the source resolver"
    assert published[-1].terms, "a card with no match terms would gate everything out"
    # The refresh that precedes the start carries the same card.
    assert recorder.contexts[-1] is not None
    assert recorder.contexts[-1].event_id == published[-1].event_id


@pytest.mark.asyncio
async def test_tick_does_not_arm_a_day_out(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")

    # Just past the 24h mark: the countdown goes out, but nothing is armed.
    await scheduler.tick(stream_running=False, now=CARD_START - timedelta(hours=24) + timedelta(minutes=5))

    assert recorder.started == []
    # First tracking of this card also posts "Coming up", so match on any title.
    assert any("24 hours away" in title for title in titles(recorder))


@pytest.mark.asyncio
async def test_24h_warning_survives_the_calendar_skew(tmp_path):
    """Regression: ESPN's calendar start is not always the first bout.

    For this card the calendar says 16:00Z (main card) while the prelims open at
    13:00Z. Countdowns anchor on the real first bout, so the event detail must be
    loaded early enough that the 24h warning still fires.
    """
    scheduler, recorder = build(tmp_path, "pre")

    await scheduler.tick(stream_running=False, now=CARD_START - timedelta(hours=24) + timedelta(minutes=2))

    assert any("24 hours away" in title for title in titles(recorder))


@pytest.mark.asyncio
async def test_poll_backs_off_while_the_card_is_far_out(tmp_path):
    """The wide detail window must not turn into a 2-minute ESPN poll for 37 hours."""
    scheduler, _recorder = build(tmp_path, "pre")

    far = await scheduler.tick(stream_running=False, now=CARD_START - timedelta(hours=30))
    near = await scheduler.tick(stream_running=True, now=CARD_START - timedelta(minutes=5))

    assert far > 600
    assert near == 120


@pytest.mark.asyncio
async def test_poll_tightens_while_no_source_is_on_air(tmp_path):
    """Armed with nothing streaming means retry acquisition sooner than the 2m poll."""
    scheduler, _recorder = build(tmp_path, "pre")

    delay = await scheduler.tick(stream_running=False, now=CARD_START - timedelta(minutes=5))

    assert delay <= 180


@pytest.mark.asyncio
async def test_unsatisfied_source_is_rescraped_on_exact_three_minute_cadence(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")
    opened = CARD_START - timedelta(minutes=10)

    await scheduler.tick(stream_running=False, now=opened)
    await scheduler.tick(stream_running=True, now=opened + timedelta(seconds=179))
    assert len(recorder.refreshed) == 1

    await scheduler.tick(stream_running=True, now=opened + timedelta(seconds=180))
    assert len(recorder.refreshed) == 2
    assert recorder.refreshed[-1] == "source quality acquisition"


@pytest.mark.asyncio
async def test_each_new_segment_forces_a_refresh_even_with_a_good_feed(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")
    recorder.satisfied = True

    await scheduler.tick(stream_running=False, now=CARD_START - timedelta(minutes=10))
    await scheduler.tick(stream_running=True, now=CARD_START + timedelta(minutes=30))
    assert len(recorder.refreshed) == 1

    # This fixture has Prelims at 13:00Z and Main card at 16:00Z. A healthy
    # prelim feed must not suppress acquisition at the main-card boundary.
    await scheduler.tick(stream_running=True, now=CARD_START + timedelta(hours=3))
    assert len(recorder.refreshed) == 2
    assert recorder.refreshed[-1] == "segment boundary: Main card"


@pytest.mark.asyncio
async def test_awaiting_source_is_owned_without_claiming_the_encoder_started(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")
    recorder.start_result = StartResult(StartStatus.AWAITING_SOURCE, "no verified current-segment feed")
    opened = CARD_START - timedelta(minutes=10)

    await scheduler.tick(stream_running=False, now=opened)

    assert scheduler.state.started_by_scheduler is True
    assert scheduler.state.armed_at == opened.timestamp()
    assert scheduler.state.started_at is None
    assert scheduler.state.hard_stop_at is not None
    assert scheduler.snapshot(opened)["run_state"] == "acquiring"

    # If the source appears between ticks and the cockpit starts ffmpeg, the
    # first positive observation records the real encoder start separately.
    await scheduler.tick(stream_running=True, now=opened + timedelta(minutes=3))
    assert scheduler.state.started_at == (opened + timedelta(minutes=3)).timestamp()


@pytest.mark.asyncio
async def test_recent_event_cache_bridges_a_detail_feed_outage(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")
    opened = CARD_START - timedelta(minutes=10)
    await scheduler.tick(stream_running=False, now=opened)
    cached_event_id = scheduler.event.event_id

    class EmptyDetailProvider(FakeProvider):
        async def fetch_event(self, day):
            self.event_calls += 1

    scheduler.bind(provider=EmptyDetailProvider(scheduler.settings, "pre"))
    await scheduler.tick(stream_running=True, now=opened + timedelta(minutes=3))

    assert scheduler.event is not None
    assert scheduler.event.event_id == cached_event_id
    assert scheduler.snapshot(opened + timedelta(minutes=3))["data_health"]["status"] == "stale-cache"
    assert recorder.stopped == []


@pytest.mark.asyncio
async def test_tick_is_a_no_op_when_disabled(tmp_path):
    scheduler, recorder = build(tmp_path, "pre", config={"enabled": False})

    delay = await scheduler.tick(stream_running=False, now=CARD_START - timedelta(minutes=10))

    assert recorder.started == []
    assert recorder.embeds == []
    assert delay > 0


@pytest.mark.asyncio
async def test_tick_stands_down_after_the_grace_period(tmp_path):
    scheduler, recorder = build(tmp_path, "final")
    finished_at = CARD_START + timedelta(hours=5)

    # First observation stamps the grace clock but holds the stream up.
    await scheduler.tick(stream_running=True, now=finished_at)
    scheduler.state.started_by_scheduler = True
    scheduler.state.started_at = CARD_START.timestamp()
    assert recorder.stopped == []

    # Once the grace elapses the stream comes down and standby resumes.
    await scheduler.tick(stream_running=True, now=finished_at + timedelta(minutes=31))

    assert len(recorder.stopped) == 1
    assert scheduler.state.handled_event_id == "600059667"
    assert scheduler.state.started_by_scheduler is False


@pytest.mark.asyncio
async def test_wrap_up_embed_is_posted_when_the_card_ends(tmp_path):
    scheduler, recorder = build(tmp_path, "final")

    await scheduler.tick(stream_running=True, now=CARD_START + timedelta(hours=5))

    assert any("has ended" in title for title in titles(recorder))
    wrap = next(embed for embed in recorder.embeds if "has ended" in embed["title"])
    assert "Magomed Ankalaev" in wrap["description"]


@pytest.mark.asyncio
async def test_card_start_embed_fires_as_a_segment_goes_live(tmp_path):
    scheduler, recorder = build(tmp_path, "mid")

    await scheduler.tick(stream_running=True, now=CARD_START + timedelta(minutes=2))

    assert any("LIVE NOW" in title for title in titles(recorder))


@pytest.mark.asyncio
async def test_notifications_are_not_repeated_across_ticks(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")
    now = CARD_START - timedelta(hours=24) + timedelta(minutes=5)

    await scheduler.tick(stream_running=False, now=now)
    first = len(recorder.embeds)
    await scheduler.tick(stream_running=False, now=now + timedelta(minutes=2))

    assert first > 0
    assert len(recorder.embeds) == first


@pytest.mark.asyncio
async def test_state_survives_a_restart(tmp_path):
    scheduler, _recorder = build(tmp_path, "pre")
    now = CARD_START - timedelta(hours=24) + timedelta(minutes=5)
    await scheduler.tick(stream_running=False, now=now)

    revived, revived_recorder = build(tmp_path, "pre")
    await revived.tick(stream_running=False, now=now + timedelta(minutes=1))

    # A fresh process must not re-announce what the old one already sent.
    assert revived_recorder.embeds == []


@pytest.mark.asyncio
async def test_operator_suppression_blocks_arming(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")

    # Seed the tracked event, then veto it the way the Stop endpoint does.
    await scheduler.tick(stream_running=False, now=CARD_START - timedelta(hours=24) + timedelta(minutes=5))
    scheduler.state.suppressed_event_id = scheduler.state.current_event_id

    await scheduler.tick(stream_running=False, now=CARD_START - timedelta(minutes=5))

    assert recorder.started == []
    assert "manually" in scheduler.last_decision.reason


@pytest.mark.asyncio
async def test_snapshot_reports_the_tracked_card(tmp_path):
    scheduler, _recorder = build(tmp_path, "pre")
    now = CARD_START - timedelta(hours=2)
    await scheduler.tick(stream_running=False, now=now)

    snapshot = scheduler.snapshot(now)

    assert snapshot["enabled"] is True
    assert snapshot["event"]["name"] == "UFC Fight Night: Ankalaev vs. Guskov"
    assert [card["label"] for card in snapshot["event"]["cards"]] == ["Prelims", "Main card"]
    assert snapshot["countdown_seconds"] == 7200
    assert snapshot["phase"] == "pending"


@pytest.mark.asyncio
async def test_snapshot_without_a_tracked_event_is_still_well_formed(tmp_path):
    scheduler, _recorder = build(tmp_path, "pre")
    snapshot = scheduler.snapshot(CARD_START)

    assert snapshot["event"] is None
    assert snapshot["action"] == "idle"


@pytest.mark.asyncio
async def test_calendar_is_cached_between_ticks(tmp_path):
    scheduler, _recorder = build(tmp_path, "pre")
    now = CARD_START - timedelta(hours=24) + timedelta(minutes=5)

    await scheduler.tick(stream_running=False, now=now)
    fetched_at = scheduler.state.calendar_fetched_at
    await scheduler.tick(stream_running=False, now=now + timedelta(minutes=2))

    assert scheduler.state.calendar_fetched_at == fetched_at
    assert len(scheduler.state.calendar) > 0


def test_next_after_picks_the_following_card():
    calendar = (
        CalendarEntry(label="UFC 330", start=CARD_START, end=None),
        CalendarEntry(label="UFC 331", start=CARD_START + timedelta(days=14), end=None),
    )
    following = UfcScheduler.next_after(calendar, CARD_START)
    assert following is not None
    assert following.label == "UFC 331"
    assert UfcScheduler.next_after(calendar, CARD_START + timedelta(days=30)) is None


@pytest.mark.asyncio
async def test_adopts_a_running_stream_and_stands_it_down(tmp_path):
    """The 24/7 case: nobody pressed Stop, yet the card still ends in standby.

    This is the scenario production was actually in — the encode had been up for
    days. Without adoption the scheduler would never own it and it would keep
    running after the card, which is the behaviour this feature exists to end.
    """
    scheduler, recorder = build(tmp_path, "mid")

    await scheduler.tick(stream_running=True, now=CARD_START + timedelta(minutes=30))
    assert scheduler.state.started_by_scheduler is True
    assert recorder.stopped == []

    # Card wraps up; the adopted stream is stood down after the grace period.
    scheduler.bind(provider=FakeProvider(scheduler.settings, "final"))
    finished_at = CARD_START + timedelta(hours=5)
    await scheduler.tick(stream_running=True, now=finished_at)
    await scheduler.tick(stream_running=True, now=finished_at + timedelta(minutes=31))

    assert len(recorder.stopped) == 1
    assert scheduler.state.started_by_scheduler is False


@pytest.mark.asyncio
async def test_rearms_after_the_encode_dies_mid_card(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")

    await scheduler.tick(stream_running=False, now=CARD_START - timedelta(minutes=10))
    assert len(recorder.started) == 1

    # ffmpeg is gone and the watchdog could not bring it back.
    await scheduler.tick(stream_running=False, now=CARD_START + timedelta(minutes=30))

    assert len(recorder.started) == 2
    assert "re-arming" in recorder.started[-1]


@pytest.mark.asyncio
async def test_manual_stop_survives_adoption(tmp_path):
    """A vetoed card must not be silently re-adopted on the next tick."""
    scheduler, recorder = build(tmp_path, "mid")

    await scheduler.tick(stream_running=True, now=CARD_START + timedelta(minutes=30))
    # Operator presses Stop: the cockpit stamps the veto and kills the encode.
    scheduler.state.suppressed_event_id = scheduler.state.current_event_id
    scheduler.state.started_by_scheduler = False

    await scheduler.tick(stream_running=False, now=CARD_START + timedelta(minutes=40))

    assert recorder.started == []
    assert scheduler.state.started_by_scheduler is False


@pytest.mark.asyncio
async def test_polling_backs_off_once_the_card_is_stood_down(tmp_path):
    """No point polling ESPN every 2 minutes for the rest of the lookback window."""
    scheduler, _recorder = build(tmp_path, "final")
    finished_at = CARD_START + timedelta(hours=5)

    await scheduler.tick(stream_running=True, now=finished_at)
    scheduler.state.started_by_scheduler = True
    scheduler.state.started_at = CARD_START.timestamp()
    await scheduler.tick(stream_running=True, now=finished_at + timedelta(minutes=31))
    assert scheduler.state.handled_event_id == "600059667"

    delay = await scheduler.tick(stream_running=False, now=finished_at + timedelta(minutes=25))
    assert delay >= 3600
    assert _recorder.refreshed == []


@pytest.mark.asyncio
async def test_countdown_is_flagged_as_an_estimate_until_the_detail_loads(tmp_path):
    """The calendar start and the real first bout can differ by hours."""
    scheduler, _recorder = build(tmp_path, "pre")

    far = scheduler.snapshot(CARD_START - timedelta(days=3))
    assert far["countdown_is_estimate"] is True

    await scheduler.tick(stream_running=False, now=CARD_START - timedelta(hours=2))
    near = scheduler.snapshot(CARD_START - timedelta(hours=2))
    assert near["countdown_is_estimate"] is False
    assert near["countdown_seconds"] == 7200


@pytest.mark.asyncio
async def test_coming_up_is_announced_once_when_a_card_is_first_tracked(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")
    now = CARD_START - timedelta(hours=30)

    await scheduler.tick(stream_running=False, now=now)
    assert any(t.startswith("📅 Coming up") for t in titles(recorder))

    # Subsequent ticks must not repeat it.
    before = len(recorder.embeds)
    await scheduler.tick(stream_running=False, now=now + timedelta(minutes=5))
    assert len(recorder.embeds) == before


@pytest.mark.asyncio
async def test_coming_up_survives_a_restart_without_repeating(tmp_path):
    scheduler, recorder = build(tmp_path, "pre")
    now = CARD_START - timedelta(hours=30)
    await scheduler.tick(stream_running=False, now=now)
    assert recorder.embeds

    revived, revived_recorder = build(tmp_path, "pre")
    await revived.tick(stream_running=False, now=now + timedelta(minutes=1))
    assert revived_recorder.embeds == []


@pytest.mark.asyncio
async def test_coming_up_can_be_forced_by_the_operator(tmp_path):
    """The cockpit button re-posts on demand, ignoring the once-per-event ledger."""
    scheduler, recorder = build(tmp_path, "pre")
    now = CARD_START - timedelta(hours=30)
    await scheduler.tick(stream_running=False, now=now)
    before = len(recorder.embeds)

    event = await scheduler.load_upcoming_event(now)
    assert event is not None
    assert await scheduler.announce_coming_up(event, force=True) is True
    assert len(recorder.embeds) == before + 1


@pytest.mark.asyncio
async def test_coming_up_can_be_disabled(tmp_path):
    scheduler, recorder = build(tmp_path, "pre", config={"notify": {"discord_webhook_url": WEBHOOK, "notify_coming_up": False}})

    await scheduler.tick(stream_running=False, now=CARD_START - timedelta(hours=30))

    assert not any(t.startswith("📅 Coming up") for t in titles(recorder))


@pytest.mark.asyncio
async def test_coming_up_is_not_announced_for_a_finished_card(tmp_path):
    scheduler, recorder = build(tmp_path, "final")
    await scheduler.tick(stream_running=False, now=CARD_START + timedelta(hours=6))
    assert not any(t.startswith("📅 Coming up") for t in titles(recorder))


@pytest.mark.asyncio
async def test_load_upcoming_event_works_outside_the_poll_window(tmp_path):
    """The manual button must work even when the loop would not load detail yet."""
    scheduler, _recorder = build(tmp_path, "pre")
    event = await scheduler.load_upcoming_event(CARD_START - timedelta(days=5))
    assert event is not None
    assert event.cards


@pytest.mark.asyncio
async def test_a_dark_feed_announces_itself_once(tmp_path):
    """The 2026-08-04 blackout lasted three days because it was silent. A
    scheduler that cannot see the calendar cannot warn about a card, so it has to
    warn about itself - once per outage, not once per failed fetch."""
    from obbyschedule import scheduler as scheduler_module
    from obbyschedule.espn import FEED_HEALTH

    scheduler, recorder = build(tmp_path, "pre")
    FEED_HEALTH.__init__()
    FEED_HEALTH.failure("403 Forbidden", 403)
    FEED_HEALTH.failure("403 Forbidden", 403)
    # Never succeeded since boot, so staleness cannot exonerate it.
    assert FEED_HEALTH.consecutive_failures >= scheduler_module.FEED_DARK_FAILURES

    await scheduler._alert_if_feed_is_dark()
    await scheduler._alert_if_feed_is_dark()

    alarms = [t for t in titles(recorder) if "feed is down" in t]
    assert len(alarms) == 1, f"expected exactly one alarm, got {titles(recorder)}"


@pytest.mark.asyncio
async def test_a_recovered_feed_re_arms_the_alarm(tmp_path):
    """Otherwise the second outage of the day passes in silence."""
    from obbyschedule import scheduler as scheduler_module
    from obbyschedule.espn import FEED_HEALTH

    scheduler, recorder = build(tmp_path, "pre")
    FEED_HEALTH.__init__()
    FEED_HEALTH.failure("403", 403)
    FEED_HEALTH.failure("403", 403)
    await scheduler._alert_if_feed_is_dark()

    FEED_HEALTH.success(200, "python-httpx/0.28.1")
    await scheduler._alert_if_feed_is_dark()

    # A feed that answered seconds ago is not an outage yet, however many
    # requests have failed since - the staleness gate is what separates a blip
    # from a blackout.
    FEED_HEALTH.failure("403", 403)
    FEED_HEALTH.failure("403", 403)
    await scheduler._alert_if_feed_is_dark()
    assert len([t for t in titles(recorder) if "feed is down" in t]) == 1

    # Once it has been dark for the threshold, it alarms again.
    assert FEED_HEALTH.last_success_at is not None
    FEED_HEALTH.last_success_at = (
        FEED_HEALTH.last_success_at - scheduler_module.FEED_DARK_SECONDS - 60
    )
    await scheduler._alert_if_feed_is_dark()

    alarms = [t for t in titles(recorder) if "feed is down" in t]
    assert len(alarms) == 2, "the alarm did not re-arm after recovery"


@pytest.mark.asyncio
async def test_one_flaky_fetch_is_not_an_outage(tmp_path):
    """A single failed request must not page anyone."""
    from obbyschedule.espn import FEED_HEALTH

    scheduler, recorder = build(tmp_path, "pre")
    FEED_HEALTH.__init__()
    FEED_HEALTH.failure("timeout", None)

    await scheduler._alert_if_feed_is_dark()

    assert not [t for t in titles(recorder) if "feed is down" in t]
