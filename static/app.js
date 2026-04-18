const state = {
  token: localStorage.getItem("obbystreams_token") || "",
  config: null,
  links: [],
  player: null,
  hlsUrl: "",
  playerUrl: "",
  playRetryTimer: null,
  playRetryMs: 800,
};

const $ = (id) => document.getElementById(id);

function fmtBytes(bytes) {
  if (!bytes) return "0 MB";
  const units = ["B", "KB", "MB", "GB"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtAge(seconds) {
  if (seconds == null) return "waiting";
  if (seconds < 1) return "now";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}

function fmtClock(ms) {
  if (!ms) return "n/a";
  return new Date(ms).toLocaleTimeString();
}

function fmtPercent(value, digits = 0) {
  if (value == null) return "n/a";
  return `${Number(value).toFixed(digits)}%`;
}

function fmtMetric(value, suffix = "", digits = 0) {
  if (value == null) return "n/a";
  return `${Number(value).toFixed(digits)}${suffix}`;
}

function absoluteUrl(url) {
  if (!url) return "";
  return new URL(url, window.location.origin).toString();
}

function setText(id, value) {
  const node = $(id);
  if (!node) return;
  node.textContent = value;
}

function setBadge(id, text, tone = "") {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.classList.remove("ok", "warn", "bad");
  if (tone) node.classList.add(tone);
}

function setActiveToggle(id, active) {
  const node = $(id);
  if (!node) return;
  node.classList.toggle("active", active);
  node.setAttribute("aria-pressed", active ? "true" : "false");
}

function encoderLabel(encoder) {
  const labels = {
    auto: "Auto",
    "gpu-only": "GPU only",
    cpu: "CPU only",
  };
  return labels[encoder || "auto"] || encoder || "Auto";
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers["x-obbystreams-token"] = state.token;
  const res = await fetch(path, { ...options, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  setText("sessionState", "active");
}

function showLogin() {
  $("login").classList.remove("hidden");
  $("app").classList.add("hidden");
  setText("sessionState", "locked");
}

async function login(event) {
  event.preventDefault();
  setText("loginError", "");
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: $("password").value }),
    });
    state.token = data.token;
    localStorage.setItem("obbystreams_token", state.token);
    showApp();
    await refresh();
    await refreshGpuTelemetry();
  } catch (err) {
    setText("loginError", err.message);
  }
}

function renderLinks(links) {
  state.links = [...links];
  const root = $("links");
  root.innerHTML = "";

  state.links.forEach((url, index) => {
    const item = document.createElement("div");
    item.className = "linkItem";

    const meta = document.createElement("div");
    meta.className = "linkMeta";

    const idx = document.createElement("div");
    idx.className = "linkIndex";
    idx.textContent = `Link ${index + 1}`;

    const open = document.createElement("a");
    open.className = "buttonLink";
    open.href = url;
    open.target = "_blank";
    open.rel = "noreferrer";
    open.textContent = "Open";
    meta.append(idx, open);

    const urlText = document.createElement("div");
    urlText.className = "linkUrl";
    urlText.textContent = url;

    const controls = document.createElement("div");
    controls.className = "linkControls";

    const up = document.createElement("button");
    up.className = "secondary";
    up.textContent = "Up";
    up.onclick = () => {
      if (index > 0) {
        [state.links[index - 1], state.links[index]] = [state.links[index], state.links[index - 1]];
        renderLinks(state.links);
      }
    };

    const down = document.createElement("button");
    down.className = "secondary";
    down.textContent = "Down";
    down.onclick = () => {
      if (index < state.links.length - 1) {
        [state.links[index + 1], state.links[index]] = [state.links[index], state.links[index + 1]];
        renderLinks(state.links);
      }
    };

    const remove = document.createElement("button");
    remove.className = "danger";
    remove.textContent = "Remove";
    remove.onclick = async () => {
      await api("/api/links/remove", { method: "POST", body: JSON.stringify({ url }) });
      await refresh();
    };

    controls.append(up, down, remove);
    item.append(meta, urlText, controls);
    root.append(item);
  });

  if (state.links.length === 0) {
    const empty = document.createElement("div");
    empty.className = "emptyLine";
    empty.textContent = "No stream links configured.";
    root.append(empty);
  }
}

