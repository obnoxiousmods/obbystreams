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
];

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
      ".primaryGrid",
      ".rightRail",
      ".lowerGrid",
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
      if (element instanceof HTMLInputElement && element.type === "range") {
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
    return { overflow, offenders, smallTargets };
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
  await page.addInitScript((key) => window.localStorage.setItem(key, "responsive-token"), TOKEN_KEY);
  await installRoutes(page);
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.locator(".appShell").waitFor({ timeout: 10000 });
  await page.waitForTimeout(250);

  const layout = await collectLayoutIssues(page);
  if (layout.overflow > 1 || layout.offenders.length || layout.smallTargets.length) {
    throw new Error(`${viewport.name}: layout issues ${JSON.stringify(layout, null, 2)}`);
  }

  await assertDropdownWithinViewport(page, ".encoderDropdown .dropdownButton", viewport.name);
  await assertDropdownWithinViewport(page, ".playerMenu .dropdownButton", viewport.name);

  const screenshotPath = join(screenshotDir, `${viewport.name}-${viewport.width}x${viewport.height}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await context.close();
  return screenshotPath;
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
    const screenshotPath = await checkViewport(browser, viewport);
    console.log(`ok ${viewport.name} ${viewport.width}x${viewport.height} -> ${screenshotPath}`);
  }
} finally {
  if (browser) await browser.close();
  server.child.kill("SIGTERM");
}
