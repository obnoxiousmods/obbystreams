"""Start/stop policy for the UFC auto-schedule.

``UfcScheduler.decide`` is pure and synchronous, so the whole timeline is walked
here with a fake clock — no network, no event loop, no ffmpeg.
"""

from datetime import UTC, datetime, timedelta

import pytest

from obbyschedule import CardSegment, EventPhase, SchedulerAction, ScheduleSettings, ScheduleState, UfcEvent, UfcScheduler

FIRST_CARD = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
MAIN_CARD = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)
EVENT_ID = "600060000"


def build_event(*, final=False, event_id=EVENT_ID, cards=True):
    segments = ()
    if cards:
        segments = (
            CardSegment(start=FIRST_CARD, label="Prelims", bout_count=6, completed_bouts=6 if final else 0),
            CardSegment(start=MAIN_CARD, label="Main card", bout_count=5, completed_bouts=5 if final else 0),
        )
    return UfcEvent(
        event_id=event_id,
        name="UFC Fight Night: Medić vs. Rodriguez",
        short_name="Medić vs. Rodriguez",
        venue="Belgrade Arena",
        city="Belgrade, Serbia",
        cards=segments,
        is_final=final,
    )


@pytest.fixture
def scheduler():
    """A scheduler with no collaborators — only the pure methods are exercised."""
    return UfcScheduler.__new__(UfcScheduler)


@pytest.fixture
def settings():
    return ScheduleSettings.from_config({})


def armed_state(*, started_at=None, final_seen_at=None):
    """State as it looks once the scheduler owns the encode."""
    state = ScheduleState()
    state.current_event_id = EVENT_ID
    state.started_by_scheduler = True
    state.started_at = (started_at or FIRST_CARD).timestamp()
    state.final_seen_at = final_seen_at.timestamp() if final_seen_at else None
    return state


def test_automation_defaults_match_the_broadcast_contract(settings):
    assert settings.lead_minutes == 10
    assert settings.end_grace_minutes == 30
    assert settings.acquisition_poll_seconds == 180
    assert settings.cache_max_age_hours == 72


# ------------------------------------------------------------------- arming
def test_idle_well_before_the_card(scheduler, settings):
    now = FIRST_CARD - timedelta(hours=25)
    decision = scheduler.decide(now, build_event(), ScheduleState(), settings, stream_running=False)

    assert decision.action is SchedulerAction.IDLE
    assert "pre-roll window" in decision.reason
    assert decision.phase is EventPhase.PENDING


def test_idle_one_minute_before_the_pre_roll_opens(scheduler, settings):
    now = FIRST_CARD - timedelta(minutes=settings.lead_minutes + 1)
    decision = scheduler.decide(now, build_event(), ScheduleState(), settings, stream_running=False)
    assert decision.action is SchedulerAction.IDLE


def test_starts_once_inside_the_pre_roll_window(scheduler, settings):
    now = FIRST_CARD - timedelta(minutes=settings.lead_minutes - 1)
    decision = scheduler.decide(now, build_event(), ScheduleState(), settings, stream_running=False)

    assert decision.action is SchedulerAction.START
    assert decision.event_id == EVENT_ID
    assert decision.phase is EventPhase.PRE_ROLL


def test_starts_late_if_the_service_was_down_at_card_time(scheduler, settings):
    """Booting mid-card must still arm rather than wait for the next event."""
    decision = scheduler.decide(FIRST_CARD + timedelta(hours=1), build_event(), ScheduleState(), settings, stream_running=False)
    assert decision.action is SchedulerAction.START


def test_does_not_start_after_the_window_lapsed(scheduler, settings):
    now = FIRST_CARD + timedelta(hours=settings.max_runtime_hours + 1)
    decision = scheduler.decide(now, build_event(), ScheduleState(), settings, stream_running=False)

    assert decision.action is SchedulerAction.IDLE
    assert "lapsed" in decision.reason


