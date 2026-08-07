"""Milestone scheduling and Discord embed shapes.

``due_milestones`` is pure, and ``EmbedBuilder`` is dict-in/dict-out, so the
whole notification path is asserted offline.
"""

from datetime import UTC, datetime, timedelta

import pytest

from obbyschedule import (
    CardSegment,
    EmbedBuilder,
    Milestone,
    MilestoneKind,
    NotifySettings,
    ScheduleSettings,
    ScheduleState,
    UfcEvent,
    UfcScheduler,
)
from obbyschedule.notify import humanize_minutes

FIRST_CARD = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
MAIN_CARD = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)
EVENT_ID = "600060000"
WEBHOOK = "https://discord.com/api/webhooks/1/token"


def build_event(*, final=False):
    return UfcEvent(
        event_id=EVENT_ID,
        name="UFC Fight Night: Medić vs. Rodriguez",
        short_name="Medić vs. Rodriguez",
        venue="Belgrade Arena",
        city="Belgrade, Serbia",
        cards=(
            CardSegment(start=FIRST_CARD, label="Prelims", bout_count=6, completed_bouts=6 if final else 0),
            CardSegment(start=MAIN_CARD, label="Main card", bout_count=5, completed_bouts=5 if final else 0),
        ),
        is_final=final,
        main_event_bout="Ante Medić vs. Daniel Rodriguez",
        main_event_winner="Ante Medić" if final else None,
    )


@pytest.fixture
def scheduler():
    return UfcScheduler.__new__(UfcScheduler)


@pytest.fixture
def settings():
    return ScheduleSettings.from_config({"notify": {"discord_webhook_url": WEBHOOK}})


def keys(milestones):
    return [milestone.key for milestone in milestones]


# ----------------------------------------------------------- warning ladder
@pytest.mark.parametrize("minutes", [1440, 720, 360, 120, 30])
def test_each_warning_fires_at_its_moment(scheduler, settings, minutes):
    now = FIRST_CARD - timedelta(minutes=minutes)
    due = scheduler.due_milestones(now, build_event(), ScheduleState(), settings)
    assert f"warn:{minutes}" in keys(due)


def test_no_warning_before_the_24h_mark(scheduler, settings):
    now = FIRST_CARD - timedelta(hours=25)
    assert scheduler.due_milestones(now, build_event(), ScheduleState(), settings) == []


def test_warning_does_not_refire_once_recorded(scheduler, settings):
    now = FIRST_CARD - timedelta(minutes=360)
    state = ScheduleState()

    first_pass = scheduler.due_milestones(now, build_event(), state, settings)
    assert "warn:360" in keys(first_pass)
    for milestone in first_pass:
        state.mark_fired(EVENT_ID, milestone.key)

    assert scheduler.due_milestones(now, build_event(), state, settings) == []


def test_stale_warning_is_dropped_not_fired(scheduler, settings):
    """After a redeploy we want silence, not a burst of hours-late countdowns."""
    now = FIRST_CARD - timedelta(minutes=360) + timedelta(minutes=settings.notify.max_late_minutes + 1)
    assert "warn:360" not in keys(scheduler.due_milestones(now, build_event(), ScheduleState(), settings))


def test_warnings_stop_once_the_card_is_final(scheduler, settings):
    now = FIRST_CARD - timedelta(minutes=30)
    due = scheduler.due_milestones(now, build_event(final=True), ScheduleState(), settings)
    assert not [milestone for milestone in due if milestone.kind is MilestoneKind.WARNING]


def test_custom_warn_ladder_is_honoured(scheduler):
    custom = ScheduleSettings.from_config({"notify": {"discord_webhook_url": WEBHOOK, "warn_minutes": [90]}})
    now = FIRST_CARD - timedelta(minutes=90)
    assert keys(scheduler.due_milestones(now, build_event(), ScheduleState(), custom)) == ["warn:90"]


