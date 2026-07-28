import { describe, expect, it } from "vitest";
import type { ScheduleSnapshot, StatusPayload } from "../types";
import {
  cardProgress,
  formatCardTime,
  formatCountdown,
  isCurrentEventSuppressed,
  phaseLabel,
  phaseTone,
  scheduleEnabled,
  standbyBannerText,
  standbyMode,
  stopReasonOf,
  upcomingLabel,
} from "./schedule";

function status(schedule?: Partial<ScheduleSnapshot>, runtime?: Record<string, unknown>): StatusPayload {
  return {
    ok: true,
    config: {},
    ...(schedule ? { schedule: { enabled: true, ...schedule } as ScheduleSnapshot } : {}),
    ...(runtime ? { runtime } : {}),
  } as StatusPayload;
}

describe("standbyMode", () => {
  it("reports running when the operator has not stopped it", () => {
    expect(standbyMode(status({ enabled: true }), false)).toBe("running");
  });

  it("reads a Stop as standby while auto-schedule is on", () => {
    expect(standbyMode(status({ enabled: true }), true)).toBe("standby");
  });

  it("reads a Stop as a hard stop once auto-schedule is off", () => {
    expect(standbyMode(status({ enabled: false }), true)).toBe("stopped");
  });

  it("falls back to a hard stop when the payload has no schedule block", () => {
    expect(standbyMode(status(), true)).toBe("stopped");
    expect(standbyMode(null, true)).toBe("stopped");
  });
});

describe("scheduleEnabled / stopReasonOf", () => {
  it("reads the flags off the payload", () => {
    expect(scheduleEnabled(status({ enabled: true }))).toBe(true);
    expect(scheduleEnabled(status({ enabled: false }))).toBe(false);
    expect(scheduleEnabled(null)).toBe(false);
  });

  it("surfaces who stopped the stream", () => {
    expect(stopReasonOf(status(undefined, { stop_reason: "schedule" }))).toBe("schedule");
    expect(stopReasonOf(status(undefined, { stop_reason: "manual" }))).toBe("manual");
    expect(stopReasonOf(null)).toBe("");
  });
});

describe("formatCountdown", () => {
  it("renders days and hours for far-out cards", () => {
    expect(formatCountdown(21 * 86400 + 4 * 3600)).toBe("21d 4h");
  });

  it("renders hours and minutes within a day", () => {
    expect(formatCountdown(3 * 3600 + 12 * 60)).toBe("3h 12m");
  });

  it("renders bare minutes under an hour", () => {
    expect(formatCountdown(45 * 60)).toBe("45m");
  });

  it("renders seconds in the last minute", () => {
    expect(formatCountdown(45)).toBe("45s");
  });

  it("never shows a negative clock", () => {
    expect(formatCountdown(0)).toBe("now");
    expect(formatCountdown(-500)).toBe("now");
  });

  it("handles missing values", () => {
    expect(formatCountdown(null)).toBe("—");
    expect(formatCountdown(undefined)).toBe("—");
    expect(formatCountdown(Number.NaN)).toBe("—");
  });
});

describe("standbyBannerText", () => {
  it("names the next card and the countdown", () => {
    const text = standbyBannerText({
      enabled: true,
      countdown_seconds: 3 * 3600,
      next_event: { label: "UFC 330: Makhachev vs. Machado Garry", start: "2026-08-16T00:00:00+00:00" },
    } as ScheduleSnapshot);

    expect(text).toContain("STANDBY");
    expect(text).toContain("UFC 330: Makhachev vs. Machado Garry");
    expect(text).toContain("3h 0m");
  });

  it("prefers the loaded event detail over the calendar label", () => {
    const text = standbyBannerText({
      enabled: true,
      countdown_seconds: 60,
      next_event: { label: "calendar label", start: "" },
      event: { name: "detail name" },
    } as ScheduleSnapshot);

    expect(text).toContain("detail name");
  });

  it("degrades gracefully with nothing scheduled", () => {
    expect(standbyBannerText(null)).toContain("waiting for the next UFC card");
    expect(standbyBannerText({ enabled: true } as ScheduleSnapshot)).toContain("waiting for the next UFC card");
  });
});

describe("upcomingLabel", () => {
  it("returns null when nothing is known", () => {
    expect(upcomingLabel(null)).toBeNull();
    expect(upcomingLabel({ enabled: true } as ScheduleSnapshot)).toBeNull();
  });
});

describe("phaseLabel / phaseTone", () => {
  it("maps each phase to a readable label", () => {
    expect(phaseLabel("pre_roll")).toBe("Pre-roll");
    expect(phaseLabel("live")).toBe("Live");
    expect(phaseLabel("wrapping")).toBe("Wrapping up");
    expect(phaseLabel("pending")).toBe("Scheduled");
    expect(phaseLabel(undefined)).toBe("Idle");
  });

  it("tones live green and transitional states amber", () => {
    expect(phaseTone("live")).toBe("ok");
    expect(phaseTone("pre_roll")).toBe("warn");
    expect(phaseTone("wrapping")).toBe("warn");
    expect(phaseTone(undefined)).toBe("neutral");
  });
});

describe("formatCardTime", () => {
  it("returns TBA for missing or unparseable times", () => {
    expect(formatCardTime(null)).toBe("TBA");
    expect(formatCardTime("")).toBe("TBA");
    expect(formatCardTime("not-a-date")).toBe("TBA");
  });

  it("formats a real ISO timestamp", () => {
    expect(formatCardTime("2026-08-01T17:00:00+00:00")).not.toBe("TBA");
  });
});

describe("cardProgress", () => {
  it("shows completed out of total", () => {
    expect(cardProgress(3, 5)).toBe("3/5 bouts");
  });

  it("never exceeds the total", () => {
    expect(cardProgress(9, 5)).toBe("5/5 bouts");
  });

  it("handles an empty card", () => {
    expect(cardProgress(0, 0)).toBe("—");
  });
});

describe("isCurrentEventSuppressed", () => {
  it("detects an operator veto on the tracked card", () => {
    const schedule = { enabled: true, suppressed_event_id: "abc", event: { id: "abc" } } as ScheduleSnapshot;
    expect(isCurrentEventSuppressed(schedule)).toBe(true);
  });

  it("ignores a veto that belongs to a different card", () => {
    const schedule = { enabled: true, suppressed_event_id: "old", event: { id: "abc" } } as ScheduleSnapshot;
    expect(isCurrentEventSuppressed(schedule)).toBe(false);
  });

  it("is false when nothing is vetoed", () => {
    expect(isCurrentEventSuppressed({ enabled: true } as ScheduleSnapshot)).toBe(false);
    expect(isCurrentEventSuppressed(null)).toBe(false);
  });

  it("does not advertise a countdown for a vetoed card", () => {
    // Pressing Stop mid-card would otherwise read "auto-starts ... in now".
    const schedule = {
      enabled: true,
      countdown_seconds: 0,
      suppressed_event_id: "abc",
      event: { id: "abc", name: "UFC Fight Night: Somebody vs. Someone" },
    } as ScheduleSnapshot;

    const text = standbyBannerText(schedule);
    expect(text).toContain("STOPPED for this card");
    expect(text).toContain("next event");
    expect(text).not.toContain("auto-starts");
  });
});
