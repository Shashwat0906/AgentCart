import { useQuery } from "@tanstack/react-query";
import { Bot, ChevronLeft, ShoppingCart } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { Container } from "../components/Layout";
import { ErrorState, Notice, Price, ProductGlyph, QuantityStepper, Rating, StockBadge } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { CATEGORY_LABELS } from "../lib/format";
import { qk, useAddToCart } from "../lib/queries";

export function ProductPage() {
  const id = Number(useParams().id);
  const product = useQuery({ queryKey: qk.product(id), queryFn: () => api.product(id), enabled: Number.isInteger(id) && id > 0 });
  const [qty, setQty] = useState(1);
  const add = useAddToCart();

  if (product.isLoading) {
    return <Container><div className="grid gap-8 md:grid-cols-2"><div className="skeleton h-80" /><div className="space-y-3"><div className="skeleton h-8 w-3/4" /><div className="skeleton h-5 w-1/3" /><div className="skeleton h-40" /></div></div></Container>;
  }
  if (product.isError || !product.data) {
    const notFound = product.error instanceof ApiError && product.error.status === 404;
    return <Container>{notFound ? <ErrorState error={new Error("This product doesn't exist.")} /> : <ErrorState error={product.error} onRetry={() => product.refetch()} />}</Container>;
  }
  const p = product.data;
  const maxQty = Math.min(10, Math.max(1, p.stock));

  return (
    <Container>
      <Link to={`/products?category=${p.category}`} className="mb-5 inline-flex items-center gap-1 text-sm text-muted hover:text-ink"><ChevronLeft size={16} aria-hidden /> {CATEGORY_LABELS[p.category] ?? "Products"}</Link>
      <div className="grid gap-10 md:grid-cols-[1fr_1.1fr]">
        <div className="self-start md:sticky md:top-24"><ProductGlyph category={p.category} size="lg" className="aspect-square" /></div>
        <div>
          <p className="eyebrow">{p.brand} · {p.sku}</p>
          <h1 className="mt-2 text-3xl font-semibold sm:text-4xl">{p.name}</h1>
          <div className="mt-3 flex flex-wrap items-center gap-3"><Rating value={p.rating} count={p.review_count} /><StockBadge stock={p.stock} /></div>
          <div className="mt-5"><Price price={p.price_inr} mrp={p.mrp_inr} size="lg" /></div>
          <p className="mt-1 text-xs text-muted">Inclusive of taxes · demo pricing</p>
          {/* Product descriptions are untrusted catalogue text: rendered as plain text, never as HTML. */}
          <p className="mt-5 leading-relaxed whitespace-pre-line text-ink-2">{p.description}</p>
          <div className="mt-6 flex flex-wrap items-center gap-3">
            <QuantityStepper value={qty} max={maxQty} onChange={setQty} disabled={!p.in_stock} label="Quantity" />
            <button className="btn btn-primary" disabled={!p.in_stock || add.isPending} onClick={() => add.add(p.id, qty)}>
              <ShoppingCart size={16} aria-hidden /> Add to cart
            </button>
            <Link className="btn btn-ghost" to={`/assistant?q=${encodeURIComponent(`Compare alternatives to the ${p.name}`)}`}>
              <Bot size={16} aria-hidden /> Ask the assistant
            </Link>
          </div>
          {!p.in_stock && <div className="mt-4"><Notice tone="warn">This product is out of stock. The assistant will never recommend it as a match.</Notice></div>}
          <section className="mt-8" aria-labelledby="specs">
            <h2 id="specs" className="mb-3 text-xl font-semibold">Specifications</h2>
            <dl className="divide-y divide-line overflow-hidden rounded-xl border border-line bg-white">
              {p.specs.map((s) => (
                <div key={s.key} className="grid grid-cols-[minmax(120px,40%)_1fr] gap-3 px-4 py-2.5 text-sm">
                  <dt className="text-muted">{s.label}</dt><dd className="font-medium">{s.value}</dd>
                </div>
              ))}
            </dl>
          </section>
        </div>
      </div>
    </Container>
  );
}