# ------------------------------------------------------------- card + ending
def test_card_start_fires_for_each_segment(scheduler, settings):
    prelims = scheduler.due_milestones(FIRST_CARD, build_event(), ScheduleState(), settings)
    assert f"card:{FIRST_CARD.isoformat()}" in keys(prelims)

    main = scheduler.due_milestones(MAIN_CARD, build_event(), ScheduleState(), settings)
    assert f"card:{MAIN_CARD.isoformat()}" in keys(main)


def test_event_end_fires_from_the_grace_stamp(scheduler, settings):
    state = ScheduleState()
    state.final_seen_at = MAIN_CARD.timestamp()
    due = scheduler.due_milestones(MAIN_CARD + timedelta(minutes=1), build_event(final=True), state, settings)
    assert "end" in keys(due)


def test_event_end_needs_a_grace_stamp(scheduler, settings):
    due = scheduler.due_milestones(MAIN_CARD, build_event(final=True), ScheduleState(), settings)
    assert "end" not in keys(due)


def test_milestones_are_ordered_oldest_first(scheduler, settings):
    now = FIRST_CARD + timedelta(minutes=5)
    due = scheduler.due_milestones(now, build_event(), ScheduleState(), settings)
    assert [milestone.due for milestone in due] == sorted(milestone.due for milestone in due)


def test_nothing_fires_without_a_webhook(scheduler):
    bare = ScheduleSettings.from_config({})
    assert scheduler.due_milestones(FIRST_CARD, build_event(), ScheduleState(), bare) == []


def test_card_start_can_be_disabled(scheduler):
    quiet = ScheduleSettings.from_config({"notify": {"discord_webhook_url": WEBHOOK, "notify_card_start": False}})
    due = scheduler.due_milestones(FIRST_CARD, build_event(), ScheduleState(), quiet)
    assert not [milestone for milestone in due if milestone.kind is MilestoneKind.CARD_START]


# -------------------------------------------------------------- sweep_stale
def test_sweep_marks_long_past_milestones_so_they_never_fire(scheduler, settings):
    state = ScheduleState()
    now = MAIN_CARD + timedelta(hours=6)

    scheduler.sweep_stale(now, build_event(), state, settings)

    assert state.has_fired(EVENT_ID, "warn:1440")
    assert state.has_fired(EVENT_ID, f"card:{FIRST_CARD.isoformat()}")
    assert scheduler.due_milestones(now, build_event(), state, settings) == []


def test_sweep_leaves_upcoming_milestones_alone(scheduler, settings):
    state = ScheduleState()
    scheduler.sweep_stale(FIRST_CARD - timedelta(hours=23), build_event(), state, settings)
    assert not state.has_fired(EVENT_ID, "warn:720")


# --------------------------------------------------------------- state ledger
def test_notification_ledger_survives_a_round_trip():
    state = ScheduleState()
    state.mark_fired(EVENT_ID, "warn:360")
    revived = ScheduleState.from_json(state.to_json())
    assert revived.has_fired(EVENT_ID, "warn:360")


def test_cached_event_and_shutdown_deadline_survive_a_round_trip():
    state = ScheduleState(
        current_event_id=EVENT_ID,
        started_by_scheduler=True,
        armed_at=FIRST_CARD.timestamp(),
        hard_stop_at=(FIRST_CARD + timedelta(hours=8)).timestamp(),
        cached_event=build_event(),
        event_fetched_at=FIRST_CARD.timestamp(),
        active_segment_key=FIRST_CARD.isoformat(),
        last_source_refresh_at=FIRST_CARD.timestamp(),
    )

    revived = ScheduleState.from_json(state.to_json())

    assert revived.cached_event == build_event()
    assert revived.hard_stop_at == state.hard_stop_at
    assert revived.active_segment_key == FIRST_CARD.isoformat()
    assert revived.last_source_refresh_at == FIRST_CARD.timestamp()


def test_ledger_is_pruned_to_recent_events():
    state = ScheduleState()
    for index in range(40):
        state.mark_fired(f"event-{index}", "end")
    assert len(state.notified) <= 24
    assert "event-39" in state.notified


