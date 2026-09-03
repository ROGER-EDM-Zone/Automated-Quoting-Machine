/**
 * Entry point for the shareable static preview.
 *
 * Differs from src/main.tsx in exactly two ways: HashRouter, because the
 * preview is served as a single file with no server to rewrite paths; and a
 * banner making it unmistakable that this is recorded data, so nobody mistakes
 * it for the live system and tries to quote from it.
 */
import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { HashRouter } from "react-router-dom";
import App from "../src/App";
import "../src/styles.css";
import "./preview.css";

function PreviewBanner() {
  const [nudged, setNudged] = useState(false);

  useEffect(() => {
    const onWrite = () => {
      setNudged(true);
      const timer = setTimeout(() => setNudged(false), 4000);
      return () => clearTimeout(timer);
    };
    window.addEventListener("aqm-demo-write", onWrite);
    return () => window.removeEventListener("aqm-demo-write", onWrite);
  }, []);

  return (
    <div className={`preview-banner${nudged ? " nudged" : ""}`}>
      {nudged ? (
        <strong>
          Nothing was changed — this preview has no backend, so buttons that
          would write are inert.
        </strong>
      ) : (
        <>
          <strong>Static preview.</strong> The real estimator workspace, showing
          responses recorded from a running backend. Navigation works; anything
          that would write is inert. All figures are illustrative placeholders,
          not real rates.
        </>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <PreviewBanner />
    <HashRouter>
      <App />
    </HashRouter>
  </React.StrictMode>,
);
