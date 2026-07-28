import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// The panels live in App.tsx, which imports video.js at module load. Mock it so
// importing the panels does not spin up a real player in jsdom.
vi.mock("video.js", () => ({ default: () => ({ dispose() {}, ready() {} }) }));

import { BlacklistPanel, CommandHeader, PublicStreamsPanel, SchedulePanel, SourcesPanel } from "./App";
import type { BlacklistEntry, PublicStreamSource, SourceStatus, StatusPayload } from "./types";

const noop = async () => {};

describe("SourcesPanel", () => {
  it("renders a Block button that calls onBlock with the source", () => {
    const source: SourceStatus = { id: "private-iptv-x", label: "UFC Main", url: "https://s/x.m3u8" };
    const onBlock = vi.fn(async () => {});
    render(
      <SourcesPanel
        sources={[source]}
        pending={false}
        onAdd={noop}
        onRemove={noop}
        onActivate={noop}
        onRecover={noop}
        onBlock={onBlock}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Block" }));
    expect(onBlock).toHaveBeenCalledWith(source);
  });
});

describe("PublicStreamsPanel", () => {
  it("renders a Block button that calls onBlock with the public source", () => {
    const source: PublicStreamSource = { id: "pub-1", label: "Backup", url: "https://p/1.m3u8", origin: "manual" };
    const onBlock = vi.fn(async () => {});
    render(
      <PublicStreamsPanel
        sources={[source]}
        pending={false}
        onAdd={noop}
        onRemove={noop}
        onScrape={async () => ({ count: 0 })}
        onBlock={onBlock}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Block" }));
    expect(onBlock).toHaveBeenCalledWith(source);
  });
});

describe("BlacklistPanel", () => {
  it("lists entries and unblocks them", () => {
    const entry: BlacklistEntry = { url: "https://blocked/x.m3u8", label: "Bad Feed", reason: "slate" };
    const onUnblock = vi.fn(async () => {});
    render(<BlacklistPanel entries={[entry]} pending={false} onUnblock={onUnblock} />);
    expect(screen.getByText("Bad Feed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Unblock" }));
    expect(onUnblock).toHaveBeenCalledWith(entry);
  });

  it("shows an empty state when there are no entries", () => {
    render(<BlacklistPanel entries={[]} pending={false} onUnblock={noop} />);
    expect(screen.getByText(/No blacklisted sources/i)).toBeInTheDocument();
  });
});

describe("CommandHeader", () => {
  it("shows the STOPPED banner and a Resume button when operator-stopped", () => {
    const status = { ok: true, config: { stream: { operator_stopped: true } } } as StatusPayload;
    render(<CommandHeader status={status} pendingAction="" onStreamAction={noop} />);
    expect(screen.getByText(/STOPPED \(manual\)/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
  });

  it("shows a normal Start button when running", () => {
    const status = { ok: true, config: { stream: { operator_stopped: false } } } as StatusPayload;
    render(<CommandHeader status={status} pendingAction="" onStreamAction={noop} />);
    expect(screen.queryByText(/STOPPED \(manual\)/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
  });

  it("reads a Stop as STANDBY while auto-schedule is armed", () => {
    const status = {
      ok: true,
      config: { stream: { operator_stopped: true } },
      schedule: {
        enabled: true,
        countdown_seconds: 7200,
        next_event: { label: "UFC 330: Makhachev vs. Machado Garry", start: "2026-08-16T00:00:00+00:00" },
      },
    } as StatusPayload;

    render(<CommandHeader status={status} pendingAction="" onStreamAction={noop} />);

    expect(screen.getByText(/STANDBY/i)).toBeInTheDocument();
    expect(screen.getByText(/UFC 330: Makhachev vs\. Machado Garry/)).toBeInTheDocument();
    expect(screen.getByText(/2h 0m/)).toBeInTheDocument();
    // The hard-stop wording must not appear — the stream is parked, not dead.
    expect(screen.queryByText(/STOPPED \(manual\)/i)).not.toBeInTheDocument();
  });

  it("still shows the hard STOPPED banner when auto-schedule is off", () => {
    const status = {
      ok: true,
      config: { stream: { operator_stopped: true } },
      schedule: { enabled: false },
    } as StatusPayload;

    render(<CommandHeader status={status} pendingAction="" onStreamAction={noop} />);

    expect(screen.getByText(/STOPPED \(manual\)/i)).toBeInTheDocument();
    expect(screen.queryByText(/STANDBY/i)).not.toBeInTheDocument();
  });

  it("toggles auto-schedule from the header switch", () => {
    const status = { ok: true, config: {}, schedule: { enabled: false } } as StatusPayload;
    const onToggleSchedule = vi.fn(async () => {});

    render(<CommandHeader status={status} pendingAction="" onStreamAction={noop} onToggleSchedule={onToggleSchedule} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /Auto-schedule/i }));

    expect(onToggleSchedule).toHaveBeenCalledWith(true);
  });
});

describe("SchedulePanel", () => {
  const baseSchedule = {
    enabled: true,
    notify_enabled: true,
    phase: "live",
    reason: "event in progress",
    countdown_seconds: 0,
    event: {
      id: "1",
      name: "UFC Fight Night: Ankalaev vs. Guskov",
      short_name: "Ankalaev vs. Guskov",
      venue: "Etihad Arena",
      city: "Abu Dhabi, United Arab Emirates",
      is_final: false,
      main_event: "Magomed Ankalaev vs. Bogdan Guskov",
      winner: null,
      first_card_start: "2026-07-25T13:00:00+00:00",
      cards: [
        { label: "Prelims", start: "2026-07-25T13:00:00+00:00", bouts: 7, completed: 7, all_final: true },
        { label: "Main card", start: "2026-07-25T16:00:00+00:00", bouts: 5, completed: 2, all_final: false },
      ],
    },
  };

  it("renders each card segment with its bout progress", () => {
    render(<SchedulePanel schedule={baseSchedule as never} pending={false} onTest={noop} />);

    expect(screen.getByText("UFC Fight Night: Ankalaev vs. Guskov")).toBeInTheDocument();
    expect(screen.getByText("Prelims")).toBeInTheDocument();
    expect(screen.getByText("7/7 bouts")).toBeInTheDocument();
    expect(screen.getByText("2/5 bouts")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("fires a Discord test embed", () => {
    const onTest = vi.fn(async () => {});
    render(<SchedulePanel schedule={baseSchedule as never} pending={false} onTest={onTest} />);

    fireEvent.click(screen.getByRole("button", { name: "Test Discord" }));
    expect(onTest).toHaveBeenCalled();
  });

  it("disables the test button without a webhook", () => {
    const schedule = { ...baseSchedule, notify_enabled: false };
    render(<SchedulePanel schedule={schedule as never} pending={false} onTest={noop} />);
    expect(screen.getByRole("button", { name: "Test Discord" })).toBeDisabled();
  });

  it("explains itself when auto-schedule is off", () => {
    render(<SchedulePanel schedule={{ enabled: false } as never} pending={false} onTest={noop} />);
    expect(screen.getByText(/Auto-schedule is off/i)).toBeInTheDocument();
  });
});
