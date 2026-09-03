import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Builds the shareable single-file preview.
 *
 * Only the API transport is swapped — every page, component and stylesheet is
 * the production one, so the preview cannot drift into being a mockup of the
 * app rather than the app itself.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [{ find: /^\.\.\/lib\/api$/, replacement: path.resolve(__dirname, "demo/api.ts") }],
  },
  build: {
    outDir: "dist-demo",
    // One JS chunk and one CSS file, so inlining into a single page is trivial.
    assetsInlineLimit: 100_000_000,
    cssCodeSplit: false,
    rollupOptions: {
      input: path.resolve(__dirname, "demo/index.html"),
      output: { inlineDynamicImports: true, entryFileNames: "app.js", assetFileNames: "app[extname]" },
    },
  },
});