function appendFeedItem(root, level, ts, message) {
  const item = document.createElement("div");
  item.className = `feedItem ${level || "info"}`;

  const head = document.createElement("div");
  head.className = "feedItemHead";
  const left = document.createElement("strong");
  left.textContent = level || "info";
  const right = document.createElement("span");
  right.textContent = ts ? new Date(ts).toLocaleTimeString() : "";
  head.append(left, right);

  const body = document.createElement("div");
  body.textContent = message || "";

  item.append(head, body);
  root.append(item);
}

function renderEvents(events) {
  const root = $("events");
  root.innerHTML = "";
  events.slice(-20).reverse().forEach((entry) => {
    appendFeedItem(root, entry.level, entry.ts, entry.message);
  });
}

function renderLogs(logs) {
  const root = $("logs");
  root.innerHTML = "";
  logs.slice(-28).reverse().forEach((log) => {
    const item = document.createElement("div");
    item.className = `logLine ${log.level || ""}`;
    item.textContent = log.line || JSON.stringify(log);
    root.append(item);
  });
}

function renderErrors(errors) {
  const root = $("errors");
  root.innerHTML = "";
  const list = (errors || []).slice(-16).reverse();
  list.forEach((entry) => {
    const item = document.createElement("div");
    item.className = "errorLine";
    const ts = entry.ts ? new Date(entry.ts).toLocaleTimeString() : "";
    item.textContent = `${ts} ${entry.line || JSON.stringify(entry)}`.trim();
    root.append(item);
  });
  if (list.length === 0) {
    const empty = document.createElement("div");
    empty.className = "emptyLine";
    empty.textContent = "No ffmpeg errors captured yet.";
    root.append(empty);
  }
  setText("errorCount", String((errors || []).length));
}

function renderSegments(segments) {
  const root = $("playlistSegments");
  root.innerHTML = "";
  (segments || []).slice(-16).reverse().forEach((name) => {
    const item = document.createElement("div");
    item.className = "segmentLine";
    item.textContent = name;
    root.append(item);
  });
}

function renderProcessLists(managed, external) {
  const childRoot = $("children");
  childRoot.innerHTML = "";
  const children = managed.children || [];
  if (!children.length) {
    const empty = document.createElement("div");
    empty.className = "emptyLine";
    empty.textContent = "No child process.";
    childRoot.append(empty);
  } else {
    children.forEach((proc) => {
      appendFeedItem(childRoot, "info", null, `pid ${proc.pid} ${proc.name} | rss ${fmtBytes(proc.rss)} | cpu ${(proc.cpu ?? 0).toFixed(1)}%`);
    });
  }

  const extRoot = $("external");
  extRoot.innerHTML = "";
  if (!external.length) {
    const empty = document.createElement("div");
    empty.className = "emptyLine";
    empty.textContent = "No other stream process detected.";
    extRoot.append(empty);
  } else {
    external.forEach((proc) => {
      appendFeedItem(extRoot, "warn", null, `pid ${proc.pid} | age ${fmtAge(proc.age)} | ${proc.cmd}`);
    });
  }
}

function renderEncoderMode(encoder) {
  const mode = encoder || "auto";
  setText("encoderMode", encoderLabel(mode));
  setActiveToggle("autoEncoderBtn", mode === "auto");
  setActiveToggle("gpuOnlyBtn", mode === "gpu-only");
  setActiveToggle("cpuOnlyBtn", mode === "cpu");
}

function gpuTone(gpu) {
  if ((gpu.temperature_c || 0) >= 88) return "bad";
  if ((gpu.memory_used_pct || 0) >= 92) return "warn";
  return "info";
}

