import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import { api, ApiError, onUnauthenticated } from "@/api/client";
import type { SessionState } from "@/api/types";

interface AuthValue extends SessionState {
  ready: boolean;
  /** False while the backend cannot be reached at all. Not the same as
   *  signed out — see `refresh`. */
  reachable: boolean;
  login: (username: string, password: string) => Promise<string | null>;
  setup: (password: string) => Promise<string | null>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

/** How often to re-check a backend that is not answering. */
const RETRY_MS = 1500;

const UNKNOWN: SessionState = {
  authenticated: false,
  needs_setup: false,
  auto_login: false,
  debug: false,
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionState>(UNKNOWN);
  const [ready, setReady] = useState(false);
  const [reachable, setReachable] = useState(true);
  // Whether the backend has been unreachable since the last good read, so a
  // reconnect can tell a restart from an ordinary first load.
  const wasAway = useRef(false);

  const refresh = useCallback(async () => {
    try {
      setSession(await api.get<SessionState>("/api/auth/session"));
      setReachable(true);
      // The backend went away and has come back: that is a restart, and the
      // running page is now the old build talking to the new server. Reload
      // rather than leaving the operator to press it themselves.
      if (wasAway.current) {
        wasAway.current = false;
        window.location.reload();
      }
    } catch (e) {
      if (e instanceof ApiError) {
        // The server ANSWERED. "Not authenticated" is an answer, and the
        // login screen is the right response to it.
        setSession(UNKNOWN);
        setReachable(true);
      } else {
        // Could not reach it at all. That is evidence about the network, not
        // about this operator's session — treating it as "signed out" is what
        // put the login form in front of an install with auto-login on, every
        // time the app restarted.
        wasAway.current = true;
        setReachable(false);
      }
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Keep trying while it is away, so the page comes back on its own.
  useEffect(() => {
    if (reachable) return;
    const id = setInterval(() => void refresh(), RETRY_MS);
    return () => clearInterval(id);
  }, [reachable, refresh]);

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
    () => ({ ...session, ready, reachable, login, setup, logout, refresh }),
    [session, ready, reachable, login, setup, logout, refresh],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside an AuthProvider");
  return ctx;
}
