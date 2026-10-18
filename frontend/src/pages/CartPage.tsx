import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ShoppingBag, Trash2 } from "lucide-react";
import { Link } from "react-router";
import { Container } from "../components/Layout";
import { useToast } from "../components/toastContext";
import { EmptyState, ErrorState, Notice, PageHeader, ProductGlyph, QuantityStepper, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { formatINR } from "../lib/format";
import { errorMessage, qk, useCart } from "../lib/queries";
import type { Cart } from "../lib/types";

export function CartSummary({ cart, children }: { cart: Pick<Cart, "subtotal_inr" | "shipping_inr" | "total_inr" | "free_shipping_threshold_inr">; children?: React.ReactNode }) {
  const remaining = cart.free_shipping_threshold_inr - cart.subtotal_inr;
  return (
    <aside className="card h-fit p-5" aria-label="Order summary">
      <h2 className="text-lg font-semibold">Summary</h2>
      <dl className="mt-4 space-y-2 text-sm">
        <div className="flex justify-between"><dt className="text-muted">Subtotal</dt><dd className="tabular-nums" data-testid="subtotal">{formatINR(cart.subtotal_inr)}</dd></div>
        <div className="flex justify-between"><dt className="text-muted">Shipping</dt><dd className="tabular-nums">{cart.shipping_inr === 0 ? "Free" : formatINR(cart.shipping_inr)}</dd></div>
        <div className="flex justify-between border-t border-line pt-2 text-base font-semibold"><dt>Total</dt><dd className="tabular-nums" data-testid="total">{formatINR(cart.total_inr)}</dd></div>
      </dl>
      {cart.shipping_inr > 0 && remaining > 0 && <p className="mt-2 text-xs text-muted">Add {formatINR(remaining)} more for free shipping.</p>}
      <p className="mt-3 text-xs text-muted">Totals are calculated by the server from live prices.</p>
      {children}
    </aside>
  );
}

export function CartPage() {
  const cart = useCart();
  const qc = useQueryClient();
  const toast = useToast();
  const onDone = { onSuccess: (c: Cart) => qc.setQueryData(qk.cart, c), onError: (e: unknown) => toast(errorMessage(e), "error") };
  const setQty = useMutation({ mutationFn: ({ id, q }: { id: number; q: number }) => api.setQuantity(id, q), ...onDone });
  const remove = useMutation({ mutationFn: (id: number) => api.removeFromCart(id), ...onDone });

  if (cart.isLoading) return <Container><Spinner label="Loading your cart" /></Container>;
  if (cart.isError || !cart.data) return <Container><ErrorState error={cart.error} onRetry={() => cart.refetch()} /></Container>;
  const c = cart.data;

  return (
    <Container>
      <PageHeader eyebrow="Cart" title="Your cart" />
      {c.items.length === 0 ? (
        <EmptyState title="Your cart is empty" icon={ShoppingBag} action={<div className="flex gap-2"><Link to="/assistant" className="btn btn-accent btn-sm">Ask the assistant</Link><Link to="/products" className="btn btn-ghost btn-sm">Browse</Link></div>}>
          Items you add — or agent proposals you approve — show up here.
        </EmptyState>
      ) : (
        <div className="grid gap-8 lg:grid-cols-[1fr_340px]">
          <section aria-label="Cart items" className="space-y-3">
            {c.issues.length > 0 && <Notice tone="warn" title="Some items need attention">{c.issues.join(" · ")}</Notice>}
            {c.items.map((it) => (
              <article key={it.product_id} className="card flex flex-col gap-4 p-4 sm:flex-row sm:items-center">
                <ProductGlyph category={it.category} size="sm" />
                <div className="min-w-0 flex-1">
                  <Link to={`/products/${it.product_id}`} className="font-semibold hover:text-accent">{it.name}</Link>
                  <p className="text-sm text-muted">{it.brand} · {formatINR(it.unit_price_inr)} each</p>
                  {it.problem && <p className="mt-1 text-sm font-medium text-danger">{it.problem}</p>}
                </div>
                <QuantityStepper label={`Quantity for ${it.name}`} value={it.quantity} max={Math.min(c.max_item_quantity, Math.max(it.stock, 1))} disabled={setQty.isPending} onChange={(q) => setQty.mutate({ id: it.product_id, q })} />
                <p className="w-28 text-right font-semibold tabular-nums">{formatINR(it.line_total_inr)}</p>
                <button className="rounded-lg p-2 text-muted hover:bg-danger-soft hover:text-danger" onClick={() => remove.mutate(it.product_id)} aria-label={`Remove ${it.name}`}><Trash2 size={18} /></button>
              </article>
            ))}
          </section>
          <CartSummary cart={c}>
            <Link to="/checkout" className={`btn btn-primary mt-5 w-full ${c.issues.length ? "pointer-events-none opacity-50" : ""}`} aria-disabled={c.issues.length > 0}>Continue to checkout</Link>
          </CartSummary>
        </div>
      )}
    </Container>
  );
}