function renderGpuTelemetry(data) {
  const summary = data.summary || {};
  const gpus = data.gpus || [];
  const processes = data.processes || [];
  const primary = gpus[0] || {};
  const tone = data.available ? data.level || "ok" : "bad";
  const stateText = data.available ? (data.level === "ok" ? "online" : data.level || "warn") : "offline";
  setBadge("gpuState", stateText, tone);
  setText("gpuUpdated", data.checked_at ? `${new Date(data.checked_at).toLocaleTimeString()} | 5s` : "5s");
  setText("gpuMessage", data.message || "No GPU telemetry.");
  setText("gpuDriver", summary.driver_version || "n/a");
  setText("gpuUtil", fmtPercent(summary.max_gpu_utilization_pct));

  if (primary.memory_used_mb != null && primary.memory_total_mb != null) {
    const used = fmtBytes(primary.memory_used_mb * 1024 * 1024);
    const total = fmtBytes(primary.memory_total_mb * 1024 * 1024);
    setText("gpuMemory", `${used} / ${total} (${fmtPercent(primary.memory_used_pct)})`);
  } else {
    setText("gpuMemory", fmtPercent(summary.max_memory_used_pct));
  }

  setText("gpuTemp", fmtMetric(summary.max_temperature_c, "C"));
  if (summary.power_draw_w != null && summary.power_limit_w != null) {
    setText("gpuPower", `${fmtMetric(summary.power_draw_w, "W", 1)} / ${fmtMetric(summary.power_limit_w, "W", 1)}`);
  } else {
    setText("gpuPower", fmtMetric(summary.power_draw_w, "W", 1));
  }
  const encoderBits = [];
  if (summary.encoder_session_count != null) encoderBits.push(`${summary.encoder_session_count} sessions`);
  if (summary.encoder_utilization_pct != null) encoderBits.push(`${summary.encoder_utilization_pct}% enc`);
  setText("gpuEncoder", encoderBits.length ? encoderBits.join(" | ") : "n/a");
  setText("gpuProcessCount", String(summary.process_count ?? processes.length));
  setText("gpuFfmpeg", summary.stream_gpu_active ? "visible" : "not visible");

  const gpuRoot = $("gpuList");
  gpuRoot.innerHTML = "";
  if (!gpus.length) {
    const empty = document.createElement("div");
    empty.className = "emptyLine";
    empty.textContent = "No GPU rows parsed from nvidia-smi.";
    gpuRoot.append(empty);
  } else {
    gpus.forEach((gpu) => {
      const memory = gpu.memory_used_mb != null && gpu.memory_total_mb != null
        ? `${fmtBytes(gpu.memory_used_mb * 1024 * 1024)} / ${fmtBytes(gpu.memory_total_mb * 1024 * 1024)}`
        : "memory n/a";
      appendFeedItem(
        gpuRoot,
        gpuTone(gpu),
        null,
        `GPU ${gpu.index ?? "?"} ${gpu.name || "unknown"} | util ${fmtPercent(gpu.gpu_utilization_pct)} | mem ${memory} | temp ${fmtMetric(gpu.temperature_c, "C")} | power ${fmtMetric(gpu.power_draw_w, "W", 1)} | ${gpu.pstate || "pstate n/a"}`
      );
    });
  }

  const processRoot = $("gpuProcesses");
  processRoot.innerHTML = "";
  if (!processes.length) {
    const empty = document.createElement("div");
    empty.className = "emptyLine";
    empty.textContent = "No GPU processes visible.";
    processRoot.append(empty);
  } else {
    processes.forEach((proc) => {
      const procTone = proc.is_ffmpeg ? "ok" : "info";
      const memory = proc.used_memory_mb == null ? "memory n/a" : `${proc.used_memory_mb} MB`;
      const enc = proc.enc_pct == null ? "enc n/a" : `enc ${proc.enc_pct}%`;
      const dec = proc.dec_pct == null ? "dec n/a" : `dec ${proc.dec_pct}%`;
      appendFeedItem(
        processRoot,
        procTone,
        null,
        `GPU ${proc.gpu_index ?? "?"} pid ${proc.pid} ${proc.process_name || "unknown"} | ${memory} | sm ${fmtPercent(proc.sm_pct)} | mem ${fmtPercent(proc.mem_pct)} | ${enc} | ${dec}`
      );
    });
  }

  const diagRoot = $("gpuDiagnostics");
  diagRoot.innerHTML = "";
  const lines = [...(data.diagnosis || []), ...(data.errors || [])];
  Object.entries(data.commands || {}).forEach(([name, result]) => {
    if (result.returncode && result.returncode !== 0) {
      lines.push(`${name}: ${result.stderr || result.stdout || `exit ${result.returncode}`}`);
    }
  });
  if (!lines.length) {
    const empty = document.createElement("div");
    empty.className = "emptyLine";
    empty.textContent = "No GPU diagnostics.";
    diagRoot.append(empty);
  } else {
    lines.slice(0, 12).forEach((line) => {
      const item = document.createElement("div");
      item.className = "logLine";
      item.textContent = line;
      diagRoot.append(item);
    });
  }
}

