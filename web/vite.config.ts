import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs separately: uv run python -m cva.web votes.db
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/pdf": "http://127.0.0.1:8000",
      "/download-db": "http://127.0.0.1:8000",
    },
  },
});
