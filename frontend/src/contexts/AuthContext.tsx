import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from "react";
import { api, onUnauthenticated } from "@/api/client";
import type { SessionState } from "@/api/types";

interface AuthValue extends SessionState {
  ready: boolean;
  login: (username: string, password: string) => Promise<string | null>;
  setup: (password: string) => Promise<string | null>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

const UNKNOWN: SessionState = {
  authenticated: false,
  needs_setup: false,
  auto_login: false,
  debug: false,
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionState>(UNKNOWN);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setSession(await api.get<SessionState>("/api/auth/session"));
    } catch {
      // An unreachable session endpoint is not permission to assume a session.
      setSession(UNKNOWN);
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(
    // Any 401 from any call, anywhere, drops us back to signed-out. One place,
    // so no call site can forget.
    () => onUnauthenticated(() => setSession((s) => ({ ...s, authenticated: false }))),
    [],
  );

  const login = useCallback(async (username: string, password: string) => {
    const r = await api.post<{ ok: boolean; message?: string }>("/api/auth/login", {
      username, password,
    });
    if (!r.ok) return r.message ?? "Incorrect username or password";
    await refresh();
    return null;
  }, [refresh]);

  const setup = useCallback(async (password: string) => {
    const r = await api.post<{ ok: boolean; message?: string }>("/api/auth/setup", { password });
    if (!r.ok) return r.message ?? "Could not set a password";
    await refresh();
    return null;
  }, [refresh]);

  const logout = useCallback(async () => {
    await api.post("/api/auth/logout");
    await refresh();
  }, [refresh]);

  const value = useMemo<AuthValue>(
    () => ({ ...session, ready, login, setup, logout, refresh }),
    [session, ready, login, setup, logout, refresh],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside an AuthProvider");
  return ctx;
}