function setPlayerState(text) {
  setText("playerState", text);
}

function clearPlayRetry() {
  if (!state.playRetryTimer) return;
  clearTimeout(state.playRetryTimer);
  state.playRetryTimer = null;
}

function schedulePlayRetry(reason = "retry") {
  clearPlayRetry();
  const delay = Math.max(400, Math.min(5000, state.playRetryMs));
  state.playRetryTimer = setTimeout(() => {
    state.playRetryTimer = null;
    requestAutoplay(reason);
  }, delay);
  state.playRetryMs = Math.min(5000, Math.floor(state.playRetryMs * 1.5));
}

function requestAutoplay(reason = "autoplay") {
  if (!state.player || state.player.isDisposed() || !state.playerUrl) return;
  state.player.muted(true);
  state.player.volume(0);
  state.player.play().then(() => {
    state.playRetryMs = 800;
    setPlayerState("playing");
  }).catch(() => {
    setPlayerState(`${reason}: retrying`);
    schedulePlayRetry(reason);
  });
}

function setVideo(url, playerUrl = url) {
  setText("previewUrl", url || "Waiting for HLS output");
  $("openHlsBtn").href = absoluteUrl(playerUrl || url) || "#";
  if (!playerUrl || state.playerUrl === playerUrl) return;
  state.hlsUrl = url;
  state.playerUrl = playerUrl;
  initPlayer(playerUrl);
}

function clearVideo(url, message) {
  clearPlayRetry();
  setText("previewUrl", url || "Waiting for HLS output");
  $("openHlsBtn").href = absoluteUrl(url) || "#";
  setPlayerState(message || "waiting for stream");
  if (state.player && state.playerUrl) {
    state.player.pause();
    state.player.reset();
  }
  state.playerUrl = "";
}

function ensureVideoElement() {
  if ($("preview")) return $("preview");
  const video = document.createElement("video");
  video.id = "preview";
  video.className = "video-js vjs-theme-obby vjs-big-play-centered";
  video.setAttribute("controls", "");
  video.setAttribute("preload", "auto");
  video.setAttribute("playsinline", "");
  $("videoMount").replaceChildren(video);
  return video;
}

