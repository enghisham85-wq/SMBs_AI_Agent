/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Node globals without pulling @types/node into the app build.
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [react()],
  // The vendored Boosthis kit is source-only; its package.json uses pnpm "catalog:" specs npm cannot install.
  resolve: { alias: { "@workspace/boosthis-runtime-web": "/lib/boosthis-runtime-web/src/index.ts" } },
  server: {
    port: Number(process.env.VITE_PORT ?? 5173),
    // Fail instead of drifting to another port: the e2e suite and the proxy expect this exact one.
    strictPort: true,
    // The e2e suite points the proxy at its own seeded backend (playwright.config.ts).
    proxy: { "/api": { target: process.env.API_TARGET ?? "http://localhost:8000", changeOrigin: true } },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/unit/setup.ts"],
    include: ["tests/unit/**/*.test.{ts,tsx}"],
  },
});
