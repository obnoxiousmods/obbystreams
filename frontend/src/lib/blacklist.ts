// Pure, framework-free helpers for the source blacklist + operator-stop UI.
// Kept out of App.tsx so they can be unit-tested without a DOM or video.js.

import type { BlacklistEntry, StatusPayload } from "../types";

/** A source-like object that can be blocked (managed source or public tile). */
export interface BlockableSource {
  url?: string;
  id?: string;
  label?: string;
}

/** Build the POST body for /api/blacklist from a source row. */
export function blockPayload(source: BlockableSource, reason = "blocked from cockpit"): {
  url?: string;
  id?: string;
  label?: string;
  reason: string;
} {
  return { url: source.url, id: source.id, label: source.label, reason };
}

/**
 * Whether the cockpit is in a persisted operator Stop. Reads the config flag
 * first, falling back to the runtime mirror, so it is correct even on a payload
 * that only carries one of them.
 */
export function isOperatorStopped(status: StatusPayload | null | undefined): boolean {
  return Boolean(status?.config?.stream?.operator_stopped ?? status?.runtime?.operator_stopped);
}

/** Human-readable primary label for a blacklist entry (url/label/channel/id). */
export function blacklistPrimaryLabel(entry: BlacklistEntry, index = 0): string {
  return entry.label || entry.channel || entry.url || entry.id || `Entry ${index + 1}`;
}

/** Stable React key for a blacklist entry. */
export function blacklistKey(entry: BlacklistEntry, index = 0): string {
  return entry.url || entry.id || `${blacklistPrimaryLabel(entry, index)}-${index}`;
}
