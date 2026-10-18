import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Headphones,
  Keyboard,
  Laptop,
  Minus,
  Monitor,
  Mouse,
  PackageSearch,
  Plus,
  Smartphone,
  Speaker,
  Star,
  Tablet,
  Watch,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { discountPercent, formatINR } from "../lib/format";

const GLYPHS: Record<string, { icon: LucideIcon; from: string; to: string; fg: string }> = {
  laptops: { icon: Laptop, from: "#fde7d3", to: "#f9c9a4", fg: "#9a3412" },
  smartphones: { icon: Smartphone, from: "#dbeafe", to: "#bcd3f5", fg: "#1e4f8a" },
  headphones: { icon: Headphones, from: "#ede4fb", to: "#d6c6f4", fg: "#5b3a9b" },
  monitors: { icon: Monitor, from: "#dcefe4", to: "#bcdcc9", fg: "#2f6f4f" },
  tablets: { icon: Tablet, from: "#fdf1c7", to: "#f7dd8a", fg: "#8a5a00" },
  smartwatches: { icon: Watch, from: "#fbe0e4", to: "#f4bcc6", fg: "#9f1f3d" },
  keyboards: { icon: Keyboard, from: "#e5e7eb", to: "#cfd3da", fg: "#374151" },
  mice: { icon: Mouse, from: "#e0f2f1", to: "#b9e0dc", fg: "#11635c" },
  speakers: { icon: Speaker, from: "#fde2cf", to: "#f6c09a", fg: "#8c3b0b" },
};

/** Category illustration tile — the demo catalogue intentionally ships without product photos. */
export function ProductGlyph({ category, size = "md", className = "" }: { category: string; size?: "sm" | "md" | "lg"; className?: string }) {
  const g = GLYPHS[category] ?? GLYPHS.keyboards!;
  const Icon = g.icon;
  const dims = { sm: "h-12 w-12 rounded-xl", md: "h-full min-h-28 w-full rounded-xl", lg: "h-full min-h-64 w-full rounded-2xl" }[size];
  const icon = { sm: 22, md: 40, lg: 88 }[size];
  return (
    <div
      aria-hidden
      className={`relative flex items-center justify-center overflow-hidden ${dims} ${className}`}
      style={{ background: `radial-gradient(120% 120% at 20% 10%, ${g.from}, ${g.to})` }}
    >
      <div className="absolute -right-6 -bottom-6 h-24 w-24 rounded-full opacity-30" style={{ background: g.fg }} />
      <Icon size={icon} strokeWidth={1.4} color={g.fg} />
    </div>
  );
}

export function Rating({ value, count, compact = false }: { value: number; count?: number; compact?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1 text-sm text-ink-2" aria-label={`Rated ${value.toFixed(1)} out of 5${count !== undefined ? ` from ${count} reviews` : ""}`}>
      <Star size={14} className="fill-gold text-gold" aria-hidden />
      <span className="font-semibold">{value.toFixed(1)}</span>
      {count !== undefined && !compact && <span className="text-muted">({count.toLocaleString("en-IN")})</span>}
    </span>
  );
}

export function Price({ price, mrp, size = "md" }: { price: number; mrp?: number; size?: "sm" | "md" | "lg" }) {
  const off = mrp ? discountPercent(price, mrp) : 0;
  const cls = { sm: "text-base", md: "text-lg", lg: "text-3xl" }[size];
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-2">
      <span className={`font-semibold tracking-tight text-ink ${cls}`}>{formatINR(price)}</span>
      {off > 0 && mrp && (
        <>
          <span className="text-sm text-muted line-through">{formatINR(mrp)}</span>
          <span className="text-sm font-semibold text-leaf">{off}% off</span>
        </>
      )}
    </span>
  );
}

export function StockBadge({ stock }: { stock: number }) {
  if (stock <= 0) return <span className="chip border-danger/30 bg-danger-soft text-danger">Out of stock</span>;
  if (stock <= 3) return <span className="chip border-warn/30 bg-warn-soft text-warn">Only {stock} left</span>;
  return <span className="chip border-leaf/20 bg-leaf-soft text-leaf">In stock</span>;
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-muted">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-line-strong border-t-accent" aria-hidden />
      {label}
    </span>
  );
}

