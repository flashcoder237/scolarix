import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

// Pages chargées en file:// par Electron : chemins relatifs.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "chrome130",
    chunkSizeWarningLimit: 3000,
    rollupOptions: { input: { index: resolve(__dirname, "index.html"), print: resolve(__dirname, "print.html") } },
  },
});
