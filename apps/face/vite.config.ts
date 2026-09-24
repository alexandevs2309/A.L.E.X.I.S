import { defineConfig } from "vite";

export default defineConfig({
  base: "/face/",
  build: {
    outDir: "dist",
    target: "es2020",
    chunkSizeWarningLimit: 1200,
  },
});