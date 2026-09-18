import { useState, type FormEvent } from "react";
import { Lock } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { useAuth } from "@/contexts/AuthContext";

/**
 * The only thing between someone at the keyboard and the trading controls.
 *
 * A fresh install with no password is offered one-time setup instead of a
 * login form — a form that could never succeed, whatever is typed, is worse
 * than no form (review 2026-08-11, C2).
 */
export function LoginPage() {
  const { needs_setup, debug, login, setup } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const message = needs_setup ? await setup(password) : await login(username, password);
    setError(message);
    setBusy(false);
  };

  return (
    <div className="flex h-full items-center justify-center bg-surface-0">
      <form
        onSubmit={submit}
        className="w-80 rounded-lg border border-line bg-surface-1 p-6 text-center"
      >
        <Lock className="mx-auto text-accent" size={32} />
        <h1 className="mt-3 text-lg font-bold text-ink-1">FOREX Trader</h1>
        <p className="mt-1 text-xs text-ink-3">
          {needs_setup
            ? "No password is set on this install. Choose one now."
            : "Sign in to reach the dashboard."}
        </p>

        {!needs_setup && (
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="Username"
            autoComplete="username"
            aria-label="Username"
            className="mt-4 w-full rounded border border-line bg-surface-2 px-3 py-2 text-sm text-ink-1 placeholder:text-ink-3"
          />
        )}
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder={needs_setup ? "Choose a password" : "Password"}
          autoComplete={needs_setup ? "new-password" : "current-password"}
          aria-label="Password"
          className="mt-2 w-full rounded border border-line bg-surface-2 px-3 py-2 text-sm text-ink-1 placeholder:text-ink-3"
        />

        {error && (
          <p role="alert" className="mt-3 text-xs text-loss">{error}</p>
        )}
        {debug && !needs_setup && (
          <p className="mt-3 text-[11px] text-warning">
            Debug mode: the seeded development login applies.
          </p>
        )}

        <Button type="submit" className="mt-4 w-full justify-center" disabled={busy}>
          {busy ? "Working…" : needs_setup ? "Set password and continue" : "Sign in"}
        </Button>
      </form>
    </div>
  );
}
