import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

type ToastSpec = { id: number; message: ReactNode; undo?: (() => void) | null };

/** Presentational — bottom-right, one at a time. */
export function Toast({ message, undo }: { message: ReactNode; undo?: (() => void) | null }) {
  return (
    <div className="ui-toast" role="status" aria-live="polite">
      <span>{message}</span>
      {undo && (
        <button type="button" className="ui-toast__undo" onClick={undo}>
          Undo
        </button>
      )}
    </div>
  );
}

const ToastCtx = createContext<(message: ReactNode, undo?: (() => void) | null) => void>(() => {});

/** `const toast = useToast(); toast("Saved", () => restore())` */
export function useToast() {
  return useContext(ToastCtx);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<ToastSpec | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const toast = useCallback((message: ReactNode, undo?: (() => void) | null) => {
    if (timer.current) clearTimeout(timer.current);
    const id = Date.now();
    setCurrent({ id, message, undo });
    timer.current = setTimeout(() => setCurrent((c) => (c?.id === id ? null : c)), 4000);
  }, []);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  return (
    <ToastCtx.Provider value={toast}>
      {children}
      {current && (
        <div className="ui-toast-host">
          <Toast
            key={current.id}
            message={current.message}
            undo={
              current.undo
                ? () => {
                    current.undo?.();
                    setCurrent(null);
                  }
                : null
            }
          />
        </div>
      )}
    </ToastCtx.Provider>
  );
}
