import { ArrowRight, BadgeCheck, Brain, ListFilter, Scale, ShieldCheck, Sparkles } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { Container } from "../components/Layout";
import { ProductCard } from "../components/ProductCard";
import { ProductGlyph, SkeletonGrid } from "../components/ui";
import { formatINR } from "../lib/format";
import { useCategories, useProducts } from "../lib/queries";

const EXAMPLES = [
  "I need a laptop under ₹70,000 for machine learning and college work, with 16 GB RAM, good battery life, and upgradeable storage.",
  "Noise-cancelling earbuds under 5k",
  "27 inch QHD gaming monitor under 35000",
  "Smartwatch with GPS for running that works with my iPhone",
];

const STEPS = [
  { icon: Brain, title: "Understands", text: "Turns your sentence into explicit, editable requirements — budget, specs and preferences." },
  { icon: ListFilter, title: "Filters honestly", text: "Hard requirements are applied first. Nothing that breaks them is ever shown as a match." },
  { icon: Scale, title: "Ranks transparently", text: "A deterministic score for budget fit, specs, ratings and your preferences — tune the weights yourself." },
  { icon: ShieldCheck, title: "Asks before acting", text: "Cart changes are proposals you approve. Price and stock are re-checked before anything happens." },
];

export function HomePage() {
  const [q, setQ] = useState("");
  const navigate = useNavigate();
  const categories = useCategories();
  const featured = useProducts({ sort: "rating", in_stock: true, page_size: 6 });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (q.trim()) navigate(`/assistant?q=${encodeURIComponent(q.trim())}`);
  }

  return (
    <>
      <section className="relative overflow-hidden border-b border-line">
        <div className="pointer-events-none absolute inset-0 opacity-70" aria-hidden style={{ background: "radial-gradient(60% 80% at 85% 0%, #fde7d3 0%, transparent 60%), radial-gradient(50% 60% at 0% 100%, #e6f2ea 0%, transparent 60%)" }} />
        <Container className="relative grid gap-10 py-14 lg:grid-cols-[1.15fr_0.85fr] lg:py-20">
          <div className="animate-rise">
            <p className="eyebrow mb-4 inline-flex items-center gap-2"><Sparkles size={14} className="text-accent" aria-hidden /> Agentic commerce, explained</p>
            <h1 className="text-4xl leading-[1.05] font-semibold sm:text-6xl">
              Say what you need.<br />
              <span className="text-accent italic">See exactly why</span> it fits.
            </h1>
            <p className="mt-5 max-w-xl text-lg text-ink-2">
              AgentCart reads your request, searches a real catalogue, ranks options against your constraints and shows its working — and it never touches your cart without your approval.
            </p>
            <form onSubmit={submit} className="mt-8 flex max-w-2xl flex-col gap-2 rounded-2xl border border-line-strong bg-white p-2 shadow-card sm:flex-row" role="search">
              <label htmlFor="hero-q" className="sr-only">Describe what you're shopping for</label>
              <input id="hero-q" className="flex-1 rounded-xl px-3 py-3 text-base outline-none" placeholder="e.g. a laptop under ₹70,000 for ML with 16 GB RAM" value={q} onChange={(e) => setQ(e.target.value)} maxLength={2000} />
              <button className="btn btn-accent px-5 py-3 text-base" type="submit" disabled={!q.trim()}>
                Ask AgentCart <ArrowRight size={18} aria-hidden />
              </button>
            </form>
            <div className="mt-4 flex flex-wrap gap-2">
              {EXAMPLES.slice(1).map((ex) => (
                <button key={ex} className="chip hover:border-accent hover:text-accent" onClick={() => navigate(`/assistant?q=${encodeURIComponent(ex)}`)}>{ex}</button>
              ))}
            </div>
          </div>
          <aside className="card hidden animate-rise self-center p-5 lg:block" aria-label="Example recommendation">
            <p className="eyebrow">What you get back</p>
            <div className="mt-3 flex gap-3">
              <ProductGlyph category="laptops" size="sm" />
              <div>
                <p className="font-semibold">Lenovo IdeaPad Gaming 3</p>
                <p className="text-sm text-muted">{formatINR(61990)} · ₹8,010 under budget</p>
              </div>
              <span className="ml-auto grid h-11 w-11 place-items-center rounded-full border-4 border-leaf/70 text-sm font-bold text-leaf">60</span>
            </div>
            <ul className="mt-4 space-y-1.5 text-sm">
              <li className="flex gap-2"><BadgeCheck size={16} className="mt-0.5 shrink-0 text-leaf" aria-hidden /> RAM: 16 GB ✓ · Upgradeable storage ✓</li>
              <li className="flex gap-2"><BadgeCheck size={16} className="mt-0.5 shrink-0 text-leaf" aria-hidden /> Dedicated GPU (helps with machine learning)</li>
              <li className="flex gap-2 text-warn"><span aria-hidden className="w-4 text-center">!</span> Weaker battery life: 6 h vs up to 12 h</li>
            </ul>
            <div className="mt-4 rounded-xl border border-leaf/25 bg-leaf-soft p-3 text-sm">
              <p className="font-semibold text-leaf">Proposed: add 1 × to your cart</p>
              <div className="mt-2 flex gap-2"><span className="btn btn-approve btn-sm">Approve</span><span className="btn btn-ghost btn-sm">Reject</span></div>
            </div>
          </aside>
        </Container>
      </section>

      <Container>
        <section aria-labelledby="how" className="py-6">
          <h2 id="how" className="text-2xl font-semibold">How the assistant works</h2>
          <ol className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {STEPS.map((s, i) => (
              <li key={s.title} className="card p-5">
                <div className="flex items-center gap-3">
                  <span className="grid h-9 w-9 place-items-center rounded-xl bg-accent-soft text-accent"><s.icon size={18} aria-hidden /></span>
                  <span className="text-xs font-bold text-muted">0{i + 1}</span>
                </div>
                <h3 className="mt-3 font-sans text-base font-semibold">{s.title}</h3>
                <p className="mt-1 text-sm text-muted">{s.text}</p>
              </li>
            ))}
          </ol>
        </section>

        <section aria-labelledby="cats" className="py-8">
          <div className="flex items-end justify-between">
            <h2 id="cats" className="text-2xl font-semibold">Browse the catalogue</h2>
            <Link to="/products" className="text-sm font-semibold text-accent hover:underline">All products →</Link>
          </div>
          <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5 xl:grid-cols-9">
            {(categories.data ?? []).map((c) => (
              <Link key={c.key} to={`/products?category=${c.key}`} className="card flex flex-col items-center gap-2 p-3 text-center transition-shadow hover:shadow-lift">
                <ProductGlyph category={c.key} size="sm" />
                <span className="text-sm font-semibold">{c.label}</span>
                <span className="text-xs text-muted">{c.product_count} items</span>
              </Link>
            ))}
            {categories.isLoading && Array.from({ length: 9 }, (_, i) => <div key={i} className="skeleton h-28" />)}
          </div>
        </section>

        <section aria-labelledby="top" className="py-8">
          <h2 id="top" className="mb-5 text-2xl font-semibold">Highest rated right now</h2>
          {featured.isLoading ? <SkeletonGrid /> : (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {featured.data?.items.map((p) => <ProductCard key={p.id} product={p} />)}
            </div>
          )}
        </section>
      </Container>
    </>
  );
}
