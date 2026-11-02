import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// `npm run dev` serves the UI on :5173 and forwards API and MCP calls to
// `brain start` on :8765. `npm run build` writes into the Python package, so
// the wheel ships the built UI and Node is needed only at build time.
const backend = process.env.BRAIN_URL ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/brain/web/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/mcp": { target: backend, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
