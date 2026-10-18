import { useQuery } from "@tanstack/react-query";
import { Package } from "lucide-react";
import { Link } from "react-router";
import { Container } from "../components/Layout";
import { EmptyState, ErrorState, PageHeader, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";
import { qk } from "../lib/queries";

export function OrdersPage() {
  const orders = useQuery({ queryKey: qk.orders, queryFn: api.orders });
  return (
    <Container className="max-w-4xl">
      <PageHeader eyebrow="Account" title="Orders" />
      {orders.isLoading ? <Spinner /> : orders.isError ? <ErrorState error={orders.error} onRetry={() => orders.refetch()} /> : !orders.data?.length ? (
        <EmptyState title="No orders yet" icon={Package} action={<Link to="/products" className="btn btn-primary btn-sm">Start shopping</Link>}>Confirmed demo orders appear here.</EmptyState>
      ) : (
        <ul className="space-y-4">
          {orders.data.map((o) => (
            <li key={o.id} className="card p-5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="font-semibold">Order #{o.id}</p>
                  <p className="text-sm text-muted">{formatDate(o.created_at)}</p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="chip border-leaf/25 bg-leaf-soft text-leaf">{o.status}</span>
                  <span className="chip">{o.payment_mode} payment</span>
                  <span className="text-lg font-semibold tabular-nums">{formatINR(o.total_inr)}</span>
                </div>
              </div>
              <ul className="mt-3 divide-y divide-line border-t border-line text-sm">
                {o.items.map((it) => (
                  <li key={it.sku} className="flex justify-between gap-3 py-2">
                    <span>{it.quantity} × {it.product_id ? <Link to={`/products/${it.product_id}`} className="hover:text-accent">{it.name}</Link> : it.name}</span>
                    <span className="tabular-nums text-muted">{formatINR(it.line_total_inr)}</span>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </Container>
  );
}
