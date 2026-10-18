import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router";
import { Notice } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/useAuth";

function safeNext(raw: string | null): string {
  // Only allow same-site relative paths (prevents open redirects via ?next=https://evil).
  return raw && raw.startsWith("/") && !raw.startsWith("//") ? raw : "/assistant";
}

function fieldErrors(err: unknown): string {
  if (err instanceof ApiError && err.status === 422) {
    const errors = (err.details.errors as { loc: string[]; msg: string }[] | undefined) ?? [];
    const fields = errors.map((e) => `${e.loc[e.loc.length - 1]}: ${e.msg.replace(/^Value error, /, "")}`);
    return fields.length ? fields.join(" · ") : err.message;
  }
  return err instanceof Error ? err.message : "Something went wrong";
}

function AuthShell({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  return (
    <div className="mx-auto flex max-w-md flex-col px-4 py-14">
      <h1 className="text-3xl font-semibold">{title}</h1>
      <p className="mt-1 text-muted">{subtitle}</p>
      <div className="card mt-6 p-6">{children}</div>
    </div>
  );
}

export function LoginPage() {
  const { signIn, isAuthenticated, sessionExpired } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const next = safeNext(params.get("next"));

  if (isAuthenticated && !busy) return <Navigate to={next} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      signIn(await api.login({ email, password }));
      navigate(next, { replace: true });
    } catch (err) {
      setError(fieldErrors(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title="Welcome back" subtitle="Sign in to chat with the assistant, manage your cart and see orders.">
      {sessionExpired && <div className="mb-4"><Notice tone="warn">Your session expired. Please sign in again.</Notice></div>}
      <form onSubmit={submit} className="space-y-4" noValidate>
        <div>
          <label className="label" htmlFor="email">Email</label>
          <input id="email" className="input" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="password">Password</label>
          <input id="password" className="input" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error && <Notice tone="danger">{error}</Notice>}
        <button className="btn btn-primary w-full" type="submit" disabled={busy || !email || !password}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
      <p className="mt-5 text-center text-sm text-muted">New here? <Link className="font-semibold text-accent" to={`/register?next=${encodeURIComponent(next)}`}>Create an account</Link></p>
    </AuthShell>
  );
}

export function RegisterPage() {
  const { signIn, isAuthenticated } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [form, setForm] = useState({ display_name: "", email: "", password: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const next = safeNext(params.get("next"));
  const weak = form.password.length > 0 && (form.password.length < 8 || !/[a-z]/i.test(form.password) || !/\d/.test(form.password));

  if (isAuthenticated && !busy) return <Navigate to={next} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (weak) return;
    setBusy(true);
    setError(null);
    try {
      signIn(await api.register(form));
      navigate(next, { replace: true });
    } catch (err) {
      setError(fieldErrors(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title="Create your account" subtitle="Free, local and private — your data stays in your own database.">
      <form onSubmit={submit} className="space-y-4" noValidate>
        <div>
          <label className="label" htmlFor="name">Name</label>
          <input id="name" className="input" autoComplete="name" required maxLength={80} value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
        </div>
        <div>
          <label className="label" htmlFor="reg-email">Email</label>
          <input id="reg-email" className="input" type="email" autoComplete="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </div>
        <div>
          <label className="label" htmlFor="reg-password">Password</label>
          <input id="reg-password" className="input" type="password" autoComplete="new-password" required minLength={8} maxLength={128} aria-describedby="pw-help" aria-invalid={weak} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          <p id="pw-help" className={`mt-1 text-xs ${weak ? "text-danger" : "text-muted"}`}>At least 8 characters, with letters and numbers.</p>
        </div>
        {error && <Notice tone="danger">{error}</Notice>}
        <button className="btn btn-primary w-full" type="submit" disabled={busy || weak || !form.email || !form.password || !form.display_name}>{busy ? "Creating account…" : "Create account"}</button>
      </form>
      <p className="mt-5 text-center text-sm text-muted">Already have an account? <Link className="font-semibold text-accent" to={`/login?next=${encodeURIComponent(next)}`}>Sign in</Link></p>
    </AuthShell>
  );
}
