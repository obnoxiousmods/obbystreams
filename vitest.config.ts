/// <reference types="vitest/config" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Vitest config for the cockpit frontend. Kept separate from vite.config.ts so
// the production build stays free of test concerns. Tests live in
// frontend/src/**/*.test.{ts,tsx}; component tests render with jsdom.
export default defineConfig({
  plugins: [react()],
  test: {
    root: "frontend",
    environment: "jsdom",
    globals: false,
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
