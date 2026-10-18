import { createContext, useContext, useEffect } from "react";
import { api, ApiError } from "./api";
import type { TokenResponse, User } from "./types";

export interface AuthState {
  user: User | null;
  isAuthenticated: boolean;
  signIn: (res: TokenResponse) => void;
  signOut: () => void;
  sessionExpired: boolean;
}

export const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

/** Validates a stored session against the server once on start-up. */
export function useVerifySession() {
  const { isAuthenticated, signOut } = useAuth();
  useEffect(() => {
    if (!isAuthenticated) return;
    api.me().catch((err) => {
      // Only a rejected token ends the session; a flaky network must not log the user out.
      if (err instanceof ApiError && err.status === 401) signOut();
    });
  }, [isAuthenticated, signOut]);
}
