import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies the API, so the browser talks to one origin (no CORS needed).
//   AJE_API_URL=http://localhost:8000 npm run dev
const target = process.env.AJE_API_URL || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": { target, changeOrigin: true },
      "/internal": { target, changeOrigin: true },
    },
  },
});
