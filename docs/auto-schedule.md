# UFC auto-schedule

Turns the operator **Stop** into a **standby**: the cockpit wakes itself up 10 minutes before a
UFC card's earliest published segment, follows every segment, and stands itself back down 30 minutes
after every bout is decided.
Along the way it posts countdown / go-live / wrap-up embeds to Discord.

Motivation: before this, the NVENC transcode ran 24/7 (~39 W) even hours after a card ended, because
the only way to stop it was a manual Stop that stayed down until a human pressed Start.

## Where the code lives

The feature is a self-contained package, `obbyschedule/`, which **never imports `app.py`** — the
cockpit injects its primitives as callbacks, so the dependency runs one way only.

| Module | Contents |
|---|---|
| `models.py` | Frozen dataclasses + `StrEnum`s. `ScheduleSettings.from_config` does all coercion/bounding. No I/O. |
| `espn.py` | `EspnScheduleProvider`. Pure `@staticmethod` parsers (`parse_calendar`, `parse_event`) so tests run on recorded JSON. |
| `notify.py` | `DiscordNotifier` (retries, 429 handling, never raises) + `EmbedBuilder` (dict in, dict out). |
| `state.py` | `ScheduleState` + `ScheduleStateStore` — atomic JSON at `schedule.state_path`. |
| `scheduler.py` | `UfcScheduler`. `decide()` and `due_milestones()` are **pure and synchronous**; `tick()`/`run()` are the async shell. |
| `protocols.py` | `ScheduleProvider` / `Notifier` structural interfaces, so stubs are type-legal. |

## Data source

`https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard` — public, unauthenticated.

* `leagues[0].calendar` → the season's cards (`label`, `startDate`, `endDate`).
* `events[].competitions[]` → one row per bout, each with its own `date` and
  `status.type.completed`. **Distinct dates are the broadcast segments** (early prelims / prelims /
  main card), and the completion flags are how the card's end is detected.

### Gotcha: the calendar start is not always the first bout

For the 2026-07-25 Abu Dhabi card the calendar reads **16:00Z** (main card) while the prelims open at
**13:00Z**. Countdowns therefore anchor on `first_card_start` from the *event detail*, and the detail
is loaded `max(warn_minutes) + 60m + CALENDAR_SKEW(12h)` ≈ 37h ahead so the 24-hour warning is never
missed. `next_wakeup()` keeps that wide window cheap by sleeping until the next milestone rather than
polling every two minutes for a day and a half.

### Gotcha: a scratched bout never completes

`STATUS_CANCELED` / `STATUS_POSTPONED` bouts are excluded from the "is everything done?" test, and a
completed main event also ends the card. Otherwise one cancelled prelim would hold the stream up
forever.

## Policy

`UfcScheduler.decide()` — evaluated fresh every tick (level-triggered, so a restart self-heals):

* **START** once `now >= first_card_start - lead_minutes` (default 10), if the card is not finished,
  not suppressed, and nothing is already running. Clears the operator Stop and kicks a private-IPTV
  refresh — the pre-roll is the one window where the spare provider connection is free.
* **STOP** once every bout is decided **and** `end_grace_minutes` (default 30) has elapsed since the
  card was first observed final, **or** when `max_runtime_hours` (default 8) trips as a failsafe if
  ESPN stalls. The failsafe is checked first so a stuck grace stamp cannot defeat it.
* **Re-arm** if the encode goes down mid-card (a crash the watchdog could not recover, or a scraper
  stand-down). The scheduler is level-triggered, so it re-issues START rather than sitting idle until
  the card ends.
* Only ever stands down a stream it **owns** (`started_by_scheduler`).

### Adoption — why it isn't "only what I started"

The obvious rule, *never touch a stream you did not start*, quietly makes the whole feature a no-op
in the case that actually matters. Production had the encode up for six days straight; if nobody
remembers to press Stop first, the scheduler never owns it, the stand-down never fires, and the card
ends with ffmpeg still burning — the exact 24/7 behaviour this replaces.

So `should_adopt()` takes ownership of an already-running stream, but only once the card is genuinely
in its window (`PRE_ROLL` or `LIVE`). It will **not** adopt a card the operator vetoed with Stop, one
already stood down, a finished card, or anything at all while auto-schedule is off. That keeps the
operator's explicit "not this card" intact while still guaranteeing the card ends in standby.

### Operator override

Pressing **Stop** during a card records `stream.stop_reason = "manual"` and stamps
`suppressed_event_id` with the card being tracked. That means *"not this event"* — the scheduler
stays armed for the next one. A manual **Start** or **Restart** lifts the suppression.

`schedule_start_stream` / `schedule_stop_stream` in `app.py` both take `PROCESS_LOCK`, which is what
serialises the scheduler against a human pressing Stop: whoever takes the lock first wins, and the
loser observes the other's effect.

### Ownership continuity

