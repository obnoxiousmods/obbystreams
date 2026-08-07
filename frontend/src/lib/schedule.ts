// Pure, framework-free helpers for the UFC auto-schedule panel.
// Kept out of App.tsx so the countdown/banner logic is unit-testable without a DOM.

import type { EventPhase, ScheduleSnapshot, StatusPayload, StopReason } from "../types";

/** How the header banner should read while the stream is down. */
export type StandbyMode = "running" | "standby" | "stopped";

export function scheduleOf(status: StatusPayload | null | undefined): ScheduleSnapshot | null {
  return status?.schedule ?? null;
}

export function scheduleEnabled(status: StatusPayload | null | undefined): boolean {
  return Boolean(status?.schedule?.enabled);
}

export function stopReasonOf(status: StatusPayload | null | undefined): StopReason | "" {
  return status?.runtime?.stop_reason ?? "";
}

/**
 * Distinguish a *standby* (auto-schedule will wake it up) from a hard *stopped*.
 *
 * With auto-schedule on, pressing Stop parks the cockpit rather than killing it,
 * so the banner must not claim the stream is down for good.
 */
export function standbyMode(status: StatusPayload | null | undefined, operatorStopped: boolean): StandbyMode {
  if (!operatorStopped) return "running";
  return scheduleEnabled(status) ? "standby" : "stopped";
}

/**
 * Format a duration as a compact countdown: "21d 4h", "3h 12m", "45s".
 * Returns "now" at or past zero so a live card never shows a negative clock.
 */
export function formatCountdown(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.floor(seconds));
  if (total === 0) return "now";

  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;

  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${secs}s`;
}

/** Human label for the scheduler's current phase. */
export function phaseLabel(phase: EventPhase | undefined): string {
  switch (phase) {
    case "pre_roll":
      return "Pre-roll";
    case "live":
      return "Live";
    case "wrapping":
      return "Wrapping up";
    case "finished":
      return "Finished";
    case "pending":
      return "Scheduled";
    default:
      return "Idle";
  }
}

export function phaseTone(phase: EventPhase | undefined): "ok" | "warn" | "info" | "neutral" {
  switch (phase) {
    case "live":
      return "ok";
    case "pre_roll":
    case "wrapping":
      return "warn";
    case "pending":
      return "info";
    default:
      return "neutral";
  }
}

/** Name of the card the cockpit is waiting on, preferring the loaded event detail. */
export function upcomingLabel(schedule: ScheduleSnapshot | null | undefined): string | null {
  if (!schedule) return null;
  return schedule.event?.name ?? schedule.next_event?.label ?? null;
}

/**
 * Whether the operator vetoed the card currently being tracked.
 *
 * Pressing Stop mid-card means "not this one" — the scheduler stays armed for
 * the next event but will not resume this one.
 */
export function isCurrentEventSuppressed(schedule: ScheduleSnapshot | null | undefined): boolean {
  const suppressed = schedule?.suppressed_event_id;
  return Boolean(suppressed && schedule?.event?.id === suppressed);
}

/**
 * Whether the cockpit is armed for a card but holding with nothing on air.
 *
 * This is a deliberate state, not a fault: putting an unidentified feed on air
 * is worse than putting nothing on. It still needs saying out loud, because
 * from the outside it looks exactly like a broken scheduler.
 */
export function isAwaitingSource(schedule: ScheduleSnapshot | null | undefined): boolean {
  return Boolean(schedule?.enabled && schedule?.awaiting_source);
}

/** The "still hunting for tonight's feed" line, or null when a feed is on air. */
export function acquisitionBannerText(schedule: ScheduleSnapshot | null | undefined): string | null {
  if (!isAwaitingSource(schedule)) return null;
  const label = schedule?.event?.short_name ?? upcomingLabel(schedule) ?? "this card";
  const rejected = schedule?.source_state?.rejected?.length ?? 0;
  const attempts = schedule?.source_state?.acquire_attempts ?? 0;
  const detail = rejected ? ` — ${rejected} candidate${rejected === 1 ? "" : "s"} rejected (wrong event)` : "";
  const tries = attempts > 1 ? ` after ${attempts} sweeps` : "";
  return `🔍 Acquiring a verified source for ${label}${tries}${detail}. Nothing goes on air until one matches.`;
}

/** The one-line banner shown while the stream is parked. */
export function standbyBannerText(schedule: ScheduleSnapshot | null | undefined): string {
  // A vetoed card must not advertise a countdown it will never act on.
  if (isCurrentEventSuppressed(schedule)) {
    return "⏸ STOPPED for this card — auto-schedule resumes at the next event.";
  }
  // Armed and hunting beats "standby": the card has already started.
  const acquiring = acquisitionBannerText(schedule);
  if (acquiring) return acquiring;
  const label = upcomingLabel(schedule);
  const countdown = formatCountdown(schedule?.countdown_seconds);
  if (!label) return "⏸ STANDBY — waiting for the next UFC card.";
  if (countdown === "—") return `⏸ STANDBY — auto-starts for ${label}.`;
  return `⏸ STANDBY — auto-starts for ${label} in ${countdown}.`;
}

/** Local wall-clock rendering of an ISO card time, e.g. "Sat 10:00 AM". */
export function formatCardTime(iso: string | null | undefined): string {
  if (!iso) return "TBA";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "TBA";
  return when.toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Progress through a card segment, for the little "3/5 bouts" readout. */
export function cardProgress(completed: number, bouts: number): string {
  if (!bouts) return "—";
  return `${Math.min(completed, bouts)}/${bouts} bouts`;
}
