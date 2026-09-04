/**
 * Fold the built preview into one self-contained HTML file.
 *
 * The output has no external references at all, so it can be emailed, dropped
 * on a share, or published as-is and still render. Two variants are written:
 * a standalone page, and a fragment for hosts that supply their own document
 * shell.
 *
 * Run with: npm run build:demo
 */
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const dist = resolve(here, "../dist-demo");

const css = readFileSync(resolve(dist, "app.css"), "utf8");
const js = readFileSync(resolve(dist, "app.js"), "utf8");

// A literal </script> inside the bundle would close the tag early.
const safeJs = js.replace(/<\/script>/gi, "<\\/script>");

// Named so it is findable among other pages, not just labelled by category.
const fragment = `<title>EDM Zone Quoting Workspace</title>
<style>
${css}
</style>
<div id="root"></div>
<script type="module">
${safeJs}
</script>
`;

writeFileSync(resolve(dist, "preview.fragment.html"), fragment);
writeFileSync(
  resolve(dist, "preview.html"),
  `<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
${fragment}</head>
<body></body>
</html>
`,
);

const kb = (s) => `${(s.length / 1024).toFixed(0)} kB`;
console.log(`Wrote dist-demo/preview.html and preview.fragment.html (${kb(fragment)})`);
