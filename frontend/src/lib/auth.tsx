import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { configureApi } from "./api";
import type { TokenResponse, User } from "./types";
import { AuthContext, type AuthState } from "./useAuth";

/*
 * Session storage: the short-lived access token (default 60 min) is kept in localStorage so a
 * refresh doesn't log the user out. Trade-off documented in docs/SECURITY.md: no cookies means no
 * CSRF surface, but an XSS bug could read the token — the app therefore never renders HTML from
 * the API (no dangerouslySetInnerHTML) and ships a strict CSP in production (infra/nginx.conf).
 */
const KEY = "agentcart.session";

interface Session {
  token: string;
  user: User;
  expiresAt: number;
}

function load(): Session | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    if (!s.token || !s.user || s.expiresAt <= Date.now()) {
      localStorage.removeItem(KEY);
      return null;
    }
    return s;
  } catch {
    return null;
  }
}

let currentToken: string | null = load()?.token ?? null;
let unauthorizedHandler: () => void = () => {};

// Configured at module load (not in an effect): child components may call the API during their
// own first effects, which React runs *before* this provider's effects.
configureApi({ getToken: () => currentToken, onUnauthorized: () => unauthorizedHandler() });

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(load);
  const [sessionExpired, setSessionExpired] = useState(false);
  const queryClient = useQueryClient();

  const signOut = useCallback(() => {
    localStorage.removeItem(KEY);
    currentToken = null;
    setSession(null);
    queryClient.removeQueries({ predicate: (q) => q.queryKey[0] !== "products" && q.queryKey[0] !== "product" && q.queryKey[0] !== "categories" });
  }, [queryClient]);

  const signIn = useCallback((res: TokenResponse) => {
    const s: Session = { token: res.access_token, user: res.user, expiresAt: Date.now() + res.expires_in * 1000 };
    localStorage.setItem(KEY, JSON.stringify(s));
    currentToken = s.token;
    setSessionExpired(false);
    setSession(s);
  }, []);

  useEffect(() => {
    unauthorizedHandler = () => {
      setSessionExpired(true);
      signOut();
    };
  }, [signOut]);

  // Log out exactly when the token expires.
  useEffect(() => {
    if (!session) return;
    const ms = session.expiresAt - Date.now();
    const t = window.setTimeout(() => {
      setSessionExpired(true);
      signOut();
    }, Math.max(0, ms));
    return () => window.clearTimeout(t);
  }, [session, signOut]);

  const value = useMemo<AuthState>(
    () => ({ user: session?.user ?? null, isAuthenticated: !!session, signIn, signOut, sessionExpired }),
    [session, signIn, signOut, sessionExpired],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
