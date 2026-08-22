import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";

const TOKEN_KEY = "obbystreams_token";
const port = Number(process.env.OBBY_RESPONSIVE_PORT || 5179);
const baseUrl = `http://127.0.0.1:${port}`;
const screenshotDir = process.env.OBBY_RESPONSIVE_SCREENSHOTS || "artifacts/responsive";

const viewports = [
  { name: "iphone-se", width: 320, height: 568, mobile: true },
  { name: "small-android", width: 360, height: 740, mobile: true },
  { name: "iphone-15", width: 393, height: 852, mobile: true },
  { name: "large-phone", width: 430, height: 932, mobile: true },
  { name: "tablet-portrait", width: 768, height: 1024, mobile: true },
  { name: "tablet-landscape", width: 1024, height: 768, mobile: false },
  { name: "laptop", width: 1366, height: 768, mobile: false },
  { name: "desktop", width: 1440, height: 900, mobile: false },
  { name: "wide-desktop", width: 1920, height: 1080, mobile: false },
  { name: "wqhd", width: 2560, height: 1440, mobile: false },
  { name: "ultrawide", width: 3440, height: 1440, mobile: false },
];

// Full-page height ceiling per viewport width. The cockpit was 5405px at 1920 and
// 10534px before the layout overhaul; these are the post-overhaul numbers with a
// little headroom. Ratchet them DOWN as the layout tightens — a regression that
// re-inflates the page is exactly what this catches.
const HEIGHT_BUDGET = {
  320: 6300,
  360: 6000,
  393: 5500,
  430: 5600,
  768: 5000,
  1024: 4400,
  1366: 4200,
  1440: 4200,
  1920: 2800,
  2560: 2900,
  3440: 2900,
};

// A side-by-side child that stops more than this far above the bottom of its
// region is dead column space — the defect that left ~2000px blank beside a
// short player and squeezed telemetry into a 615px column.
const DEAD_SPACE_LIMIT = 900;

function systemChromiumPath() {
  const candidates = [
    process.env.PLAYWRIGHT_CHROMIUM,
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate));
}

function startVite() {
  const child = spawn("npm", ["run", "dev", "--", "--port", String(port), "--strictPort"], {
    cwd: process.cwd(),
    env: { ...process.env, BROWSER: "none" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (chunk) => {
    output += chunk.toString();
  });
  child.stderr.on("data", (chunk) => {
    output += chunk.toString();
  });
  return { child, getOutput: () => output };
}

async function waitForServer(getOutput) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(baseUrl);
      if (response.ok) return;
    } catch {
      // Vite is still booting.
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`Vite did not start at ${baseUrl}\n${getOutput()}`);
}

function json(route, payload) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

