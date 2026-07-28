import { describe, expect, it } from "vitest";
import { blacklistKey, blacklistPrimaryLabel, blockPayload, isOperatorStopped } from "./blacklist";
import type { StatusPayload } from "../types";

describe("blockPayload", () => {
  it("carries url/id/label and a default reason", () => {
    expect(blockPayload({ url: "https://x/y.m3u8", id: "s1", label: "S1" })).toEqual({
      url: "https://x/y.m3u8",
      id: "s1",
      label: "S1",
      reason: "blocked from cockpit",
    });
  });

  it("accepts a custom reason", () => {
    expect(blockPayload({ url: "https://x/y.m3u8" }, "slate").reason).toBe("slate");
  });
});

describe("isOperatorStopped", () => {
  it("is false for null/empty status", () => {
    expect(isOperatorStopped(null)).toBe(false);
    expect(isOperatorStopped({ ok: true, config: {} } as StatusPayload)).toBe(false);
  });

  it("reads the config flag", () => {
    const status = { ok: true, config: { stream: { operator_stopped: true } } } as StatusPayload;
    expect(isOperatorStopped(status)).toBe(true);
  });

  it("falls back to the runtime mirror", () => {
    const status = { ok: true, config: {}, runtime: { operator_stopped: true } } as StatusPayload;
    expect(isOperatorStopped(status)).toBe(true);
  });
});

describe("blacklistPrimaryLabel / blacklistKey", () => {
  it("prefers label, then channel, then url, then id", () => {
    expect(blacklistPrimaryLabel({ label: "L", channel: "C", url: "U", id: "I" })).toBe("L");
    expect(blacklistPrimaryLabel({ channel: "C", url: "U" })).toBe("C");
    expect(blacklistPrimaryLabel({ url: "U" })).toBe("U");
    expect(blacklistPrimaryLabel({ id: "I" })).toBe("I");
    expect(blacklistPrimaryLabel({}, 2)).toBe("Entry 3");
  });

  it("keys by url then id then label+index", () => {
    expect(blacklistKey({ url: "U" })).toBe("U");
    expect(blacklistKey({ id: "I" })).toBe("I");
    expect(blacklistKey({ label: "L" }, 1)).toBe("L-1");
  });
});
