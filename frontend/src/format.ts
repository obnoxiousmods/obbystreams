import type { Tone } from "./types";

export function fmtBytes(bytes?: number | null) {
  if (!bytes) return "0 MB";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export function fmtAge(seconds?: number | null) {
  if (seconds == null) return "waiting";
  if (seconds < 1) return "now";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}

export function fmtClock(ms?: number | null) {
  if (!ms) return "n/a";
  return new Date(ms).toLocaleTimeString();
}

export function fmtPercent(value?: number | null, digits = 0) {
  if (value == null) return "n/a";
  return `${Number(value).toFixed(digits)}%`;
}

export function fmtMetric(value?: number | null, suffix = "", digits = 0) {
  if (value == null) return "n/a";
  return `${Number(value).toFixed(digits)}${suffix}`;
}

export function absoluteUrl(url?: string) {
  if (!url) return "";
  return new URL(url, window.location.origin).toString();
}

export function toneFromLevel(level?: string): Tone {
  if (level === "ok" || level === "healthy") return "ok";
  if (level === "bad" || level === "failed" || level === "error") return "bad";
  if (level === "warn" || level === "assessing" || level === "degraded" || level === "recovering") return "warn";
  return "neutral";
}

export function encoderLabel(encoder?: string) {
  const labels: Record<string, string> = {
    auto: "Auto",
    "gpu-only": "GPU only",
    cpu: "CPU only",
  };
  return labels[encoder || "auto"] || encoder || "Auto";
}

export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