# -------------------------------------------------------------------- embeds
@pytest.fixture
def builder():
    return EmbedBuilder(NotifySettings.from_config({"discord_webhook_url": WEBHOOK}))


def field_names(embed):
    return [field["name"] for field in embed["fields"]]


def test_warning_embed_links_to_the_watcher(builder):
    embed = builder.warning(build_event(), 360)

    assert embed["url"] == "https://fight.nswfiles.com/"
    assert "6 hours away" in embed["title"]
    assert field_names(embed) == ["Prelims", "Main card", "Watch"]
    assert "fight.nswfiles.com" in embed["fields"][-1]["value"]
    assert embed["footer"]["text"] == "Belgrade Arena · Belgrade, Serbia"


def test_warning_embed_uses_discord_dynamic_timestamps(builder):
    """Card times must render in each reader's own timezone, not a fixed one."""
    embed = builder.warning(build_event(), 1440)
    prelims = embed["fields"][0]["value"]
    assert f"<t:{int(FIRST_CARD.timestamp())}:f>" in prelims
    assert f"<t:{int(FIRST_CARD.timestamp())}:R>" in prelims


def test_card_start_embed_names_the_segment(builder):
    event = build_event()
    embed = builder.card_start(event, event.cards[1])

    assert embed["title"] == "🔴 LIVE NOW — Main card"
    assert "5 bouts" in embed["description"]
    assert "Ante Medić vs. Daniel Rodriguez" in embed["description"]
    assert embed["url"] == "https://fight.nswfiles.com/"


def test_prelims_embed_omits_the_main_event_teaser(builder):
    event = build_event()
    embed = builder.card_start(event, event.cards[0])
    assert "Main event" not in embed["description"]


def test_event_end_embed_carries_the_result(builder):
    embed = builder.event_end(build_event(final=True), next_event_label="UFC 330: Makhachev vs. Machado Garry")

    assert "has ended" in embed["title"]
    assert "Ante Medić" in embed["description"]
    assert "standby" in embed["description"]
    assert "Up next" in field_names(embed)


def test_for_milestone_dispatches_each_kind(builder):
    event = build_event(final=True)
    card = event.cards[1]

    warning = builder.for_milestone(event, Milestone(MilestoneKind.WARNING, "warn:30", FIRST_CARD, "30", minutes=30))
    started = builder.for_milestone(event, Milestone(MilestoneKind.CARD_START, "card", MAIN_CARD, "Main card", card=card))
    ended = builder.for_milestone(event, Milestone(MilestoneKind.EVENT_END, "end", MAIN_CARD, "ended"))

    assert "30 minutes away" in warning["title"]
    assert "LIVE NOW" in started["title"]
    assert "has ended" in ended["title"]


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(1440, "24 hours"), (2880, "2 days"), (720, "12 hours"), (120, "2 hours"), (60, "1 hour"), (30, "30 minutes"), (1, "1 minute")],
)
def test_humanize_minutes(minutes, expected):
    assert humanize_minutes(minutes) == expected


# ------------------------------------------------------------- "Coming up"
def test_coming_up_embed_lists_every_card_in_multiple_timezones(builder):
    embed = builder.coming_up(build_event())

    assert embed["title"].startswith("📅 Coming up —")
    assert embed["url"] == "https://fight.nswfiles.com/"
    assert field_names(embed) == ["Prelims · 6 bouts", "Main card · 5 bouts", "Watch"]

    prelims = embed["fields"][0]["value"]
    # A static table, so the times survive being quoted or screenshotted.
    assert prelims.startswith("```")
    for city in ("Los Angeles", "New York", "London", "Sydney"):
        assert city in prelims


def test_coming_up_converts_each_zone_correctly(builder):
    """17:00 UTC is 10:00 in Los Angeles and 13:00 in New York."""
    prelims = builder.coming_up(build_event())["fields"][0]["value"]

    assert "10:00 AM PDT" in prelims
    assert "1:00 PM EDT" in prelims
    assert "6:00 PM BST" in prelims