function mockStatus() {
  const now = Date.now();
  return {
    ok: true,
    server_time: now,
    config: {
      stream: {
        encoder: "gpu-only",
        links: [
          "https://edge-a.example.net/live/events/obbystreams-primary/index.m3u8?token=mobile-layout-proof",
          "https://backup.example.net/live/secondary/playlist.m3u8",
          "https://relay.example.net/hls/third-feed-with-a-long-readable-name/index.m3u8",
        ],
      },
    },
    // Without these the source cards never render, so the fixture never exercised
    // the badge row, the truncating URL chip, or the per-source More menu — the
    // densest and most breakage-prone part of the narrow layout.
    sources: [
      {
        id: "source-primary",
        label: "PPV EVENT 03: UFC FN Long Card Name vs Other Fighter (8.8 8:00 PM ET)",
        url: "https://edge-a.example.net/live/events/obbystreams-primary/index.m3u8?token=mobile-layout-proof",
        type: "soursignal",
        health: "green",
        health_message: "Source page reachable",
        preferred: true,
        locked: false,
        viewer_count: 12,
      },
      {
        id: "source-backup",
        label: "LIVE EVENT 01 5pm Backup Feed",
        url: "https://backup.example.net/live/secondary/playlist.m3u8",
        type: "hls",
        health: "yellow",
        health_message: "Source page did not respond",
        preferred: false,
        locked: true,
        viewer_count: 0,
      },
    ],
    managed_process: {
      managed: true,
      pid: 1842,
      started_at: now - 640000,
      age: 640,
      cpu: 12.8,
      rss: 644245094,
      cmd: "ffmpeg -hide_banner -re -i https://edge-a.example.net/live/events/obbystreams-primary/index.m3u8 -c:v h264_nvenc -f hls /var/lib/obbystreams/hls/ufc.m3u8",
      children: [
        { pid: 1843, name: "ffmpeg", cpu: 12.4, rss: 603979776 },
        { pid: 1844, name: "hls-writer", cpu: 0.4, rss: 41943040 },
      ],
    },
    existing_processes: [
      {
        pid: 1990,
        age: 121,
        cmd: "ffmpeg monitor process with a very long command line that should wrap cleanly on narrow screens",
      },
    ],
    hls: {
      output_dir: "/var/lib/obbystreams/hls",
      playlist: "/hls/ufc.m3u8",
      playlist_exists: true,
      playlist_ready: true,
      playlist_age: 0.7,
      playlist_modified_at: now - 900,
      playlist_line_count: 18,
      segments: 9,
      bytes: 2147483648,
      latest_segment_modified_at: now - 900,
      oldest_segment_modified_at: now - 36000,
      target_duration: 4,
      media_sequence: 58142,
      segment_window_seconds: 36.2,
      playlist_segment_count: 9,
      playlist_segments: Array.from({ length: 16 }, (_, index) => `segment-${58126 + index}-camera-main-very-long-name.ts`),
      first_segment: "segment-58134-camera-main.ts",
      last_segment: "segment-58142-camera-main.ts",
      last_segment_size: 7340032,
      public_hls_url: "https://s.obby.ca/hls/ufc.m3u8",
      dashboard_hls_url: "/hls/ufc.m3u8",
    },
    health: {
      state: "running",
      level: "ok",
      decision: "healthy",
      message: "Playlist is fresh, segments are advancing, and managed ffmpeg is visible.",
      score: 97.4,
      confidence: 96,
      assessment_elapsed: 42.5,
      assessment_remaining: 0,
      consecutive_bad_samples: 0,
      consecutive_good_samples: 14,
      evidence: {
        has_child: true,
        playlist_exists: true,
        playlist_ready: true,
        playlist_fresh: true,
        playlist_age: 0.7,
        segment_delta: 3,
        bytes_delta: 12451840,
        playlist_moved: true,
        media_sequence_advanced: true,
        progress_seen: true,
        recent_error_count: 1,
        ramp: 1,
        reasons: ["steady hls output with fresh segment timestamps"],
      },
      samples: Array.from({ length: 18 }, (_, index) => ({
        ts: now - (18 - index) * 2500,
        score: 86 + index,
        decision: "healthy",
      })),
      recent_errors: [
        {
          ts: now - 12000,
          level: "warn",
          line: "Non-fatal timestamp discontinuity recovered after playlist refresh.",
        },
      ],
    },
    events: [
      { ts: now - 30000, level: "info", message: "Stream health confirmed after mobile viewport test fixture." },
      { ts: now - 20000, level: "debug", message: "HLS media sequence advanced by three segments." },
      { ts: now - 10000, level: "warn", message: "Recovered from stale probe without operator action." },
    ],
    logs: Array.from({ length: 18 }, (_, index) => ({
      ts: now - index * 1400,
      level: index % 5 === 0 ? "debug" : "info",
      line: `ffmpeg progress frame=${21940 + index} fps=59.9 bitrate=6200kbits/s speed=1.00x segment=segment-${58140 + index}.ts`,
    })),
    errors: [
      {
        ts: now - 12000,
        level: "warn",
        line: "Non-fatal timestamp discontinuity recovered after playlist refresh.",
      },
    ],
  };
}

function mockGpu() {
  return {
    ok: true,
    available: true,
    level: "ok",
    checked_at: Date.now(),
    message: "NVIDIA telemetry is stable.",
    diagnosis: [],
    errors: [],
    commands: {},
    summary: {
      gpu_count: 1,
      driver_version: "555.42.02",
      max_temperature_c: 61,
      max_gpu_utilization_pct: 43,
      max_memory_used_pct: 38,
      power_draw_w: 146.8,
      power_limit_w: 240,
      encoder_session_count: 1,
      encoder_utilization_pct: 28,
      process_count: 2,
      ffmpeg_process_count: 1,
      stream_gpu_active: true,
    },
    gpus: [
      {
        index: 0,
        name: "NVIDIA RTX Layout Fixture",
        uuid: "GPU-responsive-fixture",
        pstate: "P2",
        temperature_c: 61,
        gpu_utilization_pct: 43,
        memory_total_mb: 24576,
        memory_used_mb: 9344,
        memory_used_pct: 38,
        power_draw_w: 146.8,
      },
    ],
    processes: [
      {
        gpu_index: 0,
        pid: 1843,
        process_name: "ffmpeg",
        used_memory_mb: 840,
        sm_pct: 12,
        mem_pct: 8,
        enc_pct: 28,
        dec_pct: 0,
        is_ffmpeg: true,
      },
    ],
  };
}

