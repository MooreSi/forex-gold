import { useCallback, useEffect, useState } from "react";
import { GitMerge } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { asObject } from "@/lib/asArray";

interface Verdict {
  use?: boolean; a?: number | null; b?: number | null;
  n_new?: number; reason?: string; metric?: string;
}
interface MergeStatus {
  running?: boolean; stage?: string; error?: string | null;
  result?: { local?: Record<string, Verdict>; remote?: Record<string, Verdict> } | null;
}

const POLL_MS = 2_000;
const MODELS: [string, string][] = [["re", "Reversal"], ["bo", "Breakout"], ["pro", "Pro model"]];

function num(v: unknown): string {
  return typeof v === "number" ? v.toFixed(3) : "—";
}

function VerdictLine({ node, model, v }: { node: string; model: string; v?: Verdict }) {
  if (!v) return null;
  return (
    <li>
      <span className="text-ink-1">{node}, {model}:</span>{" "}
      <span className={v.use ? "text-profit" : "text-ink-3"}>
        {v.use ? "Learns from the merged history" : "Kept apart"}
      </span>
      {" "}({v.n_new ?? 0} new rows; own {num(v.a)}, merged {num(v.b)}). {v.reason}
    </li>
  );
}

/**
 * Merge training data with the VPS, both ways (docs/todo/026).
 *
 * Each node keeps the other's labelled rows apart from its own and trains on
 * them only when a model with them scores at least as well on the newest
 * signals. A kept merge changes what the Reversal gate lets through on the
 * node that trades, so it never starts on one press.
 */
export function RemoteMlMergeSection({ connected }: { connected: boolean }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<MergeStatus>({});

  const load = useCallback(async () => {
    try {
      setStatus(asObject<MergeStatus>(await api.get<MergeStatus>("/api/remote/ml-merge")));
    } catch {
      // The section still offers the button; the next poll tries again.
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!status.running) return;
    const id = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(id);
  }, [status.running, load]);

  async function start() {
    setError(null);
    try {
      await api.post("/api/remote/ml-merge", {});
      setOpen(false);
      setStatus({ running: true, stage: "starting" });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  const result = status.result;
  return (
    <div className="mt-3 border-t border-line pt-2">
      <Button
        variant="ghost"
        disabled={Boolean(status.running)}
        disabledReason={connected ? undefined : "Not connected to the VPS."}
        onClick={() => { setError(null); setOpen(true); }}
      >
        <GitMerge size={13} /> Merge training data
      </Button>
      {status.running && (
        <p className="mt-1 text-[11px] text-ink-3">Merging: {status.stage || "working"}…</p>
      )}
      {status.error && <p role="alert" className="mt-1 text-[11px] text-loss">{status.error}</p>}
      {result && (
        <ul className="mt-1 space-y-0.5 text-[11px] text-ink-2">
          {MODELS.map(([k, label]) => (
            <VerdictLine key={`l-${k}`} node="This Mac" model={label} v={result.local?.[k]} />
          ))}
          {MODELS.map(([k, label]) => (
            <VerdictLine key={`r-${k}`} node="VPS" model={label} v={result.remote?.[k]} />
          ))}
        </ul>
      )}

      <DialogShell open={open} onOpenChange={setOpen} title="Merge training data">
        <div className="space-y-3 text-xs text-ink-2">
          <p>
            Copies each node's labelled signals to the other: Reversal, Breakout
            and the pro corpus. They are kept apart from each node's own
            history, its panels and its ledger, and a repeat adds nothing twice.
          </p>
          <p>
            Each node then trains a model with and without them and scores both
            on the newest signals. It learns from the merged history only if it
            scores at least as well, and says so below.
          </p>
          <p className="text-ink-3">
            If it does, the Reversal model on the VPS retrains at once, which can
            change which signals its gate lets through.
          </p>
          {error && <p role="alert" className="text-xs text-loss">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button onClick={() => void start()}>
              <GitMerge size={13} /> Merge both ways
            </Button>
          </div>
        </div>
      </DialogShell>
    </div>
  );
}
