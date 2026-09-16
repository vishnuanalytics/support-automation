import { useEffect, useState } from "react";
import { Button } from "./Button";

type Theme = "light" | "dark";
const STORAGE_KEY = "ui-theme";

function systemPrefersDark() {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches;
}

function apply(theme: Theme | null) {
  const root = document.documentElement;
  if (theme) root.setAttribute("data-theme", theme);
  else root.removeAttribute("data-theme");
}

function initial(): Theme {
  const stored = typeof window !== "undefined" ? window.localStorage.getItem(STORAGE_KEY) : null;
  if (stored === "light" || stored === "dark") return stored;
  return systemPrefersDark() ? "dark" : "light";
}

/**
 * Icon button that flips `data-theme` on `<html>` and persists the choice —
 * an explicit pick always wins over `prefers-color-scheme` (see index.css).
 * Mounted once, in the Sidebar's pinned account row.
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(initial);

  useEffect(() => {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    apply(stored === "light" || stored === "dark" ? stored : null);
  }, []);

  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    window.localStorage.setItem(STORAGE_KEY, next);
    apply(next);
  }

  return (
    <Button
      variant="icon"
      size="sm"
      onClick={toggle}
      title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
    >
      {theme === "dark" ? "☀" : "☾"}
    </Button>
  );
}