function initPlayer(url = state.playerUrl) {
  if (!url || !window.videojs) {
    if (!window.videojs) setPlayerState("Video.js unavailable");
    return;
  }
  ensureVideoElement();
  if (!state.player || state.player.isDisposed()) {
    state.player = videojs("preview", {
      fluid: true,
      liveui: true,
      controls: true,
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

    state.player.on("loadedmetadata", () => requestAutoplay("metadata"));
    state.player.on("playing", () => {
      state.playRetryMs = 800;
      clearPlayRetry();
      setPlayerState("playing");
    });
    state.player.on("pause", () => schedulePlayRetry("paused"));
    state.player.on("waiting", () => schedulePlayRetry("buffering"));
    state.player.on("stalled", () => schedulePlayRetry("stalled"));
    state.player.on("ended", () => schedulePlayRetry("ended"));
    state.player.on("error", () => {
      const err = state.player.error();
      setPlayerState(`error ${err?.code || ""} retrying`.trim());
      schedulePlayRetry("error");
    });
  }

  state.player.ready(() => {
    clearPlayRetry();
    state.playRetryMs = 800;
    setPlayerState("loading");
    state.player.muted(true);
    state.player.volume(0);
    state.player.pause();
    state.player.reset();
    state.player.src({ src: url, type: "application/x-mpegURL" });
    state.player.load();
    requestAutoplay("load");
  });
}

async function refresh() {
  try {
    const data = await api("/api/status");
    state.config = data.config;
    const stream = data.config.stream || {};
    const proc = data.managed_process || {};
    const hls = data.hls || {};
    const health = data.health || {};

    const runState = health.state || (proc.managed ? "running" : "stopped");
    setBadge("runState", runState, runState === "healthy" || proc.managed ? "ok" : "warn");
    setBadge("encoder", stream.encoder || "auto");
    renderEncoderMode(stream.encoder || "auto");
    setBadge("updated", new Date(data.server_time).toLocaleTimeString());
    setBadge("arango", "checking");

    const healthTone = health.level === "ok" ? "ok" : health.level === "bad" ? "bad" : "warn";
    setBadge("healthState", `${health.level || "warn"}: ${health.state || "unknown"}`, healthTone);
    setText("healthMessage", health.message || "Waiting for status.");

    setText("segments", String(hls.segments ?? 0));
    setText("playlistAge", fmtAge(hls.playlist_age));
    setText("rss", proc.rss ? fmtBytes(proc.rss) : "n/a");
    setText("pid", proc.pid || "n/a");
    setText("cpu", proc.cpu == null ? "n/a" : `${proc.cpu.toFixed(1)}%`);
    setText("hlsBytes", fmtBytes(hls.bytes));
    setText("healthScore", health.score == null ? "n/a" : health.score.toFixed ? health.score.toFixed(1) : String(health.score));
    setText("healthConfidence", health.confidence == null ? "n/a" : `${health.confidence}%`);
    setText("healthDecision", health.decision || "n/a");
    const remaining = health.assessment_remaining || 0;
    const elapsed = health.assessment_elapsed || 0;
    setText("assessmentWindow", remaining > 0 ? `${elapsed.toFixed(1)}s + ${remaining.toFixed(1)}s` : `${elapsed.toFixed(1)}s`);
    const evidence = health.evidence || {};
    const reasons = evidence.reasons || [];
    const dataPoints = [
      evidence.progress_seen ? "progress" : null,
      evidence.playlist_fresh ? "fresh playlist" : null,
      evidence.media_sequence_advanced ? "sequence moved" : null,
      evidence.bytes_delta ? `+${fmtBytes(evidence.bytes_delta)}` : null,
      evidence.segment_delta ? `+${evidence.segment_delta} segment` : null,
      reasons[0] || null,
    ].filter(Boolean);
    setText("healthEvidence", dataPoints.length ? dataPoints.join(" | ") : "Collecting evidence.");
    setText("externalProcs", String((data.existing_processes || []).length));
    setText("mediaSequence", hls.media_sequence || "n/a");
    setText("targetDuration", hls.target_duration ? `${hls.target_duration}s` : "n/a");
    setText("playlistLineCount", String(hls.playlist_line_count ?? "n/a"));
    setText("windowSeconds", hls.segment_window_seconds ? `${hls.segment_window_seconds.toFixed(1)}s` : "n/a");
    setText("playlistModified", fmtClock(hls.playlist_modified_at));
    setText("firstSegment", hls.first_segment || "n/a");
    setText("lastSegment", hls.last_segment || "n/a");
    setText("lastSegmentSize", hls.last_segment_size ? fmtBytes(hls.last_segment_size) : "n/a");
    setText("hlsRoute", hls.dashboard_hls_url || "/hls/ufc.m3u8");
    setText("publicHls", hls.public_hls_url || "n/a");

    if (proc.managed && hls.playlist_ready) {
      setVideo(hls.public_hls_url, hls.dashboard_hls_url || hls.public_hls_url);
    } else {
      clearVideo(hls.dashboard_hls_url || hls.public_hls_url, health.message);
    }

    renderLinks(stream.links || []);
    renderEvents(data.events || []);
    renderLogs(data.logs || []);
    renderErrors(data.errors || health.recent_errors || []);
    renderSegments(hls.playlist_segments || []);
    renderProcessLists(proc, data.existing_processes || []);

    const arango = await api("/api/arango").catch((err) => ({ connected: false, error: err.message }));
    setBadge("arango", arango.connected ? "connected" : "offline", arango.connected ? "ok" : "bad");
  } catch (err) {
    if (err.message === "unauthorized") {
      showLogin();
      return;
    }
    setText("sessionState", `error: ${err.message}`);
  }
}

async function refreshGpuTelemetry() {
  try {
    const data = await api("/api/nvidia-smi");
    renderGpuTelemetry(data);
  } catch (err) {
    setBadge("gpuState", "error", "bad");
    setText("gpuMessage", `GPU telemetry error: ${err.message}`);
    setText("gpuUpdated", "5s");
    const diagRoot = $("gpuDiagnostics");
    if (diagRoot) {
      diagRoot.innerHTML = "";
      const item = document.createElement("div");
      item.className = "logLine error";
      item.textContent = err.message;
      diagRoot.append(item);
    }
  }
}

async function addLink(event) {
  event.preventDefault();
  const url = $("newLink").value.trim();
  if (!url) return;
  await api("/api/links", { method: "POST", body: JSON.stringify({ url }) });
  $("newLink").value = "";
  await refresh();
}

async function saveLinks() {
  await api("/api/config", { method: "PUT", body: JSON.stringify({ links: state.links }) });
  await refresh();
}

async function setEncoderMode(encoder) {
  setText("encoderMode", `${encoderLabel(encoder)}...`);
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify({ encoder }) });
    await refresh();
  } catch (err) {
    setText("sessionState", `encoder error: ${err.message}`);
    await refresh();
  }
}

