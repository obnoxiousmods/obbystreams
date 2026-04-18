import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import videojs from "video.js";
import type Player from "video.js/dist/types/player";
import { api, isUnauthorized } from "./api";
import { absoluteUrl, encoderLabel, errorMessage, fmtAge, fmtBytes, fmtClock, fmtMetric, fmtPercent, toneFromLevel } from "./format";
import type {
  ArangoStatus,
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
  StatusPayload,
  Tone,
} from "./types";

const TOKEN_KEY = "obbystreams_token";

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

  return (
    <section className="statusStrip" aria-label="stream status">
      <MetricTile label="Run" value={runText} tone={runTone} />
      <MetricTile label="Health" value={`${health.level || "warn"}: ${health.state || "unknown"}`} tone={healthTone} />
      <MetricTile label="Encoder" value={stream.encoder || "auto"} />
      <MetricTile label="GPU" value={gpu ? (gpu.available ? gpu.level || "online" : "offline") : "checking"} tone={gpuTone} />
      <MetricTile label="ArangoDB" value={arango ? (arango.connected ? "connected" : "offline") : "checking"} tone={arangoTone} />
      <MetricTile label="Updated" value={fmtClock(status?.server_time)} />
    </section>
  );
}

function CommandHeader({
  status,
  pendingAction,
  onStreamAction,
}: {
  status: StatusPayload | null;
  pendingAction: string;
  onStreamAction: (action: "start" | "restart" | "stop") => Promise<void>;
}) {
  const proc = status?.managed_process || {};
  const hls = status?.hls || {};
  const busy = Boolean(pendingAction);
  const hlsUrl = hls.public_hls_url || hls.dashboard_hls_url || "Waiting for HLS output";

  return (
    <header className="commandHeader">
      <div>
        <p className="kicker">Obbystreams</p>
        <h1>Stream Control Center</h1>
        <p className="monoLine">{hlsUrl}</p>
      </div>
      <div className="commandActions">
        <button type="button" disabled={busy || Boolean(proc.managed)} onClick={() => onStreamAction("start")}>
          {pendingAction === "start" ? "Starting" : "Start"}
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={() => onStreamAction("restart")}>
          {pendingAction === "restart" ? "Restarting" : "Restart"}
        </button>
        <button type="button" className="danger" disabled={busy || !proc.managed} onClick={() => onStreamAction("stop")}>
          {pendingAction === "stop" ? "Stopping" : "Stop"}
        </button>
      </div>
    </header>
  );
}