async function installRoutes(page) {
  await page.route("**/api/status", (route) => json(route, mockStatus()));
  await page.route("**/api/arango", (route) => json(route, { ok: true, connected: true, version: "3.12.0" }));
  await page.route("**/api/nvidia-smi", (route) => json(route, mockGpu()));
  await page.route("**/api/auth/login", (route) => json(route, { ok: true, token: "responsive-token" }));
  await page.route("**/api/config", (route) => json(route, mockStatus()));
  await page.route("**/api/links", (route) => json(route, { ok: true }));
  await page.route("**/api/links/remove", (route) => json(route, { ok: true }));
  await page.route("**/api/stream/**", (route) => json(route, { ok: true }));
  await page.route("**/hls/*.m3u8", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/vnd.apple.mpegurl",
      body: "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:4.0,\n/hls/segment-1.ts\n",
    }),
  );
  await page.route("**/hls/*.ts", (route) => route.fulfill({ status: 200, contentType: "video/mp2t", body: "" }));
}

async function assertDropdownWithinViewport(page, selector, viewportName) {
  const button = page.locator(selector).first();
  await button.scrollIntoViewIfNeeded();
  await button.click();
  const panel = page.locator(".dropdownPanel").last();
  await panel.waitFor({ state: "visible", timeout: 5000 });
  const rect = await panel.boundingBox();
  const viewport = page.viewportSize();
  if (!rect || !viewport) throw new Error(`${viewportName}: ${selector} dropdown has no layout box`);
  const outsideX = rect.x < -1 || rect.x + rect.width > viewport.width + 1;
  const outsideY = rect.y < -1 || rect.y + Math.min(rect.height, viewport.height) > viewport.height + 1;
  if (outsideX || outsideY) {
    const computed = await panel.evaluate((element) => {
      const style = window.getComputedStyle(element);
      return {
        position: style.position,
        top: style.top,
        right: style.right,
        bottom: style.bottom,
        left: style.left,
        width: style.width,
        maxHeight: style.maxHeight,
      };
    });
    throw new Error(
      `${viewportName}: ${selector} dropdown escapes viewport (${Math.round(rect.x)},${Math.round(rect.y)},${Math.round(rect.width)}x${Math.round(rect.height)}) in ${viewport.width}x${viewport.height}; computed ${JSON.stringify(computed)}`,
    );
  }
  await page.keyboard.press("Escape");
}

async function collectLayoutIssues(page) {
  return page.evaluate(() => {
    const viewportWidth = document.documentElement.clientWidth;
    const overflow = document.documentElement.scrollWidth - viewportWidth;
    const selectors = [
      "main.appShell",
      ".commandHeader",
      ".statusStrip",
      ".stagePanel",
      ".videoSurface",
      ".playerBottomRail",
      ".stageRow",
      ".rightRail",
      ".sourcesRow",
      ".telemetryRow",
      ".footerStatus",
      ".panel",
      ".metricTile",
      ".linkItem",
      ".feedBox",
    ];
    const offenders = [];
    for (const selector of selectors) {
      for (const element of document.querySelectorAll(selector)) {
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        if (style.display === "none" || rect.width === 0 || rect.height === 0) continue;
        if (rect.left < -1 || rect.right > viewportWidth + 1) {
          offenders.push({
            selector,
            left: Math.round(rect.left),
            right: Math.round(rect.right),
            width: Math.round(rect.width),
            viewportWidth,
          });
        }
      }
    }
    const smallTargets = [];
    for (const element of document.querySelectorAll("button, a.buttonLink, input")) {
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      if (style.display === "none" || style.visibility === "hidden" || rect.width === 0 || rect.height === 0) continue;
      // A control wrapped in a label is tapped via the label, so the label is the
      // real target. Applies to sliders and to checkboxes like Auto-schedule,
      // whose native box is intentionally small next to its text.
      if (element instanceof HTMLInputElement && (element.type === "range" || element.type === "checkbox")) {
        const label = element.closest("label");
        const labelRect = label?.getBoundingClientRect();
        if (labelRect && labelRect.width >= 34 && labelRect.height >= 34) continue;
      }
      if (rect.height < 34 || rect.width < 34) {
        smallTargets.push({
          tag: element.tagName.toLowerCase(),
          text: element.textContent?.trim().slice(0, 40) || element.getAttribute("aria-label") || element.getAttribute("placeholder") || "",
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        });
      }
    }
    // Dead column space, measured PER GRID ROW: group the region's children by
    // their top offset, then within each row report how far above the row's
    // deepest child each sibling stops. Comparing against the whole region's
    // bottom instead would count the next row's height as dead space.
    const deadSpace = [];
    for (const region of document.querySelectorAll(".stageRow")) {
      const rows = new Map();
      for (const el of region.children) {
        const rect = el.getBoundingClientRect();
        if (rect.height === 0) continue;
        const key = Math.round(rect.top);
        if (!rows.has(key)) rows.set(key, []);
        rows.get(key).push({ el, rect });
      }
      for (const members of rows.values()) {
        if (members.length < 2) continue; // a lone child in its row cannot strand a sibling
        const rowBottom = Math.max(...members.map((m) => m.rect.bottom));
        for (const { el, rect } of members) {
          const tail = Math.round(rowBottom - rect.bottom);
          if (tail > 0) {
            deadSpace.push({ region: region.className, child: el.className.toString().slice(0, 40), tail });
          }
        }
      }
    }

    return {
      overflow,
      offenders,
      smallTargets,
      pageHeight: Math.round(document.documentElement.scrollHeight),
      deadSpace,
    };
  });
}

