import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { CheckIcon, AlertTriangleIcon, XIcon } from "./icons";
import { IconButton } from "./IconButton";
import styles from "./Toast.module.css";

export type ToastTone = "success" | "error" | "info";

interface Toast {
  id: number;
  tone: ToastTone;
  title: string;
  description?: string;
}

interface ToastApi {
  toast: (t: { tone?: ToastTone; title: string; description?: string }) => void;
  success: (title: string, description?: string) => void;
  error: (title: string, description?: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

/** Access the toast API. Must be used within <ToastProvider>. */
export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within <ToastProvider>");
  return ctx;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const idRef = useRef(0);

  const remove = useCallback((id: number) => {
    setToasts((list) => list.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (t: { tone?: ToastTone; title: string; description?: string }) => {
      const id = ++idRef.current;
      const toast: Toast = { id, tone: t.tone ?? "info", title: t.title, description: t.description };
      setToasts((list) => [...list, toast]);
      window.setTimeout(() => remove(id), t.tone === "error" ? 6000 : 4000);
    },
    [remove],
  );

  const api = useMemo<ToastApi>(
    () => ({
      toast: push,
      success: (title, description) => push({ tone: "success", title, description }),
      error: (title, description) => push({ tone: "error", title, description }),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      {createPortal(
        <ToastViewport toasts={toasts} onDismiss={remove} />,
        document.body,
      )}
    </ToastContext.Provider>
  );
}

function ToastViewport({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  const reduce = useReducedMotion();
  return (
    <div className={styles.viewport} role="region" aria-label="Notifications">
      <AnimatePresence initial={false}>
        {toasts.map((t) => (
          <motion.div
            key={t.id}
            layout={!reduce}
            initial={reduce ? { opacity: 0 } : { opacity: 0, y: 12, scale: 0.96 }}
            animate={reduce ? { opacity: 1 } : { opacity: 1, y: 0, scale: 1 }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, x: 24, scale: 0.96 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className={`${styles.toast} ${styles[t.tone]}`}
            role={t.tone === "error" ? "alert" : "status"}
          >
            <span className={styles.icon} aria-hidden="true">
              {t.tone === "success" ? <CheckIcon /> : t.tone === "error" ? <AlertTriangleIcon /> : <CheckIcon />}
            </span>
            <div className={styles.content}>
              <p className={styles.title}>{t.title}</p>
              {t.description && <p className={styles.desc}>{t.description}</p>}
            </div>
            <IconButton
              label="Dismiss notification"
              icon={<XIcon />}
              size="sm"
              onClick={() => onDismiss(t.id)}
            />
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
