import { GitCompareArrows, ShoppingCart } from "lucide-react";
import { Link } from "react-router";
import { useAddToCart } from "../lib/queries";
import type { Product } from "../lib/types";
import { Price, ProductGlyph, Rating, StockBadge } from "./ui";

export function ProductCard({ product, compared, onToggleCompare }: { product: Product; compared?: boolean; onToggleCompare?: (id: number) => void }) {
  const add = useAddToCart();
  return (
    <article className="card group flex flex-col overflow-hidden transition-shadow hover:shadow-lift" aria-labelledby={`p-${product.id}`}>
      <Link to={`/products/${product.id}`} className="block p-3 pb-0" tabIndex={-1} aria-hidden>
        <ProductGlyph category={product.category} className="aspect-[16/9] transition-transform duration-300 group-hover:scale-[1.015]" />
      </Link>
      <div className="flex flex-1 flex-col gap-2 p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="eyebrow">{product.brand}</span>
          <StockBadge stock={product.stock} />
        </div>
        <h3 id={`p-${product.id}`} className="font-sans text-[15px] leading-snug font-semibold">
          <Link to={`/products/${product.id}`} className="hover:text-accent">{product.name}</Link>
        </h3>
        <Rating value={product.rating} count={product.review_count} />
        <ul className="flex flex-wrap gap-1.5" aria-label="Key specifications">
          {product.key_specs.slice(0, 3).map((s) => (
            <li key={s.key} className="chip" title={s.label}>{s.value}</li>
          ))}
        </ul>
        <div className="mt-auto flex items-end justify-between gap-2 pt-2">
          <Price price={product.price_inr} mrp={product.mrp_inr} />
        </div>
        <div className="flex gap-2">
          <button className="btn btn-primary btn-sm flex-1" disabled={!product.in_stock || add.isPending} onClick={() => add.add(product.id)} aria-label={`Add ${product.name} to cart`}>
            <ShoppingCart size={14} aria-hidden /> {product.in_stock ? "Add to cart" : "Unavailable"}
          </button>
          {onToggleCompare && (
            <button
              className={`btn btn-sm ${compared ? "btn-accent" : "btn-ghost"}`}
              onClick={() => onToggleCompare(product.id)}
              aria-pressed={compared}
              aria-label={compared ? `Remove ${product.name} from comparison` : `Compare ${product.name}`}
              title="Compare"
            >
              <GitCompareArrows size={14} aria-hidden />
            </button>
          )}
        </div>
      </div>
    </article>
  );
}