export function EmptyState({ title, children, action, icon: Icon = PackageSearch }: { title: string; children?: ReactNode; action?: ReactNode; icon?: LucideIcon }) {
  return (
    <div className="card flex flex-col items-center gap-3 px-6 py-14 text-center">
      <div className="rounded-2xl bg-paper-2 p-4"><Icon size={28} className="text-muted" aria-hidden /></div>
      <h2 className="text-xl font-semibold">{title}</h2>
      {children && <div className="max-w-md text-sm text-muted">{children}</div>}
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  return (
    <div role="alert" className="card flex flex-col items-center gap-3 border-danger/30 px-6 py-10 text-center">
      <AlertTriangle className="text-danger" aria-hidden />
      <p className="font-semibold">We couldn't load this</p>
      <p className="max-w-md text-sm text-muted">{message}</p>
      {onRetry && <button className="btn btn-ghost btn-sm" onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function Notice({ tone = "info", children, title }: { tone?: "info" | "warn" | "danger" | "success"; children: ReactNode; title?: string }) {
  const styles = {
    info: "border-info/25 bg-info-soft text-info",
    warn: "border-warn/30 bg-warn-soft text-warn",
    danger: "border-danger/30 bg-danger-soft text-danger",
    success: "border-leaf/25 bg-leaf-soft text-leaf",
  }[tone];
  return (
    <div className={`rounded-xl border px-3.5 py-2.5 text-sm ${styles}`} role={tone === "danger" ? "alert" : undefined}>
      {title && <p className="font-semibold">{title}</p>}
      <div className="text-ink-2">{children}</div>
    </div>
  );
}

export function QuantityStepper({ value, min = 1, max, onChange, disabled, label }: { value: number; min?: number; max: number; onChange: (v: number) => void; disabled?: boolean; label: string }) {
  return (
    <div className="inline-flex items-center rounded-xl border border-line-strong bg-white" role="group" aria-label={label}>
      <button type="button" className="p-2 text-ink-2 disabled:opacity-40" onClick={() => onChange(value - 1)} disabled={disabled || value <= min} aria-label="Decrease quantity">
        <Minus size={14} />
      </button>
      <span className="min-w-8 text-center text-sm font-semibold tabular-nums" aria-live="polite">{value}</span>
      <button type="button" className="p-2 text-ink-2 disabled:opacity-40" onClick={() => onChange(value + 1)} disabled={disabled || value >= max} aria-label="Increase quantity">
        <Plus size={14} />
      </button>
    </div>
  );
}

export function Pagination({ page, pages, onPage }: { page: number; pages: number; onPage: (p: number) => void }) {
  if (pages <= 1) return null;
  return (
    <nav className="flex items-center justify-center gap-2" aria-label="Pagination">
      <button className="btn btn-ghost btn-sm" onClick={() => onPage(page - 1)} disabled={page <= 1} aria-label="Previous page">
        <ChevronLeft size={16} /> Prev
      </button>
      <span className="px-2 text-sm text-muted">Page <strong className="text-ink">{page}</strong> of {pages}</span>
      <button className="btn btn-ghost btn-sm" onClick={() => onPage(page + 1)} disabled={page >= pages} aria-label="Next page">
        Next <ChevronRight size={16} />
      </button>
    </nav>
  );
}

export function SkeletonGrid({ count = 6 }: { count?: number }) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-busy="true" aria-label="Loading products">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="card space-y-3 p-4">
          <div className="skeleton h-32" />
          <div className="skeleton h-4 w-3/4" />
          <div className="skeleton h-4 w-1/2" />
          <div className="skeleton h-8 w-full" />
        </div>
      ))}
    </div>
  );
}

export function PageHeader({ eyebrow, title, children }: { eyebrow?: string; title: string; children?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        {eyebrow && <p className="eyebrow mb-1">{eyebrow}</p>}
        <h1 className="text-3xl font-semibold sm:text-4xl">{title}</h1>
      </div>
      {children}
    </header>
  );
}
