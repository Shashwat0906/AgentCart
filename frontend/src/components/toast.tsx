import { CheckCircle2, Info, X, XCircle } from "lucide-react";
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { ToastContext, type Tone } from "./toastContext";

interface Toast {
  id: number;
  tone: Tone;
  message: string;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const push = useCallback(
    (message: string, tone: Tone = "success") => {
      const id = Date.now() + Math.random();
      setToasts((t) => [...t.slice(-2), { id, tone, message }]);
      window.setTimeout(() => dismiss(id), 4500);
    },
    [dismiss],
  );
  const icons = useMemo(() => ({ success: CheckCircle2, error: XCircle, info: Info }), []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 bottom-4 z-50 flex flex-col items-center gap-2 px-4" aria-live="polite" aria-atomic="false">
        {toasts.map((t) => {
          const Icon = icons[t.tone];
          return (
            <div key={t.id} className="pointer-events-auto flex w-full max-w-sm animate-rise items-start gap-3 rounded-xl bg-ink px-4 py-3 text-sm text-paper shadow-lift" role={t.tone === "error" ? "alert" : "status"}>
              <Icon size={18} className={t.tone === "error" ? "text-red-300" : t.tone === "success" ? "text-emerald-300" : "text-sky-300"} aria-hidden />
              <span className="flex-1">{t.message}</span>
              <button onClick={() => dismiss(t.id)} aria-label="Dismiss notification" className="text-paper/60 hover:text-paper">
                <X size={16} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
