import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Proxying /api keeps the browser same-origin against api.py, so the FastAPI
// layer needs no CORS middleware and the client never hardcodes a backend host.
// Port 8000 is what api.py's docstring tells you to run; override with
// PF_API_PORT when something else already owns it.
export default defineConfig(({ mode }) => {
  const apiPort = loadEnv(mode, process.cwd(), "PF_").PF_API_PORT ?? "8000";
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: `http://localhost:${apiPort}`,
          changeOrigin: true,
        },
      },
    },
  };
});
