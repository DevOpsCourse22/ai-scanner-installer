import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` talks to an installer API running on :8088 (uvicorn installer.app:app --port 8088)
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8088" } },
});
