import { Bot, LogOut, Menu, ShoppingBag, SlidersHorizontal, X } from "lucide-react";
import { useState } from "react";
import { Link, NavLink, Navigate, Outlet, useLocation } from "react-router";
import { useAuth, useVerifySession } from "../lib/useAuth";
import { useCart } from "../lib/queries";

function Logo() {
  return (
    <Link to="/" className="flex items-center gap-2" aria-label="AgentCart home">
      <span className="grid h-8 w-8 place-items-center rounded-lg bg-ink text-gold">
        <ShoppingBag size={17} strokeWidth={2.2} aria-hidden />
      </span>
      <span className="font-display text-xl font-semibold tracking-tight">AgentCart</span>
    </Link>
  );
}

const navCls = ({ isActive }: { isActive: boolean }) =>
  `rounded-lg px-3 py-2 text-sm font-medium transition-colors ${isActive ? "bg-paper-2 text-ink" : "text-ink-2 hover:text-ink"}`;

export function Layout() {
  const { user, isAuthenticated, signOut } = useAuth();
  const cart = useCart();
  const [open, setOpen] = useState(false);
  useVerifySession();
  const count = cart.data?.item_count ?? 0;

  const links = (
    <>
      <NavLink to="/products" className={navCls} onClick={() => setOpen(false)}>Shop</NavLink>
      <NavLink to="/assistant" className={navCls} onClick={() => setOpen(false)}>
        <span className="inline-flex items-center gap-1.5"><Bot size={15} aria-hidden /> Assistant</span>
      </NavLink>
      {isAuthenticated && (
        <>
          <NavLink to="/orders" className={navCls} onClick={() => setOpen(false)}>Orders</NavLink>
          <NavLink to="/preferences" className={navCls} onClick={() => setOpen(false)}>
            <span className="inline-flex items-center gap-1.5"><SlidersHorizontal size={15} aria-hidden /> Preferences</span>
          </NavLink>
        </>
      )}
    </>
  );

  return (
    <div className="flex min-h-dvh flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-lg focus:bg-ink focus:px-3 focus:py-2 focus:text-paper">Skip to content</a>
      <header className="sticky top-0 z-40 border-b border-line/80 bg-paper/85 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-7xl items-center gap-4 px-4 sm:px-6">
          <Logo />
          <nav className="ml-4 hidden items-center gap-1 md:flex" aria-label="Main">{links}</nav>
          <div className="ml-auto flex items-center gap-2">
            <Link to="/cart" className="relative rounded-lg p-2 hover:bg-paper-2" aria-label={`Cart, ${count} item${count === 1 ? "" : "s"}`}>
              <ShoppingBag size={20} aria-hidden />
              {count > 0 && (
                <span className="absolute -top-0.5 -right-0.5 grid h-5 min-w-5 place-items-center rounded-full bg-accent px-1 text-[11px] font-bold text-white" data-testid="cart-count">{count}</span>
              )}
            </Link>
            {isAuthenticated ? (
              <div className="hidden items-center gap-2 sm:flex">
                <span className="max-w-36 truncate text-sm text-muted" title={user?.email}>Hi, {user?.display_name}</span>
                <button className="btn btn-ghost btn-sm" onClick={signOut} aria-label="Sign out"><LogOut size={14} aria-hidden /> Sign out</button>
              </div>
            ) : (
              <div className="hidden items-center gap-2 sm:flex">
                <Link to="/login" className="btn btn-ghost btn-sm">Sign in</Link>
                <Link to="/register" className="btn btn-primary btn-sm">Create account</Link>
              </div>
            )}
            <button className="rounded-lg p-2 md:hidden" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-controls="mobile-nav" aria-label="Toggle menu">
              {open ? <X size={20} /> : <Menu size={20} />}
            </button>
          </div>
        </div>
        {open && (
          <nav id="mobile-nav" className="flex flex-col gap-1 border-t border-line px-4 py-3 md:hidden" aria-label="Mobile">
            {links}
            {isAuthenticated ? (
              <button className="btn btn-ghost btn-sm mt-2 self-start" onClick={signOut}>Sign out</button>
            ) : (
              <div className="mt-2 flex gap-2">
                <Link to="/login" className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>Sign in</Link>
                <Link to="/register" className="btn btn-primary btn-sm" onClick={() => setOpen(false)}>Create account</Link>
              </div>
            )}
          </nav>
        )}
      </header>
      <main id="main" className="flex-1">
        <Outlet />
      </main>
      <footer className="border-t border-line bg-paper-2/60">
        <div className="mx-auto flex max-w-7xl flex-col gap-2 px-4 py-6 text-xs text-muted sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <p>AgentCart is a portfolio demo. Catalogue prices and stock are illustrative; checkout never charges money.</p>
          <p>Recommendations are ranked by transparent rules — the AI explains, you decide.</p>
        </div>
      </footer>
    </div>
  );
}

export function RequireAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAuth();
  const loc = useLocation();
  if (!isAuthenticated) {
    return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />;
  }
  return <>{children}</>;
}

export function Container({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`mx-auto max-w-7xl px-4 py-8 sm:px-6 ${className}`}>{children}</div>;
}
