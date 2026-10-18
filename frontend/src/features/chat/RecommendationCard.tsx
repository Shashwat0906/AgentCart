import { AlertTriangle, CircleCheck, CircleDashed, CircleX, ShoppingCart } from "lucide-react";
import { Link } from "react-router";
import { Price, ProductGlyph, Rating, StockBadge } from "../../components/ui";
import { useAddToCart } from "../../lib/queries";
import type { RecommendationCard as Card } from "../../lib/types";

const BREAKDOWN_LABELS: Record<string, string> = { budget: "Budget fit", spec: "Spec match", rating: "Ratings", preference: "Your preferences" };

function ScoreRing({ score }: { score: number }) {
  const pct = Math.max(0, Math.min(100, score));
  return (
    <div className="relative grid h-14 w-14 shrink-0 place-items-center" aria-label={`Match score ${Math.round(pct)} out of 100`} role="img">
      <svg viewBox="0 0 36 36" className="absolute inset-0 -rotate-90" aria-hidden>
        <circle cx="18" cy="18" r="15.5" fill="none" stroke="var(--color-line)" strokeWidth="3.5" />
        <circle cx="18" cy="18" r="15.5" fill="none" stroke="var(--color-leaf)" strokeWidth="3.5" strokeLinecap="round" strokeDasharray={`${(pct / 100) * 97.4} 97.4`} />
      </svg>
      <span className="text-sm font-bold tabular-nums">{Math.round(pct)}</span>
    </div>
  );
}

export function RecommendationCard({ card }: { card: Card }) {
  const add = useAddToCart();
  const isAlt = card.is_alternative;
  return (
    <article className={`card animate-rise overflow-hidden ${isAlt ? "border-warn/40" : ""}`} aria-labelledby={`rec-${card.product_id}-${card.rank}`}>
      {isAlt && <p className="bg-warn-soft px-4 py-1.5 text-xs font-bold tracking-wide text-warn uppercase">Alternative — doesn't meet every requirement</p>}
      <div className="flex gap-4 p-4">
        <div className="hidden w-20 shrink-0 sm:block"><ProductGlyph category={card.category} size="sm" className="h-20 w-20" /></div>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="eyebrow">{isAlt ? "Closest option" : `#${card.rank} pick`} · {card.brand}</p>
              <h3 id={`rec-${card.product_id}-${card.rank}`} className="mt-0.5 font-sans text-base leading-snug font-semibold">
                <Link to={`/products/${card.product_id}`} className="hover:text-accent">{card.name}</Link>
              </h3>
              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
                <Price price={card.price_inr} mrp={card.mrp_inr} size="sm" />
                <Rating value={card.rating} count={card.review_count} compact />
                <StockBadge stock={card.stock} />
              </div>
            </div>
            {!isAlt && <ScoreRing score={card.score} />}
          </div>

          {card.key_specs.length > 0 && (
            <ul className="mt-3 flex flex-wrap gap-1.5" aria-label="Key specifications">
              {card.key_specs.map((s) => <li key={s.key} className="chip" title={s.label}>{s.value}</li>)}
            </ul>
          )}

          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <ul className="space-y-1 text-sm" aria-label="Why it fits">
              {card.violations.map((v) => <li key={v} className="flex gap-2 text-danger"><CircleX size={16} className="mt-0.5 shrink-0" aria-hidden /><span><span className="sr-only">Does not meet: </span>{v}</span></li>)}
              {card.reasons.map((r) => <li key={r} className="flex gap-2"><CircleCheck size={16} className="mt-0.5 shrink-0 text-leaf" aria-hidden />{r}</li>)}
            </ul>
            {(card.tradeoffs.length > 0 || card.unmet.length > 0) && (
              <ul className="space-y-1 text-sm" aria-label="Trade-offs">
                {card.unmet.map((u) => <li key={u} className="flex gap-2 text-ink-2"><CircleDashed size={16} className="mt-0.5 shrink-0 text-muted" aria-hidden /><span><span className="sr-only">Not met: </span>{u}</span></li>)}
                {card.tradeoffs.map((t) => <li key={t} className="flex gap-2 text-warn"><AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden /><span><span className="sr-only">Trade-off: </span>{t}</span></li>)}
              </ul>
            )}
          </div>

          {!isAlt && (
            <details className="mt-3 text-sm">
              <summary className="cursor-pointer text-muted hover:text-ink">Score breakdown</summary>
              <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1.5 sm:grid-cols-4">
                {Object.entries(card.breakdown).map(([k, v]) => (
                  <div key={k}>
                    <dt className="text-xs text-muted">{BREAKDOWN_LABELS[k] ?? k}</dt>
                    <dd className="mt-0.5 flex items-center gap-2">
                      <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-paper-2"><span className="block h-full rounded-full bg-leaf" style={{ width: `${Math.round(v * 100)}%` }} /></span>
                      <span className="w-8 text-right text-xs tabular-nums">{Math.round(v * 100)}</span>
                    </dd>
                  </div>
                ))}
              </dl>
            </details>
          )}

          <div className="mt-3 flex gap-2">
            <button className="btn btn-ghost btn-sm" disabled={card.stock <= 0 || add.isPending} onClick={() => add.add(card.product_id)} aria-label={`Add ${card.name} to cart`}>
              <ShoppingCart size={14} aria-hidden /> Add to cart
            </button>
            <Link to={`/products/${card.product_id}`} className="btn btn-ghost btn-sm">Details</Link>
          </div>
        </div>
      </div>
    </article>
  );
}
