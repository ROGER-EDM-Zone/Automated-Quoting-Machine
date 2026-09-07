import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The API runs alongside in development; same-origin keeps auth simple.
    // No rewrite: the backend serves the API under /api in every environment,
    // so development and the installed app address it identically.
    proxy: { "/api": { target: "http://localhost:8000", changeOrigin: true } },
  },
});