def test_does_not_start_a_finished_event(scheduler, settings):
    decision = scheduler.decide(MAIN_CARD, build_event(final=True), ScheduleState(), settings, stream_running=False)

    assert decision.action is SchedulerAction.IDLE
    assert decision.phase is EventPhase.FINISHED


def test_does_not_start_when_disabled(scheduler):
    disabled = ScheduleSettings.from_config({"enabled": False})
    decision = scheduler.decide(FIRST_CARD, build_event(), ScheduleState(), disabled, stream_running=False)

    assert decision.action is SchedulerAction.IDLE
    assert "disabled" in decision.reason


def test_does_not_start_when_a_manual_stream_is_already_running(scheduler, settings):
    decision = scheduler.decide(FIRST_CARD, build_event(), ScheduleState(), settings, stream_running=True)

    assert decision.action is SchedulerAction.IDLE
    assert "not scheduler-owned" in decision.reason


def test_event_without_card_times_is_ignored(scheduler, settings):
    decision = scheduler.decide(FIRST_CARD, build_event(cards=False), ScheduleState(), settings, stream_running=False)
    assert decision.action is SchedulerAction.IDLE


def test_no_event_is_idle(scheduler, settings):
    decision = scheduler.decide(FIRST_CARD, None, ScheduleState(), settings, stream_running=False)
    assert decision.action is SchedulerAction.IDLE


# --------------------------------------------------------- operator override
def test_manual_stop_suppresses_only_this_event(scheduler, settings):
    """Stop mid-card means 'not this one' — the scheduler must not re-arm it."""
    state = ScheduleState()
    state.suppressed_event_id = EVENT_ID
    decision = scheduler.decide(FIRST_CARD, build_event(), state, settings, stream_running=False)

    assert decision.action is SchedulerAction.IDLE
    assert "manually" in decision.reason


def test_suppression_does_not_leak_to_the_next_event(scheduler, settings):
    state = ScheduleState()
    state.suppressed_event_id = "some-older-event"
    decision = scheduler.decide(FIRST_CARD, build_event(), state, settings, stream_running=False)
    assert decision.action is SchedulerAction.START


def test_already_handled_event_never_rearms(scheduler, settings):
    state = ScheduleState()
    state.handled_event_id = EVENT_ID
    decision = scheduler.decide(FIRST_CARD, build_event(), state, settings, stream_running=False)

    assert decision.action is SchedulerAction.IDLE
    assert "stood down" in decision.reason


# ---------------------------------------------------------------- stand-down
def test_stays_up_while_the_card_is_live(scheduler, settings):
    decision = scheduler.decide(MAIN_CARD, build_event(), armed_state(), settings, stream_running=True)

    assert decision.action is SchedulerAction.IDLE
    assert decision.reason == "event in progress"
    assert decision.phase is EventPhase.LIVE


def test_holds_through_the_post_fight_grace(scheduler, settings):
    final_at = MAIN_CARD + timedelta(hours=2)
    state = armed_state(final_seen_at=final_at)
    now = final_at + timedelta(minutes=settings.end_grace_minutes - 1)

    decision = scheduler.decide(now, build_event(final=True), state, settings, stream_running=True)

    assert decision.action is SchedulerAction.IDLE
    assert "grace" in decision.reason
    assert decision.phase is EventPhase.WRAPPING


def test_stops_once_the_grace_elapses(scheduler, settings):
    final_at = MAIN_CARD + timedelta(hours=2)
    state = armed_state(final_seen_at=final_at)
    now = final_at + timedelta(minutes=settings.end_grace_minutes)

    decision = scheduler.decide(now, build_event(final=True), state, settings, stream_running=True)

    assert decision.action is SchedulerAction.STOP
    assert decision.event_id == EVENT_ID