def test_coming_up_rolls_the_date_over_for_far_east_zones(builder):
    """A Saturday-evening US card is Sunday morning in Sydney."""
    prelims = builder.coming_up(build_event())["fields"][0]["value"]
    sydney = next(line for line in prelims.splitlines() if line.startswith("Sydney"))
    assert "Sun 2 Aug" in sydney


def test_coming_up_includes_the_venue_and_main_event(builder):
    embed = builder.coming_up(build_event())
    assert "Ante Medić vs. Daniel Rodriguez" in embed["description"]
    assert "Belgrade Arena" in embed["description"]


def test_timezone_table_alignment_and_bad_zones():
    from obbyschedule.notify import timezone_table

    moment = FIRST_CARD
    table = timezone_table(moment, (("America/New_York", "New York"), ("Not/AZone", "Nowhere")))
    assert "New York" in table
    assert "Nowhere" not in table  # unknown zones are dropped, not raised
    assert timezone_table(moment, ()) == ""


def test_timezone_config_accepts_strings_and_mappings():
    from obbyschedule import NotifySettings

    settings = NotifySettings.from_config(
        {"discord_webhook_url": WEBHOOK, "timezones": ["Asia/Tokyo", {"zone": "Europe/Berlin", "label": "Berlin"}, "Bad/Zone"]}
    )
    assert settings.timezones == (("Asia/Tokyo", "Tokyo"), ("Europe/Berlin", "Berlin"))


def test_timezone_config_falls_back_when_all_entries_are_junk():
    from obbyschedule import DEFAULT_TIMEZONES, NotifySettings

    settings = NotifySettings.from_config({"discord_webhook_url": WEBHOOK, "timezones": ["Bad/Zone", ""]})
    assert settings.timezones == DEFAULT_TIMEZONES


def test_for_milestone_dispatches_coming_up(builder):
    embed = builder.for_milestone(build_event(), Milestone(MilestoneKind.COMING_UP, "coming_up", FIRST_CARD, "coming up"))
    assert embed["title"].startswith("📅 Coming up")


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("America/New_York", "EDT"),
        ("Europe/London", "BST"),
        # tzdata has no letter abbreviation for these; %Z gives a bare offset.
        ("Asia/Dubai", "UTC+4"),
        ("Asia/Kathmandu", "UTC+5:45"),
        ("Asia/Kolkata", "IST"),
    ],
)
def test_zone_abbreviations_are_readable(zone, expected):
    from obbyschedule.models import load_zone
    from obbyschedule.notify import zone_abbrev

    assert zone_abbrev(FIRST_CARD.astimezone(load_zone(zone))) == expected


def test_an_abandoned_milestone_is_flagged_not_silently_marked_sent(scheduler, settings):
    """A Discord outage longer than max_late_minutes used to convert 'undelivered,
    will retry' into 'delivered': notify_due deliberately refuses to mark a failed
    send as fired, but sweep_stale wrote the same ledger key once it aged out, and
    staleness won. The channel never heard about the card, and nothing said so."""
    state = ScheduleState()
    now = MAIN_CARD + timedelta(hours=6)
    said = []
    scheduler._event_log = lambda message, level: said.append((message, level))

    scheduler.sweep_stale(now, build_event(), state, settings)

    # Still retired, so it cannot re-fire and spam after a redeploy.
    assert state.has_fired(EVENT_ID, "warn:1440")
    # ...but a human can now find out it never went out.
    warned = [m for m, level in said if "never delivered" in m and level == "warn"]
    assert warned, f"abandoned milestones were swept silently: {said}"


def test_abandoning_is_reported_once_not_every_tick(scheduler, settings):
    """Otherwise the anti-spam property of the ledger is traded for log spam."""
    state = ScheduleState()
    now = MAIN_CARD + timedelta(hours=6)
    said = []
    scheduler._event_log = lambda message, level: said.append((message, level))

    scheduler.sweep_stale(now, build_event(), state, settings)
    first = len(said)
    scheduler.sweep_stale(now, build_event(), state, settings)

    assert len(said) == first, "re-reported an already-abandoned milestone"
