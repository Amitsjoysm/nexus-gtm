import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// NEXUS frontend build.
// - Dev server proxies /api to the FastAPI backend so the SPA runs against real data.
// - Production build emits to ../nexus/web/dist, which FastAPI serves as static files.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5001,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:9000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: fileURLToPath(new URL("../nexus/web/dist", import.meta.url)),
    emptyOutDir: true,
    // "hidden" still emits maps for error-reporting tools but omits the
    // sourceMappingURL comment, so we don't ship a map link to every visitor.
    sourcemap: "hidden",
    rollupOptions: {
      output: {
        // Split rarely-changing vendor code into its own long-cache chunk.
        // React + router move together (router depends on React); framer-motion
        // is large and independent, so it gets its own chunk that only loads
        // once any animated surface mounts.
        manualChunks: {
          "react-vendor": ["react", "react-dom", "react-router-dom"],
          motion: ["framer-motion"],
        },
      },
    },
  },
});