def test_max_runtime_failsafe_stops_a_card_espn_never_finalises(scheduler, settings):
    """If ESPN stalls, the encode must still come down."""
    state = armed_state()
    now = FIRST_CARD + timedelta(hours=settings.max_runtime_hours)

    decision = scheduler.decide(now, build_event(), state, settings, stream_running=True)

    assert decision.action is SchedulerAction.STOP
    assert "failsafe" in decision.reason


def test_persisted_hard_stop_survives_complete_espn_loss(scheduler, settings):
    state = armed_state()
    state.hard_stop_at = (FIRST_CARD + timedelta(hours=settings.max_runtime_hours)).timestamp()

    decision = scheduler.decide(
        FIRST_CARD + timedelta(hours=settings.max_runtime_hours),
        None,
        state,
        settings,
        stream_running=True,
    )

    assert decision.action is SchedulerAction.STOP
    assert decision.event_id == EVENT_ID


def test_failsafe_wins_over_the_grace_hold(scheduler, settings):
    """A stuck grace stamp must not defeat the runtime cap."""
    state = armed_state(final_seen_at=FIRST_CARD + timedelta(hours=settings.max_runtime_hours))
    now = FIRST_CARD + timedelta(hours=settings.max_runtime_hours + 1)

    decision = scheduler.decide(now, build_event(final=True), state, settings, stream_running=True)

    assert decision.action is SchedulerAction.STOP
    assert "failsafe" in decision.reason


def test_never_stops_a_stream_it_did_not_start(scheduler, settings):
    """Manual streams are the operator's; the scheduler must not yank them."""
    state = ScheduleState()
    state.current_event_id = EVENT_ID
    state.started_by_scheduler = False
    state.final_seen_at = MAIN_CARD.timestamp()

    now = MAIN_CARD + timedelta(hours=3)
    decision = scheduler.decide(now, build_event(final=True), state, settings, stream_running=True)

    assert decision.action is SchedulerAction.IDLE


def test_final_without_a_grace_stamp_does_not_stop(scheduler, settings):
    """The stamp is set by the tick loop; without it the grace clock has no origin."""
    state = armed_state()
    decision = scheduler.decide(MAIN_CARD + timedelta(hours=4), build_event(final=True), state, settings, stream_running=True)
    assert decision.action is SchedulerAction.IDLE


# ------------------------------------------------------------ target picking
def test_select_target_prefers_the_card_in_progress(scheduler):
    from obbyschedule import CalendarEntry

    calendar = (
        CalendarEntry(label="UFC 330", start=FIRST_CARD, end=None),
        CalendarEntry(label="UFC 331", start=FIRST_CARD + timedelta(days=14), end=None),
    )
    picked = UfcScheduler.select_target(calendar, FIRST_CARD + timedelta(hours=3))
    assert picked is not None
    assert picked.label == "UFC 330"


def test_select_target_rolls_forward_once_the_card_is_long_past(scheduler):
    from obbyschedule import CalendarEntry

    calendar = (
        CalendarEntry(label="UFC 330", start=FIRST_CARD, end=None),
        CalendarEntry(label="UFC 331", start=FIRST_CARD + timedelta(days=14), end=None),
    )
    picked = UfcScheduler.select_target(calendar, FIRST_CARD + timedelta(days=1))
    assert picked is not None
    assert picked.label == "UFC 331"


def test_select_target_returns_none_when_the_season_is_over():
    assert UfcScheduler.select_target((), FIRST_CARD) is None


# ------------------------------------------------------- ownership continuity
def test_tracking_a_new_event_drops_ownership_when_idle():
    state = ScheduleState()
    state.current_event_id = "old"
    assert state.track("new") is True
    assert state.started_by_scheduler is False
    assert state.started_at is None


def test_ownership_carries_across_an_upstream_id_change():
    """A changed ESPN id must not orphan a stream the scheduler started.

    The stand-down branch only fires for scheduler-owned events, so dropping
    ownership here would leave ffmpeg running until a human noticed.
    """
    state = armed_state()
    started_at = state.started_at

    assert state.track("a-different-espn-id") is True

    assert state.started_by_scheduler is True
    assert state.started_at == started_at
    assert state.final_seen_at is None


