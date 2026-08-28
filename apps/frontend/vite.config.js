import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The two backends are separate trust boundaries and stay separate here too.
// `/obs` is the detector's public read API; `/ctl` is the injector. Nothing in
// the browser blurs them.
const observability = process.env.OBSERVABILITY_URL || "http://observability-service:8004";
const controller = process.env.SCENARIO_CONTROLLER_URL || "http://scenario-controller:8003";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    // Reached as `localhost` from a browser on the host and as `frontend` from
    // inside the compose network (the Playwright screenshot run). Vite blocks
    // unknown Host headers by default.
    allowedHosts: ["localhost", "127.0.0.1", "frontend"],
    proxy: {
      "/obs": { target: observability, changeOrigin: true, rewrite: (p) => p.replace(/^\/obs/, "") },
      "/ctl": { target: controller, changeOrigin: true, rewrite: (p) => p.replace(/^\/ctl/, "") },
    },
  },
});
