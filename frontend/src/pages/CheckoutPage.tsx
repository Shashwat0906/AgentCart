import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Lock, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { Container } from "../components/Layout";
import { EmptyState, Notice, PageHeader, Spinner } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { formatINR } from "../lib/format";
import { errorMessage, qk, useCart } from "../lib/queries";
import type { Order, Quote } from "../lib/types";
import { CartSummary } from "./CartPage";

function conflictText(err: ApiError): string {
  const d = err.details as { reason?: string; changes?: { name: string; quoted_price_inr: number; current_price_inr: number }[]; items?: { name: string; available: number }[] };
  if (d.reason === "price_changed" && d.changes?.length) {
    return d.changes.map((c) => `${c.name}: ${formatINR(c.quoted_price_inr)} → ${formatINR(c.current_price_inr)}`).join(" · ");
  }
  if (d.reason === "insufficient_stock" && d.items?.length) {
    return d.items.map((i) => `${i.name}: only ${i.available} available`).join(" · ");
  }
  return err.message;
}

export function CheckoutPage() {
  const cart = useCart();
  const qc = useQueryClient();
  const [quote, setQuote] = useState<Quote | null>(null);
  const [agreed, setAgreed] = useState(false);
  const [order, setOrder] = useState<Order | null>(null);
  const [conflict, setConflict] = useState<ApiError | null>(null);
  const idemKey = useRef<string>("");

  const getQuote = useMutation({
    mutationFn: api.quote,
    onSuccess: (q) => {
      setQuote(q);
      setAgreed(false);
      setConflict(null);
      idemKey.current = crypto.randomUUID().replace(/-/g, "");
    },
  });
  const confirm = useMutation({
    mutationFn: () => api.confirm(quote!.quote_id, idemKey.current),
    onSuccess: (o) => {
      setOrder(o);
      qc.invalidateQueries({ queryKey: qk.cart });
      qc.invalidateQueries({ queryKey: qk.orders });
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setConflict(err);
        setQuote(null);
        qc.invalidateQueries({ queryKey: qk.cart });
      }
    },
  });

  const { mutate: requestQuote } = getQuote;
  const hasItems = (cart.data?.items.length ?? 0) > 0;
  useEffect(() => {
    if (hasItems && !quote && !order && !conflict && getQuote.isIdle) requestQuote();
  }, [hasItems, quote, order, conflict, getQuote.isIdle, requestQuote]);

  if (order) {
    return (
      <Container className="max-w-2xl">
        <div className="card p-8 text-center" role="status">
          <CheckCircle2 size={44} className="mx-auto text-leaf" aria-hidden />
          <h1 className="mt-3 text-3xl font-semibold">Demo order placed</h1>
          <p className="mt-2 text-muted">Order #{order.id} · {formatINR(order.total_inr)} · no payment was taken.</p>
          <div className="mt-6 flex justify-center gap-2"><Link to="/orders" className="btn btn-primary">View orders</Link><Link to="/products" className="btn btn-ghost">Keep shopping</Link></div>
        </div>
      </Container>
    );
  }
  if (cart.isLoading) return <Container><Spinner /></Container>;
  if (!hasItems) {
    return <Container><EmptyState title="Nothing to check out" action={<Link to="/products" className="btn btn-primary btn-sm">Browse products</Link>}>Your cart is empty.</EmptyState></Container>;
  }

  return (
    <Container>
      <PageHeader eyebrow="Checkout" title="Review & confirm" />
      <div className="grid gap-8 lg:grid-cols-[1fr_340px]">
        <section className="space-y-4">
          <Notice tone="info" title="Demo checkout">This is a portfolio demo: confirming creates an order record and reduces stock, but no payment is ever collected.</Notice>
          {conflict && (
            <Notice tone="warn" title="Something changed since your quote">
              <p>{conflictText(conflict)}</p>
              <button className="btn btn-ghost btn-sm mt-2" onClick={() => getQuote.mutate()}><RefreshCw size={14} aria-hidden /> Get an updated quote</button>
            </Notice>
          )}
          {getQuote.isError && <Notice tone="danger" title="Can't check out yet">{errorMessage(getQuote.error)} <Link to="/cart" className="font-semibold underline">Review cart</Link></Notice>}
          {getQuote.isPending && <Spinner label="Re-checking live prices and stock" />}
          {quote && (
            <div className="card divide-y divide-line">
              {quote.items.map((it) => (
                <div key={it.product_id} className="flex items-center justify-between gap-4 p-4 text-sm">
                  <div><p className="font-semibold">{it.name}</p><p className="text-muted">{it.quantity} × {formatINR(it.unit_price_inr)}</p></div>
                  <p className="font-semibold tabular-nums">{formatINR(it.line_total_inr)}</p>
                </div>
              ))}
            </div>
          )}
        </section>
        {quote && (
          <CartSummary cart={{ ...quote, free_shipping_threshold_inr: cart.data?.free_shipping_threshold_inr ?? 0 }}>
            <p className="mt-3 text-xs text-muted">Quote valid until {new Date(quote.expires_at).toLocaleTimeString("en-IN", { timeStyle: "short" })}. Prices and stock are verified again when you confirm.</p>
            <label className="mt-4 flex items-start gap-2 text-sm">
              <input type="checkbox" className="mt-1" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />
              <span>I confirm this <strong>demo order</strong> of {formatINR(quote.total_inr)}. No payment will be taken.</span>
            </label>
            {confirm.isError && !(confirm.error instanceof ApiError && confirm.error.status === 409) && <div className="mt-3"><Notice tone="danger">{errorMessage(confirm.error)}</Notice></div>}
            <button className="btn btn-approve mt-4 w-full" disabled={!agreed || confirm.isPending} onClick={() => confirm.mutate()}>
              <Lock size={15} aria-hidden /> {confirm.isPending ? "Placing order…" : "Place demo order"}
            </button>
          </CartSummary>
        )}
      </div>
    </Container>
  );
}
