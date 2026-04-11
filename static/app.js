const state = {
  token: localStorage.getItem("obbystreams_token") || "",
  config: null,
  links: [],
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

function setVideo(url) {
  $("previewUrl").textContent = url || "Waiting for HLS output";
  if (!url || $("preview").src === url) return;
  $("preview").src = url;
  $("preview").load();
  $("preview").play().catch(() => {});
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
    $("updated").textContent = new Date(data.server_time).toLocaleTimeString();
    setVideo(hls.public_hls_url);
    renderLinks(stream.links || []);
    renderEvents(data.events || []);
    renderLogs(data.logs || []);

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

if (state.token) {
  showApp();
  refresh();
} else {
  showLogin();
}

setInterval(() => {
  if (!$("app").classList.contains("hidden")) refresh();
}, 2500);
