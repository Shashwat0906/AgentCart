import { useQuery } from "@tanstack/react-query";
import { GitCompareArrows, Search, SlidersHorizontal, X } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";
import { Container } from "../components/Layout";
import { ProductCard } from "../components/ProductCard";
import { EmptyState, ErrorState, PageHeader, Pagination, SkeletonGrid } from "../components/ui";
import { api, type ProductQuery } from "../lib/api";
import { CATEGORY_LABELS } from "../lib/format";
import { qk, useCategories, useProducts } from "../lib/queries";

const SORTS: { value: NonNullable<ProductQuery["sort"]>; label: string }[] = [
  { value: "relevance", label: "Relevance" },
  { value: "price_asc", label: "Price: low to high" },
  { value: "price_desc", label: "Price: high to low" },
  { value: "rating", label: "Top rated" },
  { value: "name", label: "Name" },
];

function num(v: string | null): number | undefined {
  if (!v) return undefined;
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : undefined;
}

export function CatalogPage() {
  const [params, setParams] = useSearchParams();
  const [showFilters, setShowFilters] = useState(false);
  const [compare, setCompare] = useState<number[]>([]);
  const query: ProductQuery = useMemo(
    () => ({
      q: params.get("q") ?? undefined,
      category: params.get("category") ?? undefined,
      brand: params.getAll("brand"),
      min_price: num(params.get("min_price")),
      max_price: num(params.get("max_price")),
      min_rating: num(params.get("min_rating")),
      in_stock: params.get("in_stock") === "true" || undefined,
      sort: (params.get("sort") as ProductQuery["sort"]) ?? "relevance",
      page: num(params.get("page")) ?? 1,
      page_size: 12,
    }),
    [params],
  );
  const products = useProducts(query);
  const categories = useCategories();
  const brands = useQuery({ queryKey: qk.brands(query.category), queryFn: () => api.brands(query.category) });
  const [text, setText] = useState(query.q ?? "");
  useEffect(() => setText(query.q ?? ""), [query.q]);

  function update(patch: Record<string, string | string[] | undefined>, resetPage = true) {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      next.delete(k);
      if (Array.isArray(v)) v.forEach((x) => next.append(k, x));
      else if (v !== undefined && v !== "") next.set(k, v);
    }
    if (resetPage) next.delete("page");
    setParams(next);
  }

  function onSearch(e: FormEvent) {
    e.preventDefault();
    update({ q: text.trim() || undefined });
  }

  const toggleCompare = (id: number) => setCompare((c) => (c.includes(id) ? c.filter((x) => x !== id) : c.length >= 4 ? c : [...c, id]));
  const title = query.category ? CATEGORY_LABELS[query.category] ?? "Products" : query.q ? `Results for “${query.q}”` : "All products";
  const activeFilters = !!(query.category || query.brand?.length || query.min_price || query.max_price || query.min_rating || query.in_stock || query.q);

  const filters = (
    <div className="space-y-6">
      <fieldset>
        <legend className="label">Category</legend>
        <div className="flex flex-col gap-1">
          <label className="flex items-center gap-2 text-sm"><input type="radio" name="cat" checked={!query.category} onChange={() => update({ category: undefined, brand: [] })} /> All</label>
          {(categories.data ?? []).map((c) => (
            <label key={c.key} className="flex items-center gap-2 text-sm">
              <input type="radio" name="cat" checked={query.category === c.key} onChange={() => update({ category: c.key, brand: [] })} />
              {c.label} <span className="text-muted">({c.product_count})</span>
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend className="label">Price (₹)</legend>
        <div className="flex items-center gap-2">
          <input aria-label="Minimum price" className="input py-1.5" type="number" min={0} step={500} placeholder="Min" defaultValue={query.min_price} key={`min-${query.min_price}`} onBlur={(e) => update({ min_price: e.target.value || undefined })} />
          <span className="text-muted">–</span>
          <input aria-label="Maximum price" className="input py-1.5" type="number" min={0} step={500} placeholder="Max" defaultValue={query.max_price} key={`max-${query.max_price}`} onBlur={(e) => update({ max_price: e.target.value || undefined })} />
        </div>
      </fieldset>
      <fieldset>
        <legend className="label">Minimum rating</legend>
        <select className="input py-1.5" value={query.min_rating ?? ""} onChange={(e) => update({ min_rating: e.target.value || undefined })}>
          <option value="">Any</option>
          <option value="4.5">4.5★ & up</option>
          <option value="4">4★ & up</option>
          <option value="3.5">3.5★ & up</option>
        </select>
      </fieldset>
      <label className="flex items-center gap-2 text-sm font-medium">
        <input type="checkbox" checked={!!query.in_stock} onChange={(e) => update({ in_stock: e.target.checked ? "true" : undefined })} /> In stock only
      </label>
      {!!brands.data?.length && (
        <fieldset>
          <legend className="label">Brand</legend>
          <div className="flex max-h-56 flex-col gap-1 overflow-auto pr-1">
            {brands.data.map((b) => (
              <label key={b} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={query.brand?.includes(b) ?? false} onChange={(e) => update({ brand: e.target.checked ? [...(query.brand ?? []), b] : (query.brand ?? []).filter((x) => x !== b) })} />
                {b}
              </label>
            ))}
          </div>
        </fieldset>
      )}
      {activeFilters && <button className="btn btn-ghost btn-sm w-full" onClick={() => setParams(new URLSearchParams())}>Clear all filters</button>}
    </div>
  );

  return (
    <Container>
      <PageHeader eyebrow="Catalogue" title={title}>
        <form onSubmit={onSearch} className="flex w-full max-w-md gap-2" role="search">
          <label htmlFor="catalog-q" className="sr-only">Search products</label>
          <input id="catalog-q" className="input" placeholder="Search name, brand or feature…" value={text} onChange={(e) => setText(e.target.value)} maxLength={120} />
          <button className="btn btn-primary" type="submit" aria-label="Search"><Search size={16} aria-hidden /></button>
        </form>
      </PageHeader>

      <div className="grid gap-8 lg:grid-cols-[240px_1fr]">
        <aside className="hidden lg:block" aria-label="Filters">{filters}</aside>
        <section aria-label="Products" aria-busy={products.isFetching}>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-muted" aria-live="polite">
              {products.data ? `${products.data.total} product${products.data.total === 1 ? "" : "s"}` : " "}
            </p>
            <div className="flex items-center gap-2">
              <button className="btn btn-ghost btn-sm lg:hidden" onClick={() => setShowFilters(true)}><SlidersHorizontal size={14} aria-hidden /> Filters</button>
              <label htmlFor="sort" className="sr-only">Sort by</label>
              <select id="sort" className="input w-auto py-1.5 text-sm" value={query.sort} onChange={(e) => update({ sort: e.target.value })}>
                {SORTS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
              </select>
            </div>
          </div>
          {products.isLoading ? <SkeletonGrid /> : products.isError ? <ErrorState error={products.error} onRetry={() => products.refetch()} /> : products.data && products.data.items.length === 0 ? (
            <EmptyState title="No products match" action={<button className="btn btn-ghost btn-sm" onClick={() => setParams(new URLSearchParams())}>Clear filters</button>}>
              Try a broader search, or describe what you need to the <Link to="/assistant" className="font-semibold text-accent">assistant</Link>.
            </EmptyState>
          ) : (
            <>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {products.data?.items.map((p) => <ProductCard key={p.id} product={p} compared={compare.includes(p.id)} onToggleCompare={toggleCompare} />)}
              </div>
              <div className="mt-8">
                <Pagination page={query.page ?? 1} pages={products.data?.pages ?? 1} onPage={(p) => { update({ page: String(p) }, false); window.scrollTo({ top: 0 }); }} />
              </div>
            </>
          )}
        </section>
      </div>

      {showFilters && (
        <div className="fixed inset-0 z-50 bg-ink/40 lg:hidden" role="dialog" aria-modal="true" aria-label="Filters" onClick={() => setShowFilters(false)}>
          <div className="absolute inset-y-0 right-0 w-80 max-w-full overflow-auto bg-paper p-5" onClick={(e) => e.stopPropagation()}>
            <div className="mb-4 flex items-center justify-between"><h2 className="text-lg font-semibold">Filters</h2><button onClick={() => setShowFilters(false)} aria-label="Close filters"><X size={20} /></button></div>
            {filters}
          </div>
        </div>
      )}

      {compare.length > 0 && (
        <div className="fixed inset-x-0 bottom-4 z-40 flex justify-center px-4">
          <div className="flex items-center gap-3 rounded-2xl bg-ink px-4 py-3 text-sm text-paper shadow-lift">
            <GitCompareArrows size={16} aria-hidden />
            <span>{compare.length} selected{compare.length === 4 ? " (max)" : ""}</span>
            <button className="text-paper/70 underline" onClick={() => setCompare([])}>Clear</button>
            <Link to={`/compare?ids=${compare.join(",")}`} className={`btn btn-accent btn-sm ${compare.length < 2 ? "pointer-events-none opacity-50" : ""}`} aria-disabled={compare.length < 2}>Compare</Link>
          </div>
        </div>
      )}
    </Container>
  );
}