def test_carried_ownership_still_honours_the_failsafe(scheduler, settings):
    state = armed_state()
    state.track("a-different-espn-id")
    event = build_event(event_id="a-different-espn-id")
    now = FIRST_CARD + timedelta(hours=settings.max_runtime_hours)

    decision = scheduler.decide(now, event, state, settings, stream_running=True)

    assert decision.action is SchedulerAction.STOP


# ------------------------------------------------------------- self-healing
def test_rearms_when_the_encode_dies_mid_card(scheduler, settings):
    """Level-triggered: a crash the watchdog could not recover must not strand the card."""
    decision = scheduler.decide(MAIN_CARD, build_event(), armed_state(), settings, stream_running=False)

    assert decision.action is SchedulerAction.START
    assert "re-arming" in decision.reason


def test_does_not_rearm_once_the_card_is_final(scheduler, settings):
    state = armed_state(final_seen_at=MAIN_CARD)
    decision = scheduler.decide(MAIN_CARD + timedelta(minutes=1), build_event(final=True), state, settings, stream_running=False)

    assert decision.action is not SchedulerAction.START


# ------------------------------------------------------------------ adoption
def test_adopts_a_stream_that_was_already_running_when_the_card_began(scheduler, settings):
    """Without this the feature is a no-op unless the operator pressed Stop first."""
    assert scheduler.should_adopt(MAIN_CARD, build_event(), ScheduleState(), settings, stream_running=True) is True


def test_adoption_makes_the_stand_down_fire(scheduler, settings):
    """The whole point: an adopted stream still gets stood down after the card."""
    state = ScheduleState()
    assert scheduler.should_adopt(MAIN_CARD, build_event(), state, settings, stream_running=True)
    state.begin_event(EVENT_ID, by_scheduler=True)
    state.final_seen_at = MAIN_CARD.timestamp()

    now = MAIN_CARD + timedelta(minutes=settings.end_grace_minutes)
    decision = scheduler.decide(now, build_event(final=True), state, settings, stream_running=True)

    assert decision.action is SchedulerAction.STOP


def test_does_not_adopt_before_the_card_window_opens(scheduler, settings):
    now = FIRST_CARD - timedelta(hours=3)
    assert scheduler.should_adopt(now, build_event(), ScheduleState(), settings, stream_running=True) is False


def test_does_not_adopt_a_stopped_stream(scheduler, settings):
    assert scheduler.should_adopt(MAIN_CARD, build_event(), ScheduleState(), settings, stream_running=False) is False


def test_does_not_adopt_a_finished_card(scheduler, settings):
    assert scheduler.should_adopt(MAIN_CARD, build_event(final=True), ScheduleState(), settings, stream_running=True) is False


def test_does_not_adopt_an_event_the_operator_vetoed(scheduler, settings):
    """Stop means 'not this card' — adoption must not quietly undo that."""
    state = ScheduleState()
    state.suppressed_event_id = EVENT_ID
    assert scheduler.should_adopt(MAIN_CARD, build_event(), state, settings, stream_running=True) is False


def test_does_not_adopt_an_already_handled_card(scheduler, settings):
    state = ScheduleState()
    state.handled_event_id = EVENT_ID
    assert scheduler.should_adopt(MAIN_CARD, build_event(), state, settings, stream_running=True) is False


def test_disabling_auto_schedule_opts_out_of_adoption(scheduler):
    disabled = ScheduleSettings.from_config({"enabled": False})
    assert scheduler.should_adopt(MAIN_CARD, build_event(), ScheduleState(), disabled, stream_running=True) is False


def test_does_not_re_adopt_what_it_already_owns(scheduler, settings):
    assert scheduler.should_adopt(MAIN_CARD, build_event(), armed_state(), settings, stream_running=True) is False
