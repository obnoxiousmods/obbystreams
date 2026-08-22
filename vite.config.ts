import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  root: "frontend",
  base: "/static/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../static",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1000,
    // Split the two large, rarely-changing dependencies into their own chunks.
    // Not lazy-loaded: the player is the primary above-the-fold content of a
    // stream cockpit, so deferring it would make the one thing the operator came
    // for arrive last. Vite emits modulepreload for all chunks, so first paint is
    // unchanged — the win is that a CSS/JSX-only redeploy no longer invalidates
    // the ~600 KB video.js chunk in anyone's cache.
    rollupOptions: {
      output: {
        manualChunks: {
          videojs: ["video.js"],
          react: ["react", "react-dom", "react-dom/client"],
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8767",
      "/hls": "http://127.0.0.1:8767",
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
  },
});