function LivePlayer({ proc, hls, health }: { proc?: ManagedProcess; hls?: HlsMetrics; health?: HealthAssessment }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const playerRef = useRef<Player | null>(null);
  const retryTimerRef = useRef<number | null>(null);
  const retryMsRef = useRef(800);
  const [playerState, setPlayerState] = useState("player idle");
  const [isPaused, setIsPaused] = useState(true);
  const [isMuted, setIsMuted] = useState(true);
  const [volume, setVolume] = useState(0);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [atLiveEdge, setAtLiveEdge] = useState(true);
  const playerUrl = proc?.managed && hls?.playlist_ready ? hls.dashboard_hls_url || hls.public_hls_url || "" : "";
  const displayUrl = hls?.public_hls_url || hls?.dashboard_hls_url || "";

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

  function toggleFullscreen() {
    const player = playerRef.current;
    if (!player || player.isDisposed()) return;
    if (player.isFullscreen()) {
      player.exitFullscreen();
    } else {
      player.requestFullscreen();
    }
    syncPlayerState(player);
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

  return (
    <Panel title="Live Player" meta={<Badge tone={playerState === "playing" ? "ok" : "neutral"}>{playerState}</Badge>} className="stagePanel">
      <div className="videoSurface">
        <video ref={videoRef} className="video-js vjs-big-play-centered vjs-obby" preload="auto" playsInline />
        <div className="customPlayerChrome">
          <div className="playerTopRail">
            <Badge tone={proc?.managed && hls?.playlist_ready ? "ok" : "warn"}>LIVE</Badge>
            <span>{playerState}</span>
          </div>
          <div className="playerBottomRail">
            <button type="button" className="playerControl primary" disabled={!canUsePlayer} onClick={togglePlayback}>
              {isPaused ? "Play" : "Pause"}
            </button>
            <button type="button" className="playerControl" disabled={!canUsePlayer} onClick={toggleMute}>
              {isMuted || volume === 0 ? "Muted" : "Sound"}
            </button>
            <label className="volumeControl">
              <span>Vol</span>
              <input
                type="range"
                min="0"
                max="1"
                step="0.01"
                value={volume}
                disabled={!canUsePlayer}
                onChange={(event) => updateVolume(Number(event.target.value))}
              />
            </label>
            <button type="button" className="playerControl" disabled={!canUsePlayer} onClick={() => loadSource("manual")}>
              Reload
            </button>
            <button type="button" className="playerControl" disabled={!canUsePlayer || atLiveEdge} onClick={goLive}>
              Go live
            </button>
            <button type="button" className="playerControl" onClick={copyHls}>
              Copy HLS
            </button>
            <a className="playerControl link" href={absoluteUrl(playerUrl || displayUrl) || "#"} target="_blank" rel="noreferrer">
              Open
            </a>
            <button type="button" className="playerControl" disabled={!canUsePlayer} onClick={toggleFullscreen}>
              {isFullscreen ? "Exit full" : "Full"}
            </button>
          </div>
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
  onSetEncoder: (encoder: "auto" | "gpu-only" | "cpu") => Promise<void>;
}) {
  const mode = encoder || "auto";
  return (
    <div className="encoderControl">
      <div>
        <span>Encoder mode</span>
        <strong>{pending ? "Updating..." : encoderLabel(mode)}</strong>
      </div>
      <div className="segmentedControl" aria-label="Encoder mode">
        <button type="button" className={mode === "auto" ? "active" : ""} disabled={pending} onClick={() => onSetEncoder("auto")}>
          Auto
        </button>
        <button type="button" className={mode === "gpu-only" ? "active" : ""} disabled={pending} onClick={() => onSetEncoder("gpu-only")}>
          GPU
        </button>
        <button type="button" className={mode === "cpu" ? "active" : ""} disabled={pending} onClick={() => onSetEncoder("cpu")}>
          CPU
        </button>
      </div>
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
  onSetEncoder: (encoder: "auto" | "gpu-only" | "cpu") => Promise<void>;
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

function LinksPanel({
  links,
  dirty,
  pending,
  onAdd,
  onMove,
  onRemove,
  onSave,
}: {
  links: string[];
  dirty: boolean;
  pending: boolean;
  onAdd: (url: string) => Promise<void>;
  onMove: (index: number, direction: -1 | 1) => void;
  onRemove: (url: string) => Promise<void>;
  onSave: () => Promise<void>;
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
      title="Links"
      meta={
        <button type="button" className="secondary compactButton" disabled={pending || !dirty} onClick={onSave}>
          {pending ? "Saving" : dirty ? "Save order" : "Saved"}
        </button>
      }
      className="linksPanel"
    >
      <form className="addLink" onSubmit={submit}>
        <input value={newLink} onChange={(event) => setNewLink(event.target.value)} type="url" placeholder="https://example.com/live.m3u8" />
        <button type="submit" disabled={pending}>
          Add
        </button>
      </form>
      <div className="linksList">
        {links.length ? (
          links.map((url, index) => (
            <div className="linkItem" key={`${url}-${index}`}>
              <div className="linkTop">
                <strong>Link {index + 1}</strong>
                <a className="buttonLink compactButton" href={url} target="_blank" rel="noreferrer">
                  Open
                </a>
              </div>
              <p>{url}</p>
              <div className="linkActions">
                <button type="button" className="secondary compactButton" disabled={pending || index === 0} onClick={() => onMove(index, -1)}>
                  Up
                </button>
                <button type="button" className="secondary compactButton" disabled={pending || index === links.length - 1} onClick={() => onMove(index, 1)}>
                  Down
                </button>
                <button type="button" className="danger compactButton" disabled={pending} onClick={() => onRemove(url)}>
                  Remove
                </button>
              </div>
            </div>
          ))
        ) : (
          <EmptyLine>No stream links configured.</EmptyLine>
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
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) || "");
  const [locked, setLocked] = useState(() => !localStorage.getItem(TOKEN_KEY));
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [gpu, setGpu] = useState<GpuTelemetryPayload | null>(null);
  const [arango, setArango] = useState<ArangoStatus | null>(null);
  const [sessionState, setSessionState] = useState("active");
  const [pendingAction, setPendingAction] = useState("");
  const [pendingEncoder, setPendingEncoder] = useState(false);
  const [pendingLinks, setPendingLinks] = useState(false);
  const [localLinks, setLocalLinks] = useState<string[]>([]);
  const [linksDirty, setLinksDirty] = useState(false);
  const authenticated = Boolean(token) && !locked;
  const serverLinks = useMemo(() => status?.config.stream?.links || [], [status?.config.stream?.links]);

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setLocked(true);
    setStatus(null);
    setGpu(null);
    setArango(null);
    setSessionState("locked");
  }, []);

  const refreshStatus = useCallback(async () => {
    if (!token) return;
    try {
      const data = await api<StatusPayload>("/api/status", token);
      setStatus(data);
      setSessionState("active");
      const arangoStatus = await api<ArangoStatus>("/api/arango", token).catch((err) => ({
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
      setSessionState(`error: ${errorMessage(err)}`);
    }
  }, [logout, token]);

  const refreshGpuTelemetry = useCallback(async () => {
    if (!token) return;
    try {
      const data = await api<GpuTelemetryPayload>("/api/nvidia-smi", token);
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
  }, [logout, token]);

  useEffect(() => {
    if (!authenticated) return;
    void refreshStatus();
    void refreshGpuTelemetry();
    const statusTimer = window.setInterval(() => void refreshStatus(), 2500);
    const gpuTimer = window.setInterval(() => void refreshGpuTelemetry(), 5000);
    return () => {
      window.clearInterval(statusTimer);
      window.clearInterval(gpuTimer);
    };
  }, [authenticated, refreshGpuTelemetry, refreshStatus]);

  useEffect(() => {
    if (!linksDirty) setLocalLinks(serverLinks);
  }, [linksDirty, serverLinks]);

  async function login(password: string) {
    const data = await api<{ ok: boolean; token: string }>("/api/auth/login", "", {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    setToken(data.token);
    localStorage.setItem(TOKEN_KEY, data.token);
    setLocked(false);
    setSessionState("active");
  }

  async function streamAction(action: "start" | "restart" | "stop") {
    setPendingAction(action);
    try {
      await api(`/api/stream/${action}`, token, {
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

  async function setEncoderMode(encoder: "auto" | "gpu-only" | "cpu") {
    setPendingEncoder(true);
    try {
      await api("/api/config", token, {
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
      await api("/api/links", token, {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      setLinksDirty(false);
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`link error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  function moveLink(index: number, direction: -1 | 1) {
    setLocalLinks((current) => {
      const target = index + direction;
      if (target < 0 || target >= current.length) return current;
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
    setLinksDirty(true);
  }

  async function removeLink(url: string) {
    setPendingLinks(true);
    try {
      await api("/api/links/remove", token, {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      setLinksDirty(false);
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`link error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  async function saveLinks() {
    setPendingLinks(true);
    try {
      await api("/api/config", token, {
        method: "PUT",
        body: JSON.stringify({ links: localLinks }),
      });
      setLinksDirty(false);
      await refreshStatus();
    } catch (err) {
      if (isUnauthorized(err)) logout();
      setSessionState(`link save error: ${errorMessage(err)}`);
    } finally {
      setPendingLinks(false);
    }
  }

  if (!authenticated) return <LoginScreen onLogin={login} />;

  const hls = status?.hls || {};
  const proc = status?.managed_process || {};
  const health = status?.health || {};
  const stream = status?.config.stream || {};
  const external = status?.existing_processes || [];
  const errors = status?.errors || health.recent_errors || [];

  return (
    <main className="appShell">
      <CommandHeader status={status} pendingAction={pendingAction} onStreamAction={streamAction} />
      <StatusStrip status={status} gpu={gpu} arango={arango} />
      <section className="primaryGrid">
        <LivePlayer proc={proc} hls={hls} health={health} />
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
        <LinksPanel links={localLinks} dirty={linksDirty} pending={pendingLinks} onAdd={addLink} onMove={moveLink} onRemove={removeLink} onSave={saveLinks} />
        <TelemetryPanel hls={hls} errors={errors} events={status?.events || []} logs={status?.logs || []} />
      </section>
      <FooterStatus hls={hls} sessionState={sessionState} />
    </main>
  );
}
