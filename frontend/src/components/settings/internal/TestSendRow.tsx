import { useState } from "react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";

interface TestSendRowProps {
  actions: { label: string; path: string; body?: unknown }[];
}

/**
 * The buttons that prove a connection works, and the result of the last press.
 *
 * **Each of these sends something.** They POST for that reason, and the row
 * shows exactly one outcome at a time: two results side by side read as one
 * result, and an operator who sees a stale green "Sent" beside a fresh failure
 * will believe the wrong one.
 *
 * A refusal's message is shown verbatim. The backend is where the SMTP
 * translation lives, so "Microsoft rejected the login: SMTP AUTH is disabled"
 * with its five-step fix arrives here as text and must not be summarised into
 * "Failed" on the way to the screen.
 */
export function TestSendRow({ actions }: TestSendRowProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  async function run(action: TestSendRowProps["actions"][number]) {
    setBusy(action.label);
    setResult(null);
    try {
      const res = await api.post<{ sent?: boolean; to?: string }>(
        action.path, action.body ?? {},
      );
      setResult({ ok: true, text: res.to ? `Sent to ${res.to}` : "Sent" });
    } catch (e) {
      setResult({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mt-3">
      <div className="flex flex-wrap gap-2">
        {actions.map((a) => (
          <Button
            key={a.label}
            variant="ghost"
            disabled={busy !== null}
            onClick={() => void run(a)}
          >
            {busy === a.label ? "Sending…" : a.label}
          </Button>
        ))}
      </div>
      {result && (
        <p
          role={result.ok ? "status" : "alert"}
          className={`mt-2 whitespace-pre-line text-[11px] ${
            result.ok ? "text-profit" : "text-loss"
          }`}
        >
          {result.text}
        </p>
      )}
    </div>
  );
}
