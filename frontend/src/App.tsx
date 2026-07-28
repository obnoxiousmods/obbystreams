import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import videojs from "video.js";
import type Player from "video.js/dist/types/player";
import { api, isUnauthorized } from "./api";
import { absoluteUrl, encoderLabel, errorMessage, fmtAge, fmtBytes, fmtClock, fmtMetric, fmtPercent, toneFromLevel } from "./format";
import { blacklistKey, blacklistPrimaryLabel, blockPayload, isOperatorStopped } from "./lib/blacklist";
import {
  cardProgress,
  formatCardTime,
  formatCountdown,
  phaseLabel,
  phaseTone,
  scheduleOf,
  standbyBannerText,
  standbyMode,
  upcomingLabel,
} from "./lib/schedule";
import type {
  ArangoStatus,
  BlacklistEntry,
  ChildProcess,
  ExternalProcess,
  FeedEvent,
  GpuInfo,
  GpuProcess,
  GpuTelemetryPayload,
  HealthAssessment,
  HlsMetrics,
  LogEntry,
  ManagedProcess,
  PrivateIptvRuntime,
  PublicStreamSource,
  ScheduleSnapshot,
  SourceStatus,
  StatusPayload,
  Tone,
} from "./types";

type EncoderMode = "auto" | "gpu-only" | "cpu";

type DropdownItem<T extends string> = {
  value: T;
  label: string;
  description?: string;
  disabled?: boolean;
  tone?: "default" | "danger";
  onSelect?: () => void | Promise<void>;
};

type PictureInPictureDocument = Document & {
  pictureInPictureElement?: Element | null;
  pictureInPictureEnabled?: boolean;
  exitPictureInPicture?: () => Promise<void>;
};

type PictureInPictureVideo = HTMLVideoElement & {
  requestPictureInPicture?: () => Promise<PictureInPictureWindow>;
};

type PlayerIconName = "play" | "pause" | "volume" | "muted" | "settings" | "pip" | "fullscreen" | "retry";