async function streamAction(action) {
  await api(`/api/stream/${action}`, { method: "POST", body: JSON.stringify({ kill_existing: true }) });
  await refresh();
}

$("loginForm").addEventListener("submit", login);
$("addLinkForm").addEventListener("submit", addLink);
$("saveLinksBtn").addEventListener("click", saveLinks);
$("autoEncoderBtn").addEventListener("click", () => setEncoderMode("auto"));
$("gpuOnlyBtn").addEventListener("click", () => setEncoderMode("gpu-only"));
$("cpuOnlyBtn").addEventListener("click", () => setEncoderMode("cpu"));
$("startBtn").addEventListener("click", () => streamAction("start"));
$("restartBtn").addEventListener("click", () => streamAction("restart"));
$("stopBtn").addEventListener("click", () => streamAction("stop"));
$("reloadPlayerBtn").addEventListener("click", () => initPlayer());
$("copyHlsBtn").addEventListener("click", async () => {
  const url = absoluteUrl(state.playerUrl || state.hlsUrl);
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    setPlayerState("HLS URL copied");
  } catch (_) {
    window.prompt("Copy HLS URL:", url);
  }
});

if (state.token) {
  showApp();
  refresh();
  refreshGpuTelemetry();
} else {
  showLogin();
}

setInterval(() => {
  if (!$("app").classList.contains("hidden")) refresh();
}, 2500);

setInterval(() => {
  if (!$("app").classList.contains("hidden")) refreshGpuTelemetry();
}, 5000);

setInterval(() => {
  if ($("app").classList.contains("hidden")) return;
  if (!state.player || state.player.isDisposed() || !state.playerUrl) return;
  state.player.muted(true);
  state.player.volume(0);
  if (state.player.paused()) requestAutoplay("heartbeat");
}, 3000);