async function checkViewport(browser, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
    deviceScaleFactor: viewport.mobile ? 2 : 1,
    isMobile: viewport.mobile,
    hasTouch: viewport.mobile,
  });
  const page = await context.newPage();

  // Nothing may leave this machine. Fonts are self-hosted and vendored into the
  // repo precisely so the cockpit never depends on a font CDN; this is the guard
  // that keeps it that way.
  const externalRequests = [];
  page.on("request", (request) => {
    let url;
    try {
      url = new URL(request.url());
    } catch {
      return;
    }
    if (url.protocol === "data:" || url.protocol === "blob:") return;
    if (url.hostname === "127.0.0.1" || url.hostname === "localhost" || url.hostname === "::1") return;
    externalRequests.push(request.url());
  });

  await page.addInitScript((key) => window.localStorage.setItem(key, "responsive-token"), TOKEN_KEY);
  await installRoutes(page);
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.locator(".appShell").waitFor({ timeout: 10000 });
  await page.waitForTimeout(250);

  const layout = await collectLayoutIssues(page);
  if (layout.overflow > 1 || layout.offenders.length || layout.smallTargets.length) {
    throw new Error(`${viewport.name}: layout issues ${JSON.stringify(layout, null, 2)}`);
  }

  const budget = HEIGHT_BUDGET[viewport.width];
  if (budget && layout.pageHeight > budget) {
    throw new Error(`${viewport.name}: page is ${layout.pageHeight}px tall, budget is ${budget}px`);
  }

  const dead = layout.deadSpace.filter((entry) => entry.tail > DEAD_SPACE_LIMIT);
  if (dead.length) {
    throw new Error(`${viewport.name}: dead column space ${JSON.stringify(dead, null, 2)}`);
  }

  if (externalRequests.length) {
    throw new Error(`${viewport.name}: external requests ${JSON.stringify([...new Set(externalRequests)], null, 2)}`);
  }

  await assertDropdownWithinViewport(page, ".encoderDropdown .dropdownButton", viewport.name);
  // Was ".playerMenu .dropdownButton", a selector no component has ever rendered
  // (the CSS has orphan .playerMenu rules; the JSX uses .playerMoreMenu, which is
  // a hand-rolled popover, not a ModernDropdown). The per-source More menu is a
  // real second case, and a useful one: it sits inside the multicol source row
  // near the right viewport edge.
  await assertDropdownWithinViewport(page, ".sourceMenu .dropdownButton", viewport.name);

  const screenshotPath = join(screenshotDir, `${viewport.name}-${viewport.width}x${viewport.height}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await context.close();
  return { screenshotPath, pageHeight: layout.pageHeight };
}

const server = startVite();
let browser;

try {
  await mkdir(screenshotDir, { recursive: true });
  await waitForServer(server.getOutput);
  const executablePath = systemChromiumPath();
  browser = await chromium.launch({
    headless: true,
    executablePath,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  for (const viewport of viewports) {
    const { screenshotPath, pageHeight } = await checkViewport(browser, viewport);
    const budget = HEIGHT_BUDGET[viewport.width];
    const budgetNote = budget ? ` height=${pageHeight}/${budget}` : ` height=${pageHeight}`;
    console.log(`ok ${viewport.name} ${viewport.width}x${viewport.height}${budgetNote} -> ${screenshotPath}`);
  }
} finally {
  if (browser) await browser.close();
  server.child.kill("SIGTERM");
}
