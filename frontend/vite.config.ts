import path from "path";
import { fileURLToPath } from "url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), viteSingleFile()],
  // Dev: the app talks to /api like in Docker, so cookies, media URLs and SSE behave the same in both.
  server: {
    proxy: { "/api": { target: process.env.VITE_DEV_API ?? "http://localhost:8000", rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