function PlayerIcon({ name }: { name: PlayerIconName }) {
  const commonProps = {
    className: "playerIcon",
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  switch (name) {
    case "play":
      return (
        <svg {...commonProps}>
          <path d="M8 5v14l11-7-11-7z" fill="currentColor" stroke="none" />
        </svg>
      );
    case "pause":
      return (
        <svg {...commonProps}>
          <path d="M8 5v14" />
          <path d="M16 5v14" />
        </svg>
      );
    case "volume":
      return (
        <svg {...commonProps}>
          <path d="M4 10v4h4l5 4V6l-5 4H4z" />
          <path d="M16 9.5a4 4 0 0 1 0 5" />
          <path d="M18.5 7a7 7 0 0 1 0 10" />
        </svg>
      );
    case "muted":
      return (
        <svg {...commonProps}>
          <path d="M4 10v4h4l5 4V6l-5 4H4z" />
          <path d="M17 9l4 4" />
          <path d="M21 9l-4 4" />
        </svg>
      );
    case "settings":
      return (
        <svg {...commonProps}>
          <path d="M4 7h16" />
          <path d="M4 17h16" />
          <path d="M9 7a2 2 0 1 0 0 .01" />
          <path d="M15 17a2 2 0 1 0 0 .01" />
        </svg>
      );
    case "pip":
      return (
        <svg {...commonProps}>
          <rect x="3" y="5" width="18" height="14" rx="2" />
          <rect x="12" y="11" width="6" height="4" rx="1" />
        </svg>
      );
    case "fullscreen":
      return (
        <svg {...commonProps}>
          <path d="M8 3H5a2 2 0 0 0-2 2v3" />
          <path d="M16 3h3a2 2 0 0 1 2 2v3" />
          <path d="M8 21H5a2 2 0 0 1-2-2v-3" />
          <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
        </svg>
      );
    case "retry":
      return (
        <svg {...commonProps}>
          <path d="M20 12a8 8 0 1 1-2.34-5.66" />
          <path d="M20 4v6h-6" />
        </svg>
      );
    default:
      return null;
  }
}

function firstEnabledIndex<T extends string>(items: DropdownItem<T>[]) {
  return items.findIndex((item) => !item.disabled);
}

function normalizeEncoder(encoder?: string): EncoderMode {
  if (encoder === "gpu-only" || encoder === "cpu") return encoder;
  return "auto";
}

function ModernDropdown<T extends string>({
  label,
  value,
  buttonLabel,
  status,
  items,
  mode = "select",
  disabled = false,
  className = "",
  onSelect,
}: {
  label: string;
  value?: T;
  buttonLabel?: string;
  status?: string;
  items: DropdownItem<T>[];
  mode?: "select" | "menu";
  disabled?: boolean;
  className?: string;
  onSelect?: (value: T) => void | Promise<void>;
}) {
  const dropdownId = useId();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [placement, setPlacement] = useState<"up" | "down">("down");
  const selectedIndex = value == null ? -1 : items.findIndex((item) => item.value === value);
  const selected = selectedIndex >= 0 ? items[selectedIndex] : undefined;
  const displayLabel = buttonLabel || selected?.label || label;
  const panelRole = mode === "select" ? "listbox" : "menu";
  const optionRole = mode === "select" ? "option" : "menuitem";

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!open) return;
    function closeOnOutside(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutside);
    return () => document.removeEventListener("pointerdown", closeOnOutside);
  }, [open]);

  function moveActive(direction: 1 | -1) {
    const enabledIndexes = items.map((item, index) => (item.disabled ? -1 : index)).filter((index) => index >= 0);
    if (!enabledIndexes.length) return;
    const currentPosition = enabledIndexes.indexOf(activeIndex);
    const nextPosition = currentPosition === -1 ? 0 : (currentPosition + direction + enabledIndexes.length) % enabledIndexes.length;
    setActiveIndex(enabledIndexes[nextPosition]);
  }

  function setBoundaryActive(boundary: "first" | "last") {
    const enabledIndexes = items.map((item, index) => (item.disabled ? -1 : index)).filter((index) => index >= 0);
    if (!enabledIndexes.length) return;
    setActiveIndex(boundary === "first" ? enabledIndexes[0] : enabledIndexes[enabledIndexes.length - 1]);
  }

  function openDropdown() {
    const fallbackIndex = firstEnabledIndex(items);
    const rect = rootRef.current?.getBoundingClientRect();
    if (rect) {
      const estimatedPanelHeight = Math.min(320, Math.max(88, items.length * 64 + 12));
      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;
      setPlacement(spaceBelow < estimatedPanelHeight && spaceAbove > spaceBelow ? "up" : "down");
    }
    setActiveIndex(selectedIndex >= 0 && !items[selectedIndex]?.disabled ? selectedIndex : fallbackIndex);
    setOpen(true);
  }

  function choose(item?: DropdownItem<T>) {
    if (!item || item.disabled) return;
    setOpen(false);
    void item.onSelect?.();
    void onSelect?.(item.value);
  }

  function handleKeyDown(event: React.KeyboardEvent) {
    if (disabled) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!open) {
        openDropdown();
      } else {
        moveActive(1);
      }
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        openDropdown();
        setBoundaryActive("last");
      } else {
        moveActive(-1);
      }
    } else if (event.key === "Home") {
      event.preventDefault();
      setOpen(true);
      setBoundaryActive("first");
    } else if (event.key === "End") {
      event.preventDefault();
      setOpen(true);
      setBoundaryActive("last");
    } else if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
    } else if ((event.key === "Enter" || event.key === " ") && open) {
      event.preventDefault();
      choose(items[activeIndex]);
    }
  }

  return (
    <div className={`customDropdown drop-${placement} ${className}`} ref={rootRef} onKeyDown={handleKeyDown}>
      <button
        type="button"
        className="dropdownButton"
        disabled={disabled}
        aria-haspopup={panelRole}
        aria-expanded={open}
        aria-controls={`${dropdownId}-panel`}
        onClick={() => {
          if (open) {
            setOpen(false);
          } else {
            openDropdown();
          }
        }}
      >
        <span>
          <span className="dropdownLabel">{label}</span>
          <strong>{displayLabel}</strong>
          {status ? <span className="dropdownStatus">{status}</span> : null}
        </span>
        <span className="dropdownChevron" aria-hidden="true" />
      </button>
      {open ? (
        <div className="dropdownPanel" id={`${dropdownId}-panel`} role={panelRole}>
          {items.map((item, index) => {
            const itemSelected = mode === "select" && item.value === value;
            return (
              <button
                type="button"
                id={`${dropdownId}-${item.value}`}
                key={item.value}
                role={optionRole}
                aria-selected={mode === "select" ? itemSelected : undefined}
                disabled={item.disabled}
                className={`dropdownOption ${index === activeIndex ? "active" : ""} ${itemSelected ? "selected" : ""} ${item.tone === "danger" ? "danger" : ""}`}
                onMouseEnter={() => {
                  if (!item.disabled) setActiveIndex(index);
                }}
                onClick={() => choose(item)}
              >
                <span>
                  <strong>{item.label}</strong>
                  {item.description ? <small>{item.description}</small> : null}
                </span>
                {itemSelected ? <em>Selected</em> : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return <span className={`badge tone-${tone}`}>{children}</span>;
}

function Panel({
  title,
  meta,
  children,
  className = "",
}: {
  title: string;
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panelHeader">
        <h2>{title}</h2>
        {meta ? <div className="panelMeta">{meta}</div> : null}
      </div>
      {children}
    </section>
  );
}

function MetricTile({ label, value, tone = "neutral" }: { label: string; value: ReactNode; tone?: Tone }) {
  return (
    <div className={`metricTile tone-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function FeedLine({ tone = "info", children }: { tone?: Tone | string; children: ReactNode }) {
  return <div className={`feedLine tone-${tone}`}>{children}</div>;
}

function EmptyLine({ children }: { children: ReactNode }) {
  return <div className="emptyLine">{children}</div>;
}

function LoginScreen({ onLogin }: { onLogin: (password: string) => Promise<void> }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await onLogin(password);
      setPassword("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="loginShell">
      <section className="loginPanel">
        <div className="brandMark" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <p className="kicker">Obbystreams</p>
        <h1>Control</h1>
        <p className="muted">Enter dashboard password.</p>
        <form className="loginForm" onSubmit={submit}>
          <input
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            type="password"
            autoComplete="current-password"
            placeholder="Password"
          />
          <button type="submit" disabled={submitting}>
            {submitting ? "Unlocking" : "Unlock"}
          </button>
        </form>
        <p className="formError">{error}</p>
      </section>
    </main>
  );
}

function StatusStrip({
  status,
  gpu,
  arango,
}: {
  status: StatusPayload | null;
  gpu: GpuTelemetryPayload | null;
  arango: ArangoStatus | null;
}) {
  const proc = status?.managed_process || {};
  const health = status?.health || {};
  const stream = status?.config.stream || {};
  const runText = health.state || (proc.managed ? "running" : "stopped");
  const runTone: Tone = health.level === "bad" ? "bad" : proc.managed ? "ok" : "warn";
  const healthTone = toneFromLevel(health.level || health.state);
  const gpuTone: Tone = gpu?.available ? toneFromLevel(gpu.level || "ok") : gpu ? "bad" : "neutral";
  const arangoTone: Tone = arango?.connected ? "ok" : arango ? "bad" : "neutral";
  const hls = status?.hls || {};
  const rate = typeof hls.encode_rate === "number" ? hls.encode_rate : null;
  const lag = typeof hls.live_lag_seconds === "number" ? hls.live_lag_seconds : null;
  const rateTone: Tone = rate == null ? "neutral" : rate >= 0.98 ? "ok" : rate >= 0.9 ? "warn" : "bad";

  return (
    <section className="statusStrip" aria-label="stream status">
      <div className={`rateBadge rate-${rateTone}`} title="Real-time encode rate: output content-seconds vs wall-clock. 1.00× = keeping up.">
        <span className="rateDot" aria-hidden="true" />
        <span className="rateValue">{rate == null ? "—" : `${rate.toFixed(2)}×`}</span>
        <span className="rateMeta">
          <span className="rateName">encode rate</span>
          <span className="rateLag">{lag == null ? "no output" : `${lag.toFixed(1)}s behind live`}</span>
        </span>
      </div>
      <MetricTile label="Run" value={runText} tone={runTone} />
      <MetricTile label="Health" value={`${health.level || "warn"}: ${health.state || "unknown"}`} tone={healthTone} />
      <MetricTile label="Encoder" value={stream.encoder || "auto"} />
      <MetricTile label="GPU" value={gpu ? (gpu.available ? gpu.level || "online" : "offline") : "checking"} tone={gpuTone} />
      <MetricTile label="ArangoDB" value={arango ? (arango.connected ? "connected" : "offline") : "checking"} tone={arangoTone} />
      <MetricTile label="Updated" value={fmtClock(status?.server_time)} />
    </section>
  );
}

/**
 * Auto-schedule status: which card is next, when each segment starts, and how
 * far through it the cockpit is. Also hosts the "send a test embed" button so
 * the Discord webhook can be verified without waiting for a real event.
 */
export function SchedulePanel({
  schedule,
  pending,
  onTest,
  onComingUp,
}: {
  schedule: ScheduleSnapshot | null | undefined;
  pending: boolean;
  onTest: () => Promise<void>;
  onComingUp: () => Promise<void>;
}) {
  if (!schedule) return null;

  const label = upcomingLabel(schedule);
  const event = schedule.event;
  const countdown = formatCountdown(schedule.countdown_seconds);

  return (
    <Panel
      title="Auto-schedule"
      className="schedulePanel"
      meta={
        <>
          <Badge tone={schedule.enabled ? phaseTone(schedule.phase) : "neutral"}>
            {schedule.enabled ? phaseLabel(schedule.phase) : "Off"}
          </Badge>
          <button type="button" className="secondary compactButton" disabled={pending || !schedule.notify_enabled} onClick={() => void onComingUp()}>
            {pending ? "Sending" : "Post “Coming up”"}
          </button>
          <button type="button" className="secondary compactButton" disabled={pending || !schedule.notify_enabled} onClick={() => void onTest()}>
            {pending ? "Sending" : "Test Discord"}
          </button>
        </>
      }
    >
      {!schedule.enabled && <p className="muted">Auto-schedule is off — Stop keeps the stream down until you press Start.</p>}

      {schedule.enabled && !label && <p className="muted">No upcoming UFC card on the ESPN calendar yet.</p>}

      {schedule.enabled && label && (
        <div className="scheduleBody">
          <p className="scheduleEventName">{label}</p>
          <p className="scheduleCountdown">
            <strong>{countdown}</strong>
            <span className="muted">{schedule.countdown_is_estimate ? " until scheduled start" : " until first card"}</span>
          </p>
          {event?.venue && (
            <p className="muted monoLine">
              {event.venue}
              {event.city ? ` · ${event.city}` : ""}
            </p>
          )}
          {event?.cards?.length ? (
            <ul className="scheduleCards">
              {event.cards.map((card) => (
                <li key={card.start} className={card.all_final ? "cardDone" : undefined}>
                  <span className="cardLabel">{card.label}</span>
                  <span className="cardTime">{formatCardTime(card.start)}</span>
                  <span className="cardBouts muted">{cardProgress(card.completed, card.bouts)}</span>
                </li>
              ))}
            </ul>
          ) : null}
          {event?.winner && (
            <p className="scheduleResult">
              🏆 {event.winner} — main event
            </p>
          )}
          <p className="muted scheduleReason">
            {schedule.reason}
            {schedule.notifications_sent ? ` · ${schedule.notifications_sent} Discord notice(s) sent` : ""}
            {schedule.notify_enabled ? "" : " · Discord webhook not configured"}
          </p>
        </div>
      )}
    </Panel>
  );
}

export function CommandHeader({
  status,
  pendingAction,
  onStreamAction,
  onToggleSchedule,
}: {
  status: StatusPayload | null;
  pendingAction: string;
  onStreamAction: (action: "start" | "restart" | "stop") => Promise<void>;
  onToggleSchedule?: (enabled: boolean) => Promise<void>;
}) {
  const proc = status?.managed_process || {};
  const externalProcessCount = status?.existing_processes?.length || 0;
  const hls = status?.hls || {};
  const busy = Boolean(pendingAction);
  const canStop = Boolean(proc.managed) || externalProcessCount > 0;
  const hlsUrl = hls.public_hls_url || hls.dashboard_hls_url || "Waiting for HLS output";
  // Persisted operator Stop: reflected from either the config flag or the runtime mirror.
  const operatorStopped = isOperatorStopped(status);
  const schedule = scheduleOf(status);
  // With auto-schedule on, Stop parks the cockpit rather than killing it, so the
  // banner has to say "standby" instead of claiming the stream is down for good.
  const mode = standbyMode(status, operatorStopped);

  return (
    <header className="commandHeader">
      <div>
        <p className="kicker">Obbystreams</p>
        <h1>Stream Control Center</h1>
        <p className="monoLine">{hlsUrl}</p>
        {mode === "standby" && (
          <p className="standbyBanner" role="status">
            {standbyBannerText(schedule)}
          </p>
        )}
        {mode === "stopped" && (
          <p className="stoppedBanner" role="status">
            ⏹ STOPPED (manual) — auto-recovery &amp; scrapers paused. Press Start to resume.
          </p>
        )}
      </div>
      <div className="commandActions">
        {onToggleSchedule && (
          <label className="scheduleToggle" title="Auto-start for UFC cards and stand down when they end">
            <input
              type="checkbox"
              checked={Boolean(schedule?.enabled)}
              disabled={busy}
              onChange={(evt) => void onToggleSchedule(evt.target.checked)}
            />
            <span>Auto-schedule</span>
          </label>
        )}
        <button
          type="button"
          className={operatorStopped ? "resumeButton" : undefined}
          disabled={busy || Boolean(proc.managed)}
          onClick={() => onStreamAction("start")}
        >
          {pendingAction === "start" ? "Starting" : operatorStopped ? "Resume" : "Start"}
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={() => onStreamAction("restart")}>
          {pendingAction === "restart" ? "Restarting" : "Restart"}
        </button>
        <button type="button" className="danger" disabled={busy || !canStop} onClick={() => onStreamAction("stop")}>
          {pendingAction === "stop" ? "Stopping" : "Stop"}
        </button>
      </div>
    </header>
  );
}

function LivePlayer({
  proc,
  hls,
  health,
  overrideUrl,
  onWatchSource,
  onClearSource,
  sourceBusy,
  sourceMsg,
}: {
  proc?: ManagedProcess;
  hls?: HlsMetrics;
  health?: HealthAssessment;
  overrideUrl?: string | null;
  onWatchSource: (url: string) => Promise<void>;
  onClearSource: () => void;
  sourceBusy: boolean;
  sourceMsg: string | null;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const videoSurfaceRef = useRef<HTMLDivElement | null>(null);
  const playerRef = useRef<Player | null>(null);
  const retryTimerRef = useRef<number | null>(null);
  const retryMsRef = useRef(800);
  const [playerState, setPlayerState] = useState("player idle");
  const [isPaused, setIsPaused] = useState(true);
  const [isMuted, setIsMuted] = useState(true);
  const [volume, setVolume] = useState(0);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [atLiveEdge, setAtLiveEdge] = useState(true);
  const [pictureInPicture, setPictureInPicture] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [volumePanelOpen, setVolumePanelOpen] = useState(false);
  const [scUrl, setScUrl] = useState("");
  const managedUrl = proc?.managed && hls?.playlist_ready ? hls.dashboard_hls_url || hls.public_hls_url || "" : "";
  const playerUrl = overrideUrl || managedUrl;
  const displayUrl = hls?.public_hls_url || hls?.dashboard_hls_url || "";
  const managedDot = proc?.managed && hls?.playlist_ready ? "green" : proc?.managed ? "yellow" : "red";
  const overrideDot = overrideUrl ? "green" : "yellow";

  const clearRetry = useCallback(() => {
    if (retryTimerRef.current == null) return;
    window.clearTimeout(retryTimerRef.current);
    retryTimerRef.current = null;
  }, []);

  const requestPlay = useCallback(
    (reason: string) => {
      const player = playerRef.current;
      if (!player || player.isDisposed() || !playerUrl) return;
      player.muted(true);
      player.volume(0);
      Promise.resolve(player.play())
        .then(() => {
          retryMsRef.current = 800;
          clearRetry();
          setIsPaused(false);
          setIsMuted(Boolean(player.muted()));
          setVolume(player.volume() ?? 0);
          setPlayerState("playing");
        })
        .catch(() => {
          setPlayerState(`${reason}: retrying`);
          clearRetry();
          const delay = Math.max(400, Math.min(5000, retryMsRef.current));
          retryTimerRef.current = window.setTimeout(() => {
            retryTimerRef.current = null;
            requestPlay(reason);
          }, delay);
          retryMsRef.current = Math.min(5000, Math.floor(retryMsRef.current * 1.5));
        });
    },
    [clearRetry, playerUrl],
  );

  const syncPlayerState = useCallback((player: Player) => {
    const liveTracker = (player as unknown as {
      liveTracker?: {
        atLiveEdge?: () => boolean;
        isLive?: () => boolean;
      };
    }).liveTracker;
    setIsPaused(Boolean(player.paused()));
    setIsMuted(Boolean(player.muted()));
    setVolume(player.volume() ?? 0);
    setIsFullscreen(Boolean(player.isFullscreen()));
    if (liveTracker?.isLive?.()) {
      setAtLiveEdge(Boolean(liveTracker.atLiveEdge?.()));
    } else {
      setAtLiveEdge(true);
    }
  }, []);

  const loadSource = useCallback(
    (reason: string) => {
      const player = playerRef.current;
      if (!player || player.isDisposed() || !playerUrl) return;
      clearRetry();
      retryMsRef.current = 800;
      setPlayerState("loading");
      player.muted(true);
      player.volume(0);
      player.pause();
      player.reset();
      player.src({ src: playerUrl, type: "application/x-mpegURL" });
      player.load();
      syncPlayerState(player);
      requestPlay(reason);
    },
    [clearRetry, playerUrl, requestPlay, syncPlayerState],
  );

  useEffect(() => {
    if (!videoRef.current) return;
    const existing = playerRef.current;

    if (!playerUrl) {
      clearRetry();
      if (existing && !existing.isDisposed()) {
        existing.pause();
        existing.reset();
        syncPlayerState(existing);
      }
      setIsPaused(true);
      setPlayerState(health?.message || "waiting for stream");
      return;
    }

    const player =
      existing && !existing.isDisposed()
        ? existing
        : videojs(videoRef.current, {
          fluid: true,
          liveui: true,
          controls: false,
          autoplay: true,
          muted: true,
            preload: "auto",
            playsinline: true,
            html5: {
              vhs: {
                overrideNative: true,
                limitRenditionByPlayerDimensions: true,
              },
              nativeAudioTracks: false,
              nativeVideoTracks: false,
            },
          });

    playerRef.current = player;
    player.off("loadedmetadata");
    player.off("playing");
    player.off("pause");
    player.off("waiting");
    player.off("stalled");
    player.off("ended");
    player.off("error");
    player.on("loadedmetadata", () => requestPlay("metadata"));
    player.on("playing", () => {
      retryMsRef.current = 800;
      clearRetry();
      syncPlayerState(player);
      setPlayerState("playing");
    });
    player.on("play", () => syncPlayerState(player));
    player.on("pause", () => {
      syncPlayerState(player);
      requestPlay("paused");
    });
    player.on("volumechange", () => syncPlayerState(player));
    player.on("fullscreenchange", () => syncPlayerState(player));
    player.on("timeupdate", () => syncPlayerState(player));
    player.on("waiting", () => requestPlay("buffering"));
    player.on("stalled", () => requestPlay("stalled"));
    player.on("ended", () => requestPlay("ended"));
    player.on("error", () => {
      const err = player.error();
      setPlayerState(`error ${err?.code || ""} retrying`.trim());
      requestPlay("error");
    });

    player.ready(() => {
      loadSource("load");
    });
  }, [clearRetry, health?.message, loadSource, playerUrl, requestPlay, syncPlayerState]);

  useEffect(() => {
    return () => {
      clearRetry();
      if (playerRef.current && !playerRef.current.isDisposed()) playerRef.current.dispose();
      playerRef.current = null;
    };
  }, [clearRetry]);

  useEffect(() => {
    function closeOnOutside(event: PointerEvent) {
      const target = event.target as Node | null;
      if (!target) return;
      if (videoSurfaceRef.current?.contains(target)) return;
      setMoreOpen(false);
      setVolumePanelOpen(false);
    }

    document.addEventListener("pointerdown", closeOnOutside);
    return () => document.removeEventListener("pointerdown", closeOnOutside);
  }, []);

  useEffect(() => {
    const video = videoRef.current as PictureInPictureVideo | null;
    if (!video) return;

    const pipOn = () => setPictureInPicture(true);
    const pipOff = () => setPictureInPicture(false);
    const syncFullscreen = () => {
      const fullscreenElement = document.fullscreenElement;
      setIsFullscreen(Boolean(fullscreenElement && videoSurfaceRef.current?.contains(fullscreenElement)));
    };

    video.addEventListener("enterpictureinpicture", pipOn);
    video.addEventListener("leavepictureinpicture", pipOff);
    document.addEventListener("fullscreenchange", syncFullscreen);
    return () => {
      video.removeEventListener("enterpictureinpicture", pipOn);
      video.removeEventListener("leavepictureinpicture", pipOff);
      document.removeEventListener("fullscreenchange", syncFullscreen);
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const player = playerRef.current;
      if (!player || player.isDisposed() || !playerUrl) return;
      player.muted(true);
      player.volume(0);
      if (player.paused()) requestPlay("heartbeat");
    }, 3000);
    return () => window.clearInterval(timer);
  }, [playerUrl, requestPlay]);

  async function copyHls() {
    const url = absoluteUrl(playerUrl || displayUrl);
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setPlayerState("HLS URL copied");
    } catch {
      window.prompt("Copy HLS URL:", url);
    }
  }

  function togglePlayback() {
    const player = playerRef.current;
    if (!player || player.isDisposed()) return;
    if (player.paused()) {
      requestPlay("manual");
    } else {
      clearRetry();
      player.pause();
      syncPlayerState(player);
      setPlayerState("paused");
    }
  }

  function toggleMute() {
    const player = playerRef.current;
    if (!player || player.isDisposed()) return;
    const nextMuted = !player.muted();
    player.muted(nextMuted);
    if (!nextMuted && player.volume() === 0) player.volume(0.55);
    syncPlayerState(player);
  }

  function updateVolume(nextVolume: number) {
    const player = playerRef.current;
    if (!player || player.isDisposed()) return;
    const normalized = Math.max(0, Math.min(1, nextVolume));
    player.volume(normalized);
    player.muted(normalized === 0);
    syncPlayerState(player);
  }

  async function togglePictureInPicture() {
    const pipDocument = document as PictureInPictureDocument;
    const video = videoRef.current as PictureInPictureVideo | null;
    if (!video || !pipDocument.pictureInPictureEnabled || !video.requestPictureInPicture) return;
    if (pipDocument.pictureInPictureElement && pipDocument.exitPictureInPicture) {
      await pipDocument.exitPictureInPicture();
      return;
    }
    await video.requestPictureInPicture();
  }

  async function toggleFullscreen() {
    const surface = videoSurfaceRef.current;
    if (!surface) return;
    if (document.fullscreenElement) {
      await document.exitFullscreen();
      return;
    }
    await surface.requestFullscreen();
  }

  function goLive() {
    const player = playerRef.current;
    if (!player || player.isDisposed()) return;
    const liveTracker = (player as unknown as {
      liveTracker?: {
        seekToLiveEdge?: () => void;
      };
    }).liveTracker;
    if (liveTracker?.seekToLiveEdge) {
      liveTracker.seekToLiveEdge();
    } else {
      const seekable = player.seekable();
      if (seekable.length) player.currentTime(seekable.end(seekable.length - 1));
    }
    syncPlayerState(player);
    requestPlay("live");
  }

  const canUsePlayer = Boolean(playerUrl);
  const hlsActionUrl = absoluteUrl(playerUrl || displayUrl);
  const statusTone: Tone = playerState === "playing" ? "ok" : playerState.includes("error") ? "bad" : "neutral";

  return (
    <Panel title="Live Player" meta={<Badge tone={statusTone}>{playerState}</Badge>} className="stagePanel">
      <div className="videoSurface" ref={videoSurfaceRef}>
        <video ref={videoRef} className="video-js vjs-big-play-centered vjs-obby" preload="auto" playsInline />
        <div className="customPlayerChrome">
          <div className="playerTopRail">
            <span className="playerSignalLine">
              <Badge tone={proc?.managed && hls?.playlist_ready ? "ok" : "warn"}>{overrideUrl ? "OVERRIDE" : "LIVE"}</Badge>
              <span>{playerState}</span>
            </span>
            <span className="playerMetaLine">
              {overrideUrl ? "Custom source preview" : "Managed stream preview"}
            </span>
          </div>
          <div className="playerBottomRail">
            <div className="playerControlRow">
              <button
                type="button"
                className="playerIconButton primary"
                disabled={!canUsePlayer}
                onClick={togglePlayback}
                aria-label={isPaused ? "Play" : "Pause"}
                title={isPaused ? "Play" : "Pause"}
              >
                <PlayerIcon name={isPaused ? "play" : "pause"} />
              </button>
              <div className={`volumeCluster ${volumePanelOpen ? "open" : ""}`}>
                <button
                  type="button"
                  className={`playerIconButton ${volumePanelOpen ? "active" : ""}`}
                  disabled={!canUsePlayer}
                  onClick={() => {
                    setMoreOpen(false);
                    setVolumePanelOpen((open) => !open);
                  }}
                  aria-label="Volume"
                  title="Volume"
                >
                  <PlayerIcon name={isMuted || volume === 0 ? "muted" : "volume"} />
                </button>
                {volumePanelOpen ? (
                  <div className="volumePopover">
                    <button type="button" className="volumeMuteButton" onClick={toggleMute}>
                      <PlayerIcon name={isMuted || volume === 0 ? "muted" : "volume"} />
                      <span>{isMuted || volume === 0 ? "Unmute" : "Mute"}</span>
                    </button>
                    <div className="volumeSliderRow">
                      <input
                        type="range"
                        min="0"
                        max="1"
                        step="0.01"
                        value={volume}
                        disabled={!canUsePlayer}
                        onChange={(event) => updateVolume(Number(event.target.value))}
                      />
                      <span>{Math.round(volume * 100)}%</span>
                    </div>
                  </div>
                ) : null}
              </div>
              <button type="button" className="liveChip" disabled={!canUsePlayer || atLiveEdge} onClick={goLive}>
                <span aria-hidden="true" />
                LIVE
              </button>
              <span className="playerReadout">
                {overrideUrl ? "Custom source active" : "Dashboard stream"} · {atLiveEdge ? "at live edge" : "behind live"}
              </span>
              <div className="sourceDots" aria-label="Source status">
                <span className={`sourceDot sourceDot-${managedDot}${!overrideUrl ? " active" : ""}`} title="Managed stream" />
                <span className={`sourceDot sourceDot-${overrideDot}${overrideUrl ? " active" : ""}`} title="Override source" />
              </div>
            </div>
            <div className="playerActionRow">
              <button
                type="button"
                className="playerIconButton"
                disabled={!canUsePlayer}
                onClick={() => loadSource("manual")}
                aria-label="Reload"
                title="Reload"
              >
                <PlayerIcon name="retry" />
              </button>
              <button
                type="button"
                className={`playerIconButton ${moreOpen ? "active" : ""}`}
                onClick={() => {
                  setVolumePanelOpen(false);
                  setMoreOpen((open) => !open);
                }}
                aria-label="More"
                title="More"
              >
                <PlayerIcon name="settings" />
              </button>
              <button
                type="button"
                className="playerIconButton"
                disabled={!canUsePlayer}
                onClick={() => void togglePictureInPicture()}
                aria-label={pictureInPicture ? "Close picture in picture" : "Picture in picture"}
                title={pictureInPicture ? "Close picture in picture" : "Picture in picture"}
              >
                <PlayerIcon name="pip" />
              </button>
              <button
                type="button"
                className="playerIconButton"
                disabled={!canUsePlayer}
                onClick={() => void toggleFullscreen()}
                aria-label={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
                title={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
              >
                <PlayerIcon name="fullscreen" />
              </button>
            </div>
          </div>
          {moreOpen ? (
            <div className="playerMoreMenu" role="menu">
              <div className="playerMoreGrid">
                <button type="button" className="playerMenuButton" disabled={!canUsePlayer} onClick={() => loadSource("manual")}>
                  <strong>Reload stream</strong>
                  <span>Refresh the preview source.</span>
                </button>
                <button type="button" className="playerMenuButton" disabled={!canUsePlayer || atLiveEdge} onClick={goLive}>
                  <strong>Go live</strong>
                  <span>Jump to the newest buffered segment.</span>
                </button>
                <button type="button" className="playerMenuButton" disabled={!hlsActionUrl} onClick={() => void copyHls()}>
                  <strong>Copy HLS URL</strong>
                  <span>Copy the active playback URL.</span>
                </button>
                <button
                  type="button"
                  className="playerMenuButton"
                  disabled={!hlsActionUrl}
                  onClick={() => {
                    if (hlsActionUrl) window.open(hlsActionUrl, "_blank", "noopener,noreferrer");
                  }}
                >
                  <strong>Open HLS URL</strong>
                  <span>Open the stream in a new tab.</span>
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </div>
      <div className="playerToolbar">
        <span className="inlineMetric">
          Window <strong>{hls?.segment_window_seconds ? `${hls.segment_window_seconds.toFixed(1)}s` : "n/a"}</strong>
        </span>
        <span className="inlineMetric">
          Playlist <strong>{fmtClock(hls?.playlist_modified_at)}</strong>
        </span>
      </div>
      <div className="sourceChanger">
        <form
          className="addLink"
          onSubmit={async (e) => {
            e.preventDefault();
            const url = scUrl.trim();
            if (!url) return;
            await onWatchSource(url);
          }}
        >
          <input
            value={scUrl}
            onChange={(e) => setScUrl(e.target.value)}
            type="url"
            placeholder="Paste SportSurge or stream URL to watch here"
            disabled={sourceBusy}
          />
          <button type="submit" className="streamNow" disabled={sourceBusy || !scUrl.trim()}>
            {sourceBusy ? "Loading…" : "Watch Source"}
          </button>
          {overrideUrl ? (
            <button
              type="button"
              className="danger compactButton"
              onClick={() => { onClearSource(); setScUrl(""); }}
            >
              Stop
            </button>
          ) : null}
        </form>
        {sourceMsg ? <p className="scrapeResult">{sourceMsg}</p> : null}
      </div>
    </Panel>
  );
}

function HealthPanel({ health, hls, proc }: { health?: HealthAssessment; hls?: HlsMetrics; proc?: ManagedProcess }) {
  const evidence = health?.evidence || {};
  const reasons = evidence.reasons || [];
  const remaining = health?.assessment_remaining || 0;
  const elapsed = health?.assessment_elapsed || 0;
  const evidenceBits = [
    evidence.progress_seen ? "progress" : null,
    evidence.playlist_fresh ? "fresh playlist" : null,
    evidence.media_sequence_advanced ? "sequence moved" : null,
    evidence.bytes_delta ? `+${fmtBytes(evidence.bytes_delta)}` : null,
    evidence.segment_delta ? `+${evidence.segment_delta} segment` : null,
    reasons[0] || null,
  ].filter(Boolean);
  const samples = health?.samples || [];

  return (
    <Panel title="Health" meta={<Badge tone={toneFromLevel(health?.level)}>{health?.decision || "checking"}</Badge>}>
      <p className="panelMessage">{health?.message || "Waiting for status."}</p>
      <div className="metricGrid two">
        <MetricTile label="Segments" value={String(hls?.segments ?? 0)} />
        <MetricTile label="Playlist age" value={fmtAge(hls?.playlist_age)} tone={evidence.playlist_fresh ? "ok" : "warn"} />
        <MetricTile label="HLS size" value={fmtBytes(hls?.bytes)} />
        <MetricTile label="RSS" value={proc?.rss ? fmtBytes(proc.rss) : "n/a"} />
        <MetricTile label="Score" value={health?.score == null ? "n/a" : health.score.toFixed(1)} tone={toneFromLevel(health?.level)} />
        <MetricTile label="Confidence" value={health?.confidence == null ? "n/a" : `${health.confidence}%`} />
        <MetricTile label="Decision" value={health?.decision || "n/a"} />
        <MetricTile label="Assessment" value={remaining > 0 ? `${elapsed.toFixed(1)}s + ${remaining.toFixed(1)}s` : `${elapsed.toFixed(1)}s`} />
      </div>
      <div className="chipRow">
        {evidenceBits.length ? evidenceBits.map((bit) => <Badge key={bit} tone="info">{bit}</Badge>) : <span className="muted">Collecting evidence.</span>}
      </div>
      <div className="sampleRail" aria-label="health score samples">
        {samples.length ? (
          samples.map((sample, index) => {
            const normalized = Math.max(8, Math.min(100, Math.abs(sample.score || 0) / 4));
            const sampleTone = sample.decision === "healthy" ? "ok" : sample.decision === "failed" ? "bad" : "warn";
            return <span key={`${sample.ts || index}-${index}`} className={`sampleBar tone-${sampleTone}`} style={{ height: `${normalized}%` }} title={`${sample.decision || "sample"} ${sample.score ?? ""}`} />;
          })
        ) : (
          <span className="muted">No score samples yet.</span>
        )}
      </div>
    </Panel>
  );
}

function EncoderControl({
  encoder,
  pending,
  onSetEncoder,
}: {
  encoder?: string;
  pending: boolean;
  onSetEncoder: (encoder: EncoderMode) => Promise<void>;
}) {
  const mode = normalizeEncoder(encoder);
  const encoderItems: DropdownItem<EncoderMode>[] = [
    {
      value: "auto",
      label: "Auto",
      description: "Use the best available encoder.",
    },
    {
      value: "gpu-only",
      label: "GPU only",
      description: "Require NVENC acceleration.",
    },
    {
      value: "cpu",
      label: "CPU only",
      description: "Run without GPU encoding.",
    },
  ];
  return (
    <div className="encoderControl">
      <div>
        <span>Encoder mode</span>
        <strong>{pending ? "Updating..." : encoderLabel(mode)}</strong>
      </div>
      <ModernDropdown label="Mode" value={mode} status={pending ? "Saving" : undefined} items={encoderItems} disabled={pending} className="encoderDropdown" onSelect={onSetEncoder} />
    </div>
  );
}

function ProcessPanel({
  proc,
  external,
  encoder,
  errorCount,
  pendingEncoder,
  onSetEncoder,
}: {
  proc?: ManagedProcess;
  external: ExternalProcess[];
  encoder?: string;
  errorCount: number;
  pendingEncoder: boolean;
  onSetEncoder: (encoder: EncoderMode) => Promise<void>;
}) {
  return (
    <Panel title="Process" meta={<Badge tone={proc?.managed ? "ok" : "warn"}>{proc?.managed ? "runtime" : "stopped"}</Badge>}>
      <EncoderControl encoder={encoder} pending={pendingEncoder} onSetEncoder={onSetEncoder} />
      <div className="metricGrid two">
        <MetricTile label="PID" value={proc?.pid || "n/a"} />
        <MetricTile label="CPU" value={proc?.cpu == null ? "n/a" : `${proc.cpu.toFixed(1)}%`} />
        <MetricTile label="External procs" value={String(external.length)} tone={external.length ? "warn" : "neutral"} />
        <MetricTile label="Errors" value={String(errorCount)} tone={errorCount ? "bad" : "neutral"} />
      </div>
      <ProcessList title="Child processes" processes={proc?.children || []} />
      <ExternalProcessList processes={external} />
    </Panel>
  );
}

function ProcessList({ title, processes }: { title: string; processes: ChildProcess[] }) {
  return (
    <div className="subsection">
      <h3>{title}</h3>
      <div className="feedBox">
        {processes.length ? (
          processes.map((proc) => (
            <FeedLine key={proc.pid || proc.name} tone="info">
              pid {proc.pid || "?"} {proc.name || "process"} | rss {fmtBytes(proc.rss)} | cpu {(proc.cpu ?? 0).toFixed(1)}%
            </FeedLine>
          ))
        ) : (
          <EmptyLine>No child process.</EmptyLine>
        )}
      </div>
    </div>
  );
}

function ExternalProcessList({ processes }: { processes: ExternalProcess[] }) {
  return (
    <div className="subsection">
      <h3>Other stream processes</h3>
      <div className="feedBox">
        {processes.length ? (
          processes.map((proc) => (
            <FeedLine key={proc.pid || proc.cmd} tone="warn">
              pid {proc.pid || "?"} | age {fmtAge(proc.age)} | {proc.cmd || "unknown command"}
            </FeedLine>
          ))
        ) : (
          <EmptyLine>No other stream process detected.</EmptyLine>
        )}
      </div>
    </div>
  );
}

function gpuTone(gpu: GpuInfo): Tone {
  if ((gpu.temperature_c || 0) >= 88) return "bad";
  if ((gpu.memory_used_pct || 0) >= 92) return "warn";
  return "info";
}

function GpuPanel({ gpu }: { gpu: GpuTelemetryPayload | null }) {
  const summary = gpu?.summary || {};
  const gpus = gpu?.gpus || [];
  const processes = gpu?.processes || [];
  const primary = gpus[0] || {};
  const diagnostics = [
    ...(gpu?.diagnosis || []),
    ...(gpu?.errors || []),
    ...Object.entries(gpu?.commands || {})
      .filter(([, result]) => result.returncode && result.returncode !== 0)
      .map(([name, result]) => `${name}: ${result.stderr || result.stdout || `exit ${result.returncode}`}`),
  ];
  const memory =
    primary.memory_used_mb != null && primary.memory_total_mb != null
      ? `${fmtBytes(primary.memory_used_mb * 1024 * 1024)} / ${fmtBytes(primary.memory_total_mb * 1024 * 1024)} (${fmtPercent(primary.memory_used_pct)})`
      : fmtPercent(summary.max_memory_used_pct);
  const power =
    summary.power_draw_w != null && summary.power_limit_w != null
      ? `${fmtMetric(summary.power_draw_w, "W", 1)} / ${fmtMetric(summary.power_limit_w, "W", 1)}`
      : fmtMetric(summary.power_draw_w, "W", 1);
  const encoderBits = [
    summary.encoder_session_count != null ? `${summary.encoder_session_count} sessions` : null,
    summary.encoder_utilization_pct != null ? `${summary.encoder_utilization_pct}% enc` : null,
  ].filter(Boolean);

  return (
    <Panel title="NVIDIA SMI" meta={<Badge tone={gpu?.available ? toneFromLevel(gpu.level) : gpu ? "bad" : "neutral"}>{gpu?.checked_at ? `${fmtClock(gpu.checked_at)} | 5s` : "5s"}</Badge>} className="gpuPanel">
      <p className="panelMessage">{gpu?.message || "Waiting for GPU telemetry."}</p>
      <div className="metricGrid two">
        <MetricTile label="Driver" value={summary.driver_version || "n/a"} />
        <MetricTile label="Utilization" value={fmtPercent(summary.max_gpu_utilization_pct)} />
        <MetricTile label="Memory" value={memory} />
        <MetricTile label="Temperature" value={fmtMetric(summary.max_temperature_c, "C")} tone={(summary.max_temperature_c || 0) >= 88 ? "bad" : "neutral"} />
        <MetricTile label="Power" value={power} />
        <MetricTile label="Encoder" value={encoderBits.length ? encoderBits.join(" | ") : "n/a"} />
        <MetricTile label="GPU processes" value={String(summary.process_count ?? processes.length)} />
        <MetricTile label="FFmpeg" value={summary.stream_gpu_active ? "visible" : "not visible"} tone={summary.stream_gpu_active ? "ok" : "neutral"} />
      </div>
      <div className="subsection">
        <h3>GPUs</h3>
        <div className="feedBox">
          {gpus.length ? (
            gpus.map((item) => {
              const gpuMemory =
                item.memory_used_mb != null && item.memory_total_mb != null
                  ? `${fmtBytes(item.memory_used_mb * 1024 * 1024)} / ${fmtBytes(item.memory_total_mb * 1024 * 1024)}`
                  : "memory n/a";
              return (
                <FeedLine key={item.uuid || item.index || item.name} tone={gpuTone(item)}>
                  GPU {item.index ?? "?"} {item.name || "unknown"} | util {fmtPercent(item.gpu_utilization_pct)} | mem {gpuMemory} | temp {fmtMetric(item.temperature_c, "C")} | power {fmtMetric(item.power_draw_w, "W", 1)} | {item.pstate || "pstate n/a"}
                </FeedLine>
              );
            })
          ) : (
            <EmptyLine>No GPU rows parsed from nvidia-smi.</EmptyLine>
          )}
        </div>
      </div>
      <div className="subsection">
        <h3>GPU processes</h3>
        <div className="feedBox">
          {processes.length ? (
            processes.map((proc) => <GpuProcessLine key={`${proc.gpu_index ?? "gpu"}-${proc.pid}`} proc={proc} />)
          ) : (
            <EmptyLine>No GPU processes visible.</EmptyLine>
          )}
        </div>
      </div>
      <div className="subsection">
        <h3>Diagnostics</h3>
        <div className="feedBox logBox">
          {diagnostics.length ? diagnostics.slice(0, 12).map((line, index) => <FeedLine key={`${line}-${index}`}>{line}</FeedLine>) : <EmptyLine>No GPU diagnostics.</EmptyLine>}
        </div>
      </div>
    </Panel>
  );
}

function GpuProcessLine({ proc }: { proc: GpuProcess }) {
  const memory = proc.used_memory_mb == null ? "memory n/a" : `${proc.used_memory_mb} MB`;
  const enc = proc.enc_pct == null ? "enc n/a" : `enc ${proc.enc_pct}%`;
  const dec = proc.dec_pct == null ? "dec n/a" : `dec ${proc.dec_pct}%`;
  return (
    <FeedLine tone={proc.is_ffmpeg ? "ok" : "info"}>
      GPU {proc.gpu_index ?? "?"} pid {proc.pid || "?"} {proc.process_name || "unknown"} | {memory} | sm {fmtPercent(proc.sm_pct)} | mem {fmtPercent(proc.mem_pct)} | {enc} | {dec}
    </FeedLine>
  );
}

export function SourcesPanel({
  sources,
  pending,
  onAdd,
  onRemove,
  onActivate,
  onLock,
  onRecover,
  onBlock,
}: {
  sources: SourceStatus[];
  pending: boolean;
  onAdd: (url: string) => Promise<void>;
  onRemove: (url: string) => Promise<void>;
  onActivate: (source: SourceStatus) => Promise<void>;
  onLock: (source: SourceStatus, locked: boolean) => Promise<void>;
  onRecover: (source: SourceStatus) => Promise<void>;
  onBlock: (source: SourceStatus) => Promise<void>;
}) {
  const [newLink, setNewLink] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const url = newLink.trim();
    if (!url) return;
    await onAdd(url);
    setNewLink("");
  }

  return (
    <Panel
      title="Official Source"
      meta={<Badge>{sources.length} configured</Badge>}
      className="linksPanel"
    >
      <form className="addLink" onSubmit={submit}>
        <input value={newLink} onChange={(event) => setNewLink(event.target.value)} type="url" placeholder="https://example.com/live.m3u8" />
        <button type="submit" disabled={pending}>
          Add
        </button>
      </form>
      <div className="linksList">
        {sources.length ? (
          sources.map((source, index) => {
            const url = source.url || "";
            const recoverable = source.type === "soursignal" || url.includes("soursignal.com");
            return (
            <div className="linkItem sourceItem" key={source.id || `${url}-${index}`}>
              <div className="linkTop">
                <strong>{source.label || `Source ${index + 1}`}</strong>
                <div className="sourceBadges">
                  <Badge tone={source.preferred ? "ok" : "neutral"}>{source.preferred ? "Preferred" : "Fallback"}</Badge>
                  {source.locked && <Badge tone="ok">Locked</Badge>}
                  <Badge tone={source.health === "red" ? "bad" : source.health === "yellow" ? "warn" : source.health === "green" ? "ok" : "neutral"}>{source.health || "unknown"}</Badge>
                  <Badge>{source.type || "hls"}</Badge>
                  <Badge>{source.viewer_count || 0} watching</Badge>
                </div>
              </div>
              <p>{url}</p>
              {source.health_message && <p className="sourceMeta">{source.health_message}</p>}
              <div className="linkActions">
                <a className="buttonLink compactButton" href={url} target="_blank" rel="noreferrer">
                  Open
                </a>
                <button type="button" className="compactButton streamNow" disabled={pending || source.preferred} onClick={() => onActivate(source)}>
                  {source.preferred ? "Active" : "Switch"}
                </button>
                <button type="button" className="secondary compactButton" disabled={pending} onClick={() => onLock(source, !source.locked)}>
                  {source.locked ? "Unlock" : "Lock"}
                </button>
                {recoverable && (
                  <button type="button" className="secondary compactButton" disabled={pending} onClick={() => onRecover(source)}>
                    Recover
                  </button>
                )}
                <button type="button" className="danger compactButton" disabled={pending} onClick={() => onBlock(source)} title="Blacklist this source so it never reappears">
                  Block
                </button>
                <button type="button" className="danger compactButton" disabled={pending} onClick={() => onRemove(url)}>
                  Remove
                </button>
              </div>
            </div>
            );
          })
        ) : (
          <EmptyLine>No stream sources configured.</EmptyLine>
        )}
      </div>
    </Panel>
  );
}

function PrivateIptvPanel({
  runtime,
  pending,
  onRefresh,
  onControl,
}: {
  runtime?: PrivateIptvRuntime;
  pending: boolean;
  onRefresh: () => Promise<void>;
  onControl: (action: "pause" | "resume" | "stop" | "restart") => Promise<void>;
}) {
  const state = runtime?.state || "idle";
  const tone: Tone = state === "active" ? "ok" : state === "error" ? "bad" : state === "inactive" ? "warn" : "neutral";
  const accepted = runtime?.accepted_count || 0;
  const candidates = runtime?.candidate_count || 0;
  const entries = runtime?.playlist_entries || 0;
  return (
    <Panel title="Private IPTV Automation" meta={<Badge tone={tone}>{state}</Badge>} className="linksPanel privateIptvPanel">
      <div className="automationSummary">
        <span>{runtime?.enabled ? "Enabled" : "Disabled"}</span>
        <span>{accepted}/{candidates} accepted</span>
        <span>{entries} entries</span>
        <span>Checked {fmtClock(runtime?.last_checked_at)}</span>
      </div>
      <p className="panelMessage">{runtime?.message || "Waiting for private IPTV automation."}</p>
      {runtime?.active_source_ids?.length ? (
        <p className="sourceMeta">Active {runtime.active_source_ids.join(", ")}</p>
      ) : null}
      <div className="linkActions">
        <button type="button" className="compactButton streamNow" disabled={pending} onClick={onRefresh}>
          {pending ? "Working..." : "Safe Refresh"}
        </button>
        <button type="button" className="secondary compactButton" disabled={pending} onClick={() => onControl("pause")}>Pause</button>
        <button type="button" className="secondary compactButton" disabled={pending} onClick={() => onControl("resume")}>Resume</button>
        <button type="button" className="secondary compactButton" disabled={pending} onClick={() => onControl("restart")}>Restart Automation</button>
        <button type="button" className="danger compactButton" disabled={pending} onClick={() => onControl("stop")}>Stop Automation</button>
      </div>
      {runtime?.reasons?.length ? (
        <div className="linksList automationEvidence">
          {runtime.reasons.slice(0, 4).map((item, index) => (
            <div className="linkItem sourceItem" key={`${item.title || item.error || "reason"}-${index}`}>
              <div className="linkTop">
                <strong>{item.title || item.error || `Candidate ${index + 1}`}</strong>
                <div className="sourceBadges">
                  {item.score != null ? <Badge>match {item.score}</Badge> : null}
                  {item.probe_score != null ? <Badge>probe {item.probe_score}</Badge> : null}
                </div>
              </div>
              {item.reasons?.length ? <p className="sourceMeta">{item.reasons.join(", ")}</p> : null}
            </div>
          ))}
        </div>
      ) : null}
    </Panel>
  );
}

export function PublicStreamsPanel({
  sources,
  pending,
  onAdd,
  onRemove,
  onScrape,
  onBlock,
}: {
  sources: PublicStreamSource[];
  pending: boolean;
  onAdd: (url: string, label?: string) => Promise<void>;
  onRemove: (source: PublicStreamSource) => Promise<void>;
  onScrape: (url: string) => Promise<{ count: number }>;
  onBlock: (source: PublicStreamSource) => Promise<void>;
}) {
  const [url, setUrl] = useState("");
  const [label, setLabel] = useState("");
  const [scrapeUrl, setScrapeUrl] = useState("");
  const [scraping, setScraping] = useState(false);
  const [scrapeResult, setScrapeResult] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const nextUrl = url.trim();
    if (!nextUrl) return;
    await onAdd(nextUrl, label.trim() || undefined);
    setUrl("");
    setLabel("");
  }

  async function submitScrape(event: React.FormEvent) {
    event.preventDefault();
    const nextUrl = scrapeUrl.trim();
    if (!nextUrl) return;
    setScraping(true);
    setScrapeResult(null);
    try {
      const result = await onScrape(nextUrl);
      setScrapeResult(result.count > 0 ? `Found ${result.count} public stream${result.count !== 1 ? "s" : ""}` : "No public streams found on that page");
      if (result.count > 0) setScrapeUrl("");
    } catch {
      setScrapeResult("Failed to scrape that page");
    } finally {
      setScraping(false);
    }
  }

  return (
    <Panel title="Public Streams" meta={<Badge>{sources.length} available</Badge>} className="linksPanel">
      <form className="addLink scrapeForm" onSubmit={submitScrape}>
        <input
          value={scrapeUrl}
          onChange={(event) => {
            setScrapeUrl(event.target.value);
            setScrapeResult(null);
          }}
          type="url"
          placeholder="https://sportsurge.ws/event/..."
          disabled={scraping}
        />
        <button type="submit" className="streamNow" disabled={scraping || !scrapeUrl.trim()}>
          {scraping ? "Scanning..." : "Find Public"}
        </button>
      </form>
      {scrapeResult && <p className="scrapeResult">{scrapeResult}</p>}
      <form className="addLink publicSourceForm" onSubmit={submit}>
        <input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Label" />
        <input value={url} onChange={(event) => setUrl(event.target.value)} type="url" placeholder="https://third-party.example/live.m3u8" />
        <button type="submit" disabled={pending || !url.trim()}>
          Add
        </button>
      </form>
      <div className="linksList">
        {sources.length ? (
          sources.map((source, index) => (
            <div className="linkItem sourceItem" key={source.id || `${source.url}-${index}`}>
              <div className="linkTop">
                <strong>{source.label || `Public ${index + 1}`}</strong>
                <div className="sourceBadges">
                  <Badge>{source.origin === "auto" ? "auto" : "pasted"}</Badge>
                  <Badge>{source.enabled === false ? "disabled" : "proxied"}</Badge>
                </div>
              </div>
              <p>{source.url}</p>
              {source.playback_url && <p className="sourceMeta">Playback {source.playback_url}</p>}
              <div className="linkActions">
                <a className="buttonLink compactButton" href={source.playback_url || source.url} target="_blank" rel="noreferrer">
                  Test
                </a>
                <button type="button" className="danger compactButton" disabled={pending} onClick={() => onBlock(source)} title="Blacklist this source so it never reappears">
                  Block
                </button>
                <button type="button" className="danger compactButton" disabled={pending || source.read_only} onClick={() => onRemove(source)}>
                  Remove
                </button>
              </div>
            </div>
          ))
        ) : (
          <EmptyLine>No pasted public streams configured.</EmptyLine>
        )}
      </div>
    </Panel>
  );
}

export function BlacklistPanel({
  entries,
  pending,
  onUnblock,
}: {
  entries: BlacklistEntry[];
  pending: boolean;
  onUnblock: (entry: BlacklistEntry) => Promise<void>;
}) {
  return (
    <Panel title="Blacklist" meta={<Badge tone={entries.length ? "warn" : "neutral"}>{entries.length} blocked</Badge>} className="linksPanel">
      <p className="panelHint">Blocked sources never reappear from scraping and are hidden from viewers until unblocked.</p>
      <div className="linksList">
        {entries.length ? (
          entries.map((entry, index) => {
            const primary = blacklistPrimaryLabel(entry, index);
            const key = blacklistKey(entry, index);
            return (
              <div className="linkItem sourceItem" key={key}>
                <div className="linkTop">
                  <strong>{primary}</strong>
                  {entry.reason && <Badge tone="neutral">{entry.reason}</Badge>}
                </div>
                {entry.url && <p>{entry.url}</p>}
                {entry.channel && entry.channel !== primary && <p className="sourceMeta">Channel {entry.channel}</p>}
                <div className="linkActions">
                  <button type="button" className="compactButton streamNow" disabled={pending} onClick={() => onUnblock(entry)}>
                    Unblock
                  </button>
                </div>
              </div>
            );
          })
        ) : (
          <EmptyLine>No blacklisted sources.</EmptyLine>
        )}
      </div>
    </Panel>
  );
}

function TelemetryPanel({
  hls,
  errors,
  events,
  logs,
}: {
  hls?: HlsMetrics;
  errors: LogEntry[];
  events: FeedEvent[];
  logs: LogEntry[];
}) {
  return (
    <Panel title="Telemetry" meta={<Badge>ffmpeg + hls</Badge>} className="telemetryPanel">
      <div className="metricGrid three">
        <MetricTile label="Media sequence" value={hls?.media_sequence || "n/a"} />
        <MetricTile label="Target duration" value={hls?.target_duration ? `${hls.target_duration}s` : "n/a"} />
        <MetricTile label="Playlist lines" value={String(hls?.playlist_line_count ?? "n/a")} />
        <MetricTile label="First segment" value={hls?.first_segment || "n/a"} />
        <MetricTile label="Last segment" value={hls?.last_segment || "n/a"} />
        <MetricTile label="Last segment size" value={hls?.last_segment_size ? fmtBytes(hls.last_segment_size) : "n/a"} />
      </div>
      <div className="telemetryFeeds">
        <FeedBlock title="Playlist tail" empty="No playlist segments yet.">
          {(hls?.playlist_segments || []).slice(-16).reverse().map((name) => <FeedLine key={name}>{name}</FeedLine>)}
        </FeedBlock>
        <FeedBlock title="Real ffmpeg errors" empty="No ffmpeg errors captured yet.">
          {errors.slice(-16).reverse().map((entry, index) => (
            <FeedLine key={`${entry.ts || index}-${entry.line}`} tone="bad">
              {fmtClock(entry.ts)} {entry.line || JSON.stringify(entry)}
            </FeedLine>
          ))}
        </FeedBlock>
        <FeedBlock title="Events" empty="No events yet.">
          {events.slice(-20).reverse().map((entry, index) => (
            <FeedLine key={`${entry.ts || index}-${entry.message}`} tone={entry.level || "info"}>
              <span className="feedHead">
                <strong>{entry.level || "info"}</strong>
                <span>{fmtClock(entry.ts)}</span>
              </span>
              {entry.message || ""}
            </FeedLine>
          ))}
        </FeedBlock>
        <FeedBlock title="Logs" empty="No logs yet.">
          {logs.slice(-28).reverse().map((entry, index) => (
            <FeedLine key={`${entry.ts || index}-${entry.line}`} tone={entry.level || "info"}>
              {entry.line || JSON.stringify(entry)}
            </FeedLine>
          ))}
        </FeedBlock>
      </div>
    </Panel>
  );
}

function FeedBlock({ title, empty, children }: { title: string; empty: string; children: ReactNode }) {
  const hasChildren = Array.isArray(children) ? children.length > 0 : Boolean(children);
  return (
    <div className="subsection">
      <h3>{title}</h3>
      <div className="feedBox">{hasChildren ? children : <EmptyLine>{empty}</EmptyLine>}</div>
    </div>
  );
}

function FooterStatus({ hls, sessionState }: { hls?: HlsMetrics; sessionState: string }) {
  return (
    <footer className="footerStatus">
      <span className="inlineMetric">
        Dashboard HLS <strong>{hls?.dashboard_hls_url || "/hls/ufc.m3u8"}</strong>
      </span>
      <span className="inlineMetric">
        Public HLS <strong>{hls?.public_hls_url || "n/a"}</strong>
      </span>
      <span className="inlineMetric">
        Session <strong>{sessionState}</strong>
      </span>
    </footer>
  );
}

export default function App() {
  const [locked, setLocked] = useState(true);
  const [authChecked, setAuthChecked] = useState(false);
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [gpu, setGpu] = useState<GpuTelemetryPayload | null>(null);
  const [arango, setArango] = useState<ArangoStatus | null>(null);
  const [sessionState, setSessionState] = useState("active");
  const [pendingAction, setPendingAction] = useState("");
  const [pendingEncoder, setPendingEncoder] = useState(false);
  const [pendingLinks, setPendingLinks] = useState(false);
  const [pendingPrivateIptv, setPendingPrivateIptv] = useState(false);
  const [pendingSchedule, setPendingSchedule] = useState(false);
  const [sourceOverride, setSourceOverride] = useState<string | null>(null);
  const [sourceBusy, setSourceBusy] = useState(false);
  const [sourceMsg, setSourceMsg] = useState<string | null>(null);
  const authenticated = !locked;
  // Plain derived values: the React Compiler memoizes these; manual useMemo here
  // tripped react-hooks/preserve-manual-memoization on the mixed source/config deps.
  const configuredSources = (status?.sources || status?.config.stream?.sources || []) as SourceStatus[];
  const publicStreams = status?.config.public_sources || [];
  const blacklist = status?.config.source_blacklist || [];

  const logout = useCallback(() => {
    setLocked(true);
    setAuthChecked(true);
    setStatus(null);
    setGpu(null);
    setArango(null);
    setSessionState("locked");
  }, []);

  const refreshStatus = useCallback(async () => {
    try {
      const data = await api<StatusPayload>("/api/status");
      setLocked(false);
      setAuthChecked(true);
      setStatus(data);
      setSessionState("active");
      const arangoStatus = await api<ArangoStatus>("/api/arango").catch((err) => ({
        ok: true,
        connected: false,
        error: errorMessage(err),
      }));
      setArango(arangoStatus);
    } catch (err) {
      if (isUnauthorized(err)) {
        logout();
        return;
      }
      setAuthChecked(true);
      setSessionState(`error: ${errorMessage(err)}`);
    }
  }, [logout]);

  const refreshGpuTelemetry = useCallback(async () => {
    try {
      const data = await api<GpuTelemetryPayload>("/api/nvidia-smi");
      setGpu(data);
    } catch (err) {
      if (isUnauthorized(err)) {
        logout();
        return;
      }
      setGpu({
        ok: true,
        available: false,
        level: "bad",
        message: `GPU telemetry error: ${errorMessage(err)}`,
        diagnosis: [errorMessage(err)],
        errors: [errorMessage(err)],
        summary: {
          gpu_count: 0,
          process_count: 0,
          ffmpeg_process_count: 0,
          stream_gpu_active: false,
        },
        gpus: [],
        processes: [],
      });
    }
  }, [logout]);

  useEffect(() => {
    void refreshStatus();
    if (!authenticated) return;
    void refreshGpuTelemetry();
    const statusTimer = window.setInterval(() => void refreshStatus(), 2500);
    const gpuTimer = window.setInterval(() => void refreshGpuTelemetry(), 5000);
    return () => {
      window.clearInterval(statusTimer);
      window.clearInterval(gpuTimer);
    };
  }, [authenticated, refreshGpuTelemetry, refreshStatus]);

  async function login(password: string) {
    await api<{ ok: boolean }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    setLocked(false);
    setAuthChecked(true);
    setSessionState("active");
    await refreshStatus();
  }

  async function streamAction(action: "start" | "restart" | "stop") {
    setPendingAction(action);
    try {
      await api(`/api/stream/${action}`, {
        method: "POST",
        body: JSON.stringify({ kill_existing: true }),
      });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`${action} error: ${errorMessage(err)}`);
    } finally {
      setPendingAction("");
    }
  }

  async function setEncoderMode(encoder: EncoderMode) {
    setPendingEncoder(true);
    try {
      await api("/api/config", {
        method: "PUT",
        body: JSON.stringify({ encoder }),
      });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`encoder error: ${errorMessage(err)}`);
    } finally {
      setPendingEncoder(false);
    }
  }

  async function addLink(url: string) {
    setPendingLinks(true);
    try {
      await api("/api/links", {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`link error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  async function removeLink(url: string) {
    setPendingLinks(true);
    try {
      await api("/api/links/remove", {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`link error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  async function activateSource(source: SourceStatus) {
    setPendingLinks(true);
    try {
      await api("/api/sources/activate", {
        method: "POST",
        body: JSON.stringify({ id: source.id, url: source.url }),
      });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`switch error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  async function recoverSource(source: SourceStatus) {
    setPendingLinks(true);
    try {
      await api("/api/sources/recover-soursignal", {
        method: "POST",
        body: JSON.stringify({ id: source.id, url: source.url }),
      });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`recover error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  async function lockSource(source: SourceStatus, locked: boolean) {
    setPendingLinks(true);
    try {
      await api("/api/sources/lock", { method: "POST", body: JSON.stringify({ id: source.id, locked }) });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`lock error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  async function addPublicStream(url: string, label?: string) {
    setPendingLinks(true);
    try {
      await api("/api/public-streams", {
        method: "POST",
        body: JSON.stringify({ url, label }),
      });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`public source error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  async function removePublicStream(source: PublicStreamSource) {
    setPendingLinks(true);
    try {
      await api("/api/public-streams/remove", {
        method: "POST",
        body: JSON.stringify({ id: source.id, url: source.url }),
      });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`public source error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  async function blockSource(source: { url?: string; id?: string; label?: string }) {
    setPendingLinks(true);
    try {
      await api("/api/blacklist", {
        method: "POST",
        body: JSON.stringify(blockPayload(source)),
      });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`blacklist error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  async function unblockSource(entry: BlacklistEntry) {
    setPendingLinks(true);
    try {
      await api("/api/blacklist/remove", {
        method: "POST",
        body: JSON.stringify({ url: entry.url, id: entry.id }),
      });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`blacklist error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  async function watchSource(inputUrl: string) {
    setSourceBusy(true);
    setSourceMsg(null);
    try {
      const isDirectStream =
        inputUrl.split("?")[0].endsWith(".m3u8") ||
        inputUrl.includes("load-playlist");
      let streamUrl = inputUrl;
      if (!isDirectStream) {
        const data = (await api("/api/scrape", {
          method: "POST",
          body: JSON.stringify({ url: inputUrl }),
        })) as { ok: boolean; links: string[]; count: number };
        if (!data.ok || !data.links?.length) {
          setSourceMsg("No streams found — try a direct stream URL");
          return;
        }
        streamUrl = data.links[0];
      }
      const proxied = `/api/proxy-hls?url=${encodeURIComponent(streamUrl)}`;
      setSourceOverride(proxied);
      setSourceMsg("Watching custom source — stream active in player");
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSourceMsg("Failed to load source");
    } finally {
      setSourceBusy(false);
    }
  }

  function clearSource() {
    setSourceOverride(null);
    setSourceMsg(null);
  }

  async function scrapeLinks(url: string): Promise<{ count: number }> {
    const data = await api("/api/scrape", {
      method: "POST",
      body: JSON.stringify({ url }),
    }) as { ok: boolean; links: string[]; count: number };
    if (!data.ok || !data.links?.length) return { count: 0 };
    for (const link of data.links) {
      try {
        await api("/api/public-streams", { method: "POST", body: JSON.stringify({ url: link, label: "Public stream" }) });
      } catch {
        // Skip duplicates or invalid scraped links; the server validates each URL.
      }
    }
    await refreshStatus();
    return { count: data.links.length };
  }

  async function toggleSchedule(enabled: boolean) {
    setPendingSchedule(true);
    try {
      await api("/api/schedule", { method: "POST", body: JSON.stringify({ enabled }) });
      await refreshStatus();
      setSessionState(enabled ? "auto-schedule enabled" : "auto-schedule disabled");
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`auto-schedule error: ${errorMessage(err)}`);
    } finally {
      setPendingSchedule(false);
    }
  }

  async function sendScheduleTest() {
    setPendingSchedule(true);
    try {
      await api("/api/schedule", { method: "POST", body: JSON.stringify({ test_notification: true }) });
      setSessionState("test notification posted to Discord");
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`Discord test failed: ${errorMessage(err)}`);
    } finally {
      setPendingSchedule(false);
    }
  }

  async function sendComingUp() {
    setPendingSchedule(true);
    try {
      const res = await api<{ event?: string }>("/api/schedule", { method: "POST", body: JSON.stringify({ coming_up: true }) });
      setSessionState(`posted "Coming up" for ${res.event || "the next card"}`);
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`Coming up post failed: ${errorMessage(err)}`);
    } finally {
      setPendingSchedule(false);
    }
  }

  async function refreshPrivateIptv() {
    setPendingPrivateIptv(true);
    try {
      await api("/api/private-iptv/refresh", { method: "POST", body: JSON.stringify({}) });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`private IPTV error: ${errorMessage(err)}`);
    } finally {
      setPendingPrivateIptv(false);
    }
  }

  async function controlPrivateIptv(action: "pause" | "resume" | "stop" | "restart") {
    setPendingPrivateIptv(true);
    try {
      await api("/api/private-iptv/control", { method: "POST", body: JSON.stringify({ action }) });
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`private IPTV control error: ${errorMessage(err)}`);
    } finally {
      setPendingPrivateIptv(false);
    }
  }

  if (!authenticated && !authChecked) return <main className="loginShell" />;
  if (!authenticated) return <LoginScreen onLogin={login} />;

  const hls = status?.hls || {};
  const proc = status?.managed_process || {};
  const health = status?.health || {};
  const stream = status?.config.stream || {};
  const external = status?.existing_processes || [];
  const errors = status?.errors || health.recent_errors || [];

  return (
    <main className="appShell">
      <CommandHeader status={status} pendingAction={pendingAction} onStreamAction={streamAction} onToggleSchedule={toggleSchedule} />
      <StatusStrip status={status} gpu={gpu} arango={arango} />
      <SchedulePanel schedule={status?.schedule} pending={pendingSchedule} onTest={sendScheduleTest} onComingUp={sendComingUp} />
      <section className="primaryGrid">
        <LivePlayer
          proc={proc}
          hls={hls}
          health={health}
          overrideUrl={sourceOverride}
          onWatchSource={watchSource}
          onClearSource={clearSource}
          sourceBusy={sourceBusy}
          sourceMsg={sourceMsg}
        />
        <aside className="rightRail">
          <HealthPanel health={health} hls={hls} proc={proc} />
          <ProcessPanel
            proc={proc}
            external={external}
            encoder={stream.encoder}
            errorCount={errors.length}
            pendingEncoder={pendingEncoder}
            onSetEncoder={setEncoderMode}
          />
          <GpuPanel gpu={gpu} />
        </aside>
      </section>
      <section className="lowerGrid">
        <SourcesPanel sources={configuredSources} pending={pendingLinks} onAdd={addLink} onRemove={removeLink} onActivate={activateSource} onLock={lockSource} onRecover={recoverSource} onBlock={blockSource} />
        <PrivateIptvPanel runtime={status?.private_iptv} pending={pendingPrivateIptv} onRefresh={refreshPrivateIptv} onControl={controlPrivateIptv} />
        <PublicStreamsPanel sources={publicStreams} pending={pendingLinks} onAdd={addPublicStream} onRemove={removePublicStream} onScrape={scrapeLinks} onBlock={blockSource} />
        <BlacklistPanel entries={blacklist} pending={pendingLinks} onUnblock={unblockSource} />
        <TelemetryPanel hls={hls} errors={errors} events={status?.events || []} logs={status?.logs || []} />
      </section>
      <FooterStatus hls={hls} sessionState={sessionState} />
    </main>
  );
}
