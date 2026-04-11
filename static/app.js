const state = {
  token: localStorage.getItem("obbystreams_token") || "",
  config: null,
  links: [],
  player: null,
  hlsUrl: "",
  playerUrl: "",
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

function absoluteUrl(url) {
  if (!url) return "";
  return new URL(url, window.location.origin).toString();
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
}

function showLogin() {
  $("login").classList.remove("hidden");
  $("app").classList.add("hidden");
}

async function login(event) {
  event.preventDefault();
  $("loginError").textContent = "";
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: $("password").value }),
    });
    state.token = data.token;
    localStorage.setItem("obbystreams_token", state.token);
    showApp();
    await refresh();
  } catch (err) {
    $("loginError").textContent = err.message;
  }
}

function renderLinks(links) {
  state.links = [...links];
  $("links").innerHTML = "";
  state.links.forEach((url, index) => {
    const item = document.createElement("div");
    item.className = "linkItem";
    item.innerHTML = `<code>${url}</code>`;
    const controls = document.createElement("div");
    controls.className = "actions";
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
    item.append(controls);
    $("links").append(item);
  });
}

function renderEvents(events) {
  $("events").innerHTML = "";
  events.slice(-16).reverse().forEach((event) => {
    const item = document.createElement("div");
    item.className = "feedItem";
    item.innerHTML = `<strong>${event.level}</strong> <span>${new Date(event.ts).toLocaleTimeString()}</span><br>${event.message}`;
    $("events").append(item);
  });
}

function renderLogs(logs) {
  $("logs").innerHTML = "";
  logs.slice(-24).reverse().forEach((log) => {
    const item = document.createElement("div");
    item.className = "logLine";
    item.textContent = log.line || JSON.stringify(log);
    $("logs").append(item);
  });
}

function renderSegments(segments) {
  $("playlistSegments").innerHTML = "";
  (segments || []).slice(-12).reverse().forEach((name) => {
    const item = document.createElement("div");
    item.className = "segmentLine";
    item.textContent = name;
    $("playlistSegments").append(item);
  });
}

function setPlayerState(text) {
  $("playerState").textContent = text;
}

function setVideo(url, playerUrl = url) {
  $("previewUrl").textContent = url || "Waiting for HLS output";
  $("openHlsBtn").href = absoluteUrl(playerUrl || url) || "#";
  if (!playerUrl || state.playerUrl === playerUrl) return;
  state.hlsUrl = url;
  state.playerUrl = playerUrl;
  initPlayer(playerUrl);
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

    state.player.on("loadedmetadata", () => setPlayerState("metadata loaded"));
    state.player.on("playing", () => setPlayerState("playing"));
    state.player.on("waiting", () => setPlayerState("buffering"));
    state.player.on("stalled", () => setPlayerState("stalled"));
    state.player.on("error", () => {
      const err = state.player.error();
      setPlayerState(`error ${err?.code || ""}`.trim());
      console.error("Video.js error", err);
    });
  }

  state.player.ready(() => {
    setPlayerState("loading");
    state.player.pause();
    state.player.reset();
    state.player.src({ src: url, type: "application/x-mpegURL" });
    state.player.load();
    state.player.play().catch(() => setPlayerState("click play to start"));
  });
}

async function refresh() {
  try {
    const data = await api("/api/status");
    state.config = data.config;
    const stream = data.config.stream || {};
    const proc = data.managed_process || {};
    const hls = data.hls || {};

    $("runState").textContent = proc.managed ? "Running" : "Stopped";
    $("encoder").textContent = stream.encoder || "auto";
    $("segments").textContent = hls.segments ?? 0;
    $("playlistAge").textContent = fmtAge(hls.playlist_age);
    $("rss").textContent = proc.rss ? fmtBytes(proc.rss) : "n/a";
    $("pid").textContent = proc.pid || "n/a";
    $("cpu").textContent = proc.cpu == null ? "n/a" : `${proc.cpu.toFixed(1)}%`;
    $("hlsBytes").textContent = fmtBytes(hls.bytes);
    $("externalProcs").textContent = (data.existing_processes || []).length;
    $("mediaSequence").textContent = hls.media_sequence || "n/a";
    $("targetDuration").textContent = hls.target_duration ? `${hls.target_duration}s` : "n/a";
    $("playlistLineCount").textContent = hls.playlist_line_count ?? "n/a";
    $("windowSeconds").textContent = hls.segment_window_seconds ? `${hls.segment_window_seconds.toFixed(1)}s` : "n/a";
    $("playlistModified").textContent = fmtClock(hls.playlist_modified_at);
    $("firstSegment").textContent = hls.first_segment || "n/a";
    $("lastSegment").textContent = hls.last_segment || "n/a";
    $("lastSegmentSize").textContent = hls.last_segment_size ? fmtBytes(hls.last_segment_size) : "n/a";
    $("updated").textContent = new Date(data.server_time).toLocaleTimeString();
    setVideo(hls.public_hls_url, hls.dashboard_hls_url || hls.public_hls_url);
    renderLinks(stream.links || []);
    renderEvents(data.events || []);
    renderLogs(data.logs || []);
    renderSegments(hls.playlist_segments || []);

    const arango = await api("/api/arango").catch((err) => ({ connected: false, error: err.message }));
    $("arango").textContent = arango.connected ? "Connected" : "Offline";
  } catch (err) {
    if (err.message === "unauthorized") showLogin();
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

async function streamAction(action) {
  await api(`/api/stream/${action}`, { method: "POST", body: JSON.stringify({ kill_existing: true }) });
  await refresh();
}

$("loginForm").addEventListener("submit", login);
$("addLinkForm").addEventListener("submit", addLink);
$("saveLinksBtn").addEventListener("click", saveLinks);
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
} else {
  showLogin();
}

setInterval(() => {
  if (!$("app").classList.contains("hidden")) refresh();
}, 2500);