If ESPN changes an event's id mid-card, `ScheduleState.track()` **carries** scheduler ownership onto
the new id. Dropping it would orphan the encode, since the stand-down branch only fires for
scheduler-owned events.

## Notifications

Milestones per card, each fired at most once (deduped in the persisted ledger):

* **Coming up** — posted when a card is first tracked (~37h out), announcing what's next with a
  static per-timezone time table for every segment. See below.
* Countdowns at **24h / 12h / 6h / 2h / 30m** before the first bout (`notify.warn_minutes`).
* **Card start** for each segment.
* **Event end**, including the main-event winner from `competitors[].winner`.

### "Coming up" and the timezone table

Discord's `<t:…:R>` markup only localises for the person reading the message *in Discord*. That is
useless the moment someone quotes the post, screenshots it, or reads it through a bridge — and this
audience is spread across North America, Europe and APAC. So the "Coming up" embed carries a static,
aligned table per card segment:

```
Los Angeles  Sat 1 Aug, 10:00 AM PDT
New York     Sat 1 Aug, 1:00 PM EDT
London       Sat 1 Aug, 6:00 PM BST
Sydney       Sun 2 Aug, 3:00 AM AEST
```

Zones come from `notify.timezones`, which accepts bare IANA names or `{zone, label}` pairs. An
unknown zone is **dropped**, never rendered: `load_zone()` falls back to UTC, so a typo would
otherwise print a confidently wrong time under the right city name.

Operators can re-post it any time from the cockpit's **Post "Coming up"** button, or
`POST /api/schedule {"coming_up": true}` — that path force-fetches the next card's detail even when
the loop would not have loaded it yet, and bypasses the once-per-event ledger.

Card times use Discord's dynamic timestamp markup (`<t:epoch:f>` / `<t:epoch:R>`) so each reader sees
them in their own timezone. Every embed links to <https://fight.nswfiles.com/>.

A milestone more than `notify.max_late_minutes` (20) past due is **dropped, not fired** — after a
redeploy or an outage the channel should get silence, not a burst of stale countdowns. `sweep_stale()`
marks those as fired so they are not re-evaluated forever.

## Config

Lives under the top-level `schedule:` key.

```yaml
schedule:
  enabled: true
  espn_scoreboard_url: https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard
  include_pattern: '^UFC\s+(\d+|Fight Night)'
  exclude_pattern: 'Contender Series|Road to UFC'
  lead_minutes: 10
  end_grace_minutes: 30
  max_runtime_hours: 8
  calendar_refresh_seconds: 3600
  live_poll_seconds: 120
  acquisition_poll_seconds: 180  # retry until the current segment has a high-grade source
  cache_max_age_hours: 72        # validated detail survives a temporary ESPN outage
  stall_hours: 6                 # stand-down backstop when ESPN never marks the card final
  stall_idle_minutes: 45
  require_event_match: true      # a feed must identify the tracked card
  display_timezone: Canada/Pacific
  state_path: /etc/obbystreams/schedule_state.json
  notify:
    enabled: true
    discord_webhook_url: '<secret>'
    watcher_url: https://fight.nswfiles.com/
    warn_minutes: [1440, 720, 360, 120, 30]
    notify_card_start: true
    notify_event_end: true
    max_late_minutes: 20
```

> **`normalize_config` gotcha.** It rebuilds the config from `DEFAULT_CONFIG` on every save and only
> copies whitelisted sections. `schedule` is copied through explicitly in `normalize_config`; without
> that line an operator's settings would be silently erased by the next unrelated `save_config()`.
> There is a regression test for exactly this (`test_schedule_section_survives_an_unrelated_save`).

The webhook URL is a bearer credential and is masked to `"***"` by `public_config()`, so it never
leaves the box via `/api/config` or `/api/status`.

## API

* `GET /api/schedule` (guarded) → `{enabled, phase, event, next_event, countdown_seconds, cards, …}`
* `POST /api/schedule` (guarded) → `{"enabled": bool}` to toggle, `{"test_notification": true}` to
  post one test embed.
* The same snapshot is embedded in `/api/status` under `schedule`, so the SPA needs no extra polling.

## Event-aware source discovery

Arming on time is only half the job — the encode also has to be carrying *that card*. Until
2026-08-02 it frequently was not: the scraper's only questions were "does this channel say UFC?"
and "is there a date within 30h?", so the feeds auto-selected for one Saturday stayed selected the
next. On 2026-08-01 the cockpit armed perfectly for Medić vs. Rodriguez and then ran the entire
event on the three soursignal channels picked a week earlier for Ankalaev vs. Guskov, reporting
healthy the whole time (ffmpeg was decoding fine — it was simply the wrong fight).

Four things changed:

1. **The card's identity reaches the scraper.** `UfcEvent.context()` builds an `EventContext`
   (fighter surnames, the `UFC 330`-style event number, and ESPN's real per-segment start times).
   The scheduler publishes it every tick through the `SourceResolver` protocol, implemented in the
   cockpit by `CockpitSourceResolver`. Names are folded to ASCII (`normalize_match_text`), because
   ESPN says `Medić` and the provider says `MEDIC`.
2. **Discovery runs before the start, not after.** `UfcScheduler.tick()` refreshes sources before
   applying its START decision.
   Previously the encode came up on stale links and the refresh that followed then declined to touch
   a stream that looked healthy — `should_protect_live_private_stream` pinned the mistake for hours.
   Protection is now forfeited by a feed that cannot be the tracked card, or one chosen before the
   card moved to its next segment.
3. **Unverified means nothing goes on air.** `schedule_start_links()` only returns feeds tagged with
   the tracked `event_id`; with none, the cockpit stays armed with the encode *down* and retries on
   `acquisition_poll_seconds` (180s). Discord gets a warning if the card starts with nothing found.
   Public backup sources are pulled in after `public_fallback_after_attempts` failed sweeps, but only
   after the card is live; an unidentified fallback is never aired during pre-roll. If ffmpeg was
   already carrying a stale/unidentified source when pre-roll opens, it is replaced with a verified
   source or quarantined offline — decoder health alone is never treated as card identity.
4. **Segments switch dynamically.** ESPN can publish one segment (main card only), two (prelims +
   main), or three (early prelims + prelims + main). The earliest one opens pre-roll, and every later
   boundary forces discovery and selects only current-segment feeds.
5. **Switching is bounded.** Every swap costs viewers a few seconds, so `switch_cooldown_seconds`,
   `switch_confirm_samples` and `max_switches_per_card` stop it flapping mid-fight. A *confirmed
   wrong-card* feed overrides the cooldown: that is a correction, not an improvement.

Sources carry `event_id` + `discovered_at` in `stream.sources`. `purge_foreign_event_sources()` runs
at every arming and the stand-down retires the card's feeds and clears `locked_source_id`.
`GET /api/schedule` gains a `source_state` block (matched sources, match terms, rejected candidates
with reasons, switch count), and the cockpit renders it — including
`🔍 Acquiring a verified source for … — 6 candidates rejected (wrong event)`.

### Stand-down backstop

ESPN sometimes never flips a card's final flag. `card_stalled()` stands the stream down when the
last segment started more than `stall_hours` (6) ago *and* no bout has been decided for
`stall_idle_minutes` (45) — both conditions, so a genuinely slow card is never cut off mid-broadcast.
The absolute failsafe allows at least `max_runtime_hours` (8h) from the earliest segment **and** a
full five-hour runway from the Main card start, whichever is later. That keeps a three-block card's
early prelims from consuming the Main card's safety margin.

### Restart safety

With auto-schedule enabled, the service always boots into process standby. The scheduler's first
event-aware tick — not the watchdog — decides whether ffmpeg may start. The exact links handed to
the running process are tracked separately from mutable config, so a config refresh cannot make an
old process appear to be carrying a newly selected source. Mid-card crash recovery is gated through
the same current-event link selection, and the persisted absolute deadline remains enforceable even
when both ESPN and the cached detail are unavailable.

## Cockpit UI

* The header banner becomes state-aware: **`⏸ STANDBY — auto-starts for … in 3h 12m`** while
  auto-schedule is on, versus the existing red `⏹ STOPPED (manual)` when it is off.
* An **Auto-schedule** switch sits in the header actions.
* A `SchedulePanel` lists the tracked card, each segment's start time, and bout progress, plus a
  **Test Discord** button.
* Pure helpers live in `frontend/src/lib/schedule.ts` so the countdown/banner logic is vitest-testable
  without a DOM.

## Verifying

```bash
cd /opt/obbystreams
uv run ruff check . && uv run ty check && uv run pytest -q && npm test && npm run build

# Walk a whole event timeline offline — no network, no Discord, no ffmpeg:
uv run python tools/schedule_replay.py --step 10

# Against the real ESPN calendar (only meaningful for a card that hasn't happened yet):
uv run python tools/schedule_replay.py --live
```

Tests: `tests/test_schedule_parsing.py` (ESPN shapes), `tests/test_schedule_policy.py` (the state
machine on a fake clock), `tests/test_schedule_notifications.py` (milestones + embeds),
`tests/test_schedule_loop.py` (the async shell with stubs). Fixtures in `tests/fixtures/espn_*.json`
are trimmed captures of the real 2026-07-25 card plus pre / mid / cancelled variants.

Note the test harness never enters the ASGI lifespan, so `SCHEDULER` is `None` there — the endpoints
degrade to an "scheduler not running" snapshot rather than erroring.

## Deploying

Code changes require restarting `obbystreams.service`, which **interrupts the live encode**. Do it
in a non-event window and check `/api/status` for viewers first.
