/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  test: {
    // e2e/ holds Playwright specs (`npx playwright test`), run by a
    // completely separate runner (playwright.config.ts) — vitest's default
    // glob would otherwise pick up the same *.spec.ts files and try (and
    // fail) to run them itself.
    exclude: ["**/node_modules/**", "e2e/**"],
  },
});
