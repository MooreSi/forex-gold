// `defineConfig` comes from vitest/config, not vite: since vitest 4 the
// `test` block below is no longer part of vite's own config type, and
// importing it from "vite" fails the typecheck with TS2769.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// The bundle is committed and served by the Python process (see
// docs/system/domains/frontend/010-the-react-decision.md), so `base` stays "/"
// and the output goes to frontend/dist where backend/src/api/server.py mounts
// it. Changing either without changing the other produces a dashboard that
// 404s every asset.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Asset names are content-hashed, so a stale index.html cannot silently
    // load a new bundle or vice versa.
    sourcemap: false,
  },
  server: {
    port: 5173,
    // `npm run dev` talks to the real Python app on 8888. Without this every
    // fetch during development hits Vite and 404s.
    proxy: {
      "/api": { target: "http://localhost:8888", changeOrigin: true },
      "/static": { target: "http://localhost:8888", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
