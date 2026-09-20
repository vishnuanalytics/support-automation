import React from "react";
import ReactDOM from "react-dom/client";
import "@xyflow/react/dist/style.css";
// react-day-picker's own stylesheet must load before ui.css: both declare
// --rdp-* custom properties on the same .rdp-root selector, equal
// specificity, so whichever comes later in the bundle wins the cascade.
// Importing it here first (rather than in DateRangeFilter.tsx, where
// module load order put it *after* ui.css and silently overrode our dark
// theme back to the library's default light-lavender palette) keeps our
// tokens in charge regardless of which component imports it.
import "react-day-picker/style.css";
import "./index.css";
import "./ui/ui.css";
import { App } from "./App";
import { ToastProvider } from "./ui";
import { ErrorFallback } from "./ErrorFallback";
import { Sentry, initSentry } from "./sentry";

initSentry();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Sentry.ErrorBoundary fallback={<ErrorFallback />}>
      <ToastProvider>
        <App />
      </ToastProvider>
    </Sentry.ErrorBoundary>
  </React.StrictMode>,
);
