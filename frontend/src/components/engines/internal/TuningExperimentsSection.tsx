import { useCallback, useState } from "react";
import { api } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { formatMoney, formatSignedMoney } from "@/components/shared/format";
import { usePoll } from "@/hooks/usePoll";
import { asArray } from "@/lib/asArray";

export interface Experiment {
  id: number;
  param: string;
  old_value: number | null;
  new_value: number;
  hypothesis: string | null;
  summary: string | null;
  status: string;
  concurrent: number;
  baseline_n: number | null;
  baseline_mean: number | null;
  after_n: number | null;
  after_mean: number | null;
  after_sum: number | null;
  verdict: string | null;
}

export interface TuningState {
  approval_required: boolean;
  min_sample: number;
  failure_usd: number;
  running: Experiment | null;
  proposals: Experiment[];
  history: Experiment[];
}

const STATUS_LABEL: Record<string, string> = {
  kept: "Kept", rolled_back: "Rolled back", abandoned: "Abandoned (changed by hand)",
  rejected: "Rejected", superseded: "Superseded", judged: "Applied by AI",
  applied_auto: "Applied by AI, measuring",
};

function v(n: number | null | undefined): string {
  return n == null ? "—" : String(Number(n.toFixed(4)));
}

function mean(n: number | null | undefined): string {
  return n == null ? "—" : formatSignedMoney(n);
}

interface ViewProps {
  state: TuningState;
  onApprove: (id: number) => void;
  onReject: (id: number) => void;
  onSetApproval: (on: boolean) => void;
}

/**
 * Tuning experiments: every change the AI batch review makes to a Breakout
 * parameter, what it was trying, and whether it helped (docs/todo/007).
 *
 * With approval off the AI applies its changes as it always has; this only
 * shows them, and says when a batch changed several at once so no single one
 * can be credited. With approval on, changes wait here. One runs at a time; it
 * is judged after `min_sample` closed signals against the average before it,
 * and rolled back if worse or if it loses `failure_usd` first.
 */
export function TuningExperimentsView({ state, onApprove, onReject, onSetApproval }: ViewProps) {
  const [confirming, setConfirming] = useState<number | null>(null);
  const running = state.running;
  const proposals = asArray<Experiment>(state.proposals);
  const history = asArray<Experiment>(state.history);

  return (
    <section className="rounded-lg border border-line bg-surface-1 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-semibold text-ink-1">Tuning experiments</h3>
        <label className="flex items-center gap-2 text-xs text-ink-2">
          <Tooltip label="On: the AI's parameter changes wait here for you, one runs at a time, and a worse result is rolled back. Off: the AI applies them as it always has and they are only recorded.">
            <input
              type="checkbox"
              aria-label="Require my approval"
              checked={state.approval_required}
              onChange={(e) => onSetApproval(e.target.checked)}
              className="accent-accent"
            />
          </Tooltip>
          Require my approval
        </label>
        <span className="text-[11px] text-ink-3">
          Judged after {state.min_sample} closed signals; rolled back early at{" "}
          {formatMoney(-state.failure_usd)}. Not worse is kept; this is not a significance test.
        </span>
      </div>

      {running && (
        <div data-testid="tuning-running" className="mb-2 rounded border border-accent/40 bg-accent/10 p-2 text-xs text-ink-1">
          Running: <b>{running.param}</b> {v(running.old_value)} → {v(running.new_value)} —{" "}
          {running.after_n ?? 0}/{state.min_sample} signals, average {mean(running.after_mean)}{" "}
          vs {mean(running.baseline_mean)} before (net {mean(running.after_sum)})
          {running.hypothesis && <p className="mt-1 text-ink-3">{running.hypothesis}</p>}
        </div>
      )}

      {proposals.length > 0 && (
        <ul className="mb-2 space-y-1">
          {proposals.map((p) => (
            <li key={p.id} data-testid={`tuning-proposal-${p.id}`}
              className="flex flex-wrap items-center gap-2 rounded border border-line p-2 text-xs">
              <span className="text-ink-1"><b>{p.param}</b> {v(p.old_value)} → {v(p.new_value)}</span>
              <span className="flex-1 text-ink-3">{p.hypothesis}</span>
              {confirming === p.id ? (
                <>
                  <span className="text-warning">Change the live engine?</span>
                  <Button onClick={() => { setConfirming(null); onApprove(p.id); }}>Confirm</Button>
                  <Button variant="ghost" onClick={() => setConfirming(null)}>Cancel</Button>
                </>
              ) : (
                <>
                  <Button
                    disabledReason={running ? "One change at a time: wait for the running experiment to be judged." : null}
                    onClick={() => setConfirming(p.id)}>Approve</Button>
                  <Button variant="ghost" onClick={() => onReject(p.id)}>Reject</Button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {history.length > 0 ? (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-[11px] text-ink-3">
              <th className="py-1 font-normal">Change</th>
              <th className="py-1 font-normal">Outcome</th>
              <th className="py-1 font-normal">Average after / before</th>
            </tr>
          </thead>
          <tbody>
            {history.map((e) => (
              <tr key={e.id} data-testid={`tuning-history-${e.id}`}>
                <td className="py-1 text-ink-1">
                  {e.param} {v(e.old_value)} → {v(e.new_value)}
                  {e.concurrent > 1 && (
                    <span className="ml-1 text-[11px] text-warning">
                      (1 of {e.concurrent} changed together)
                    </span>
                  )}
                </td>
                <td className="py-1 text-ink-2">
                  {STATUS_LABEL[e.status] ?? e.status}
                  {e.verdict && e.status !== "abandoned" ? `: ${e.verdict.replace(/_/g, " ")}` : ""}
                </td>
                <td className="num py-1 text-ink-2">
                  {mean(e.after_mean)} / {mean(e.baseline_mean)}
                  {e.after_n != null && <span className="text-ink-3"> (n={e.after_n})</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        !running && proposals.length === 0 && (
          <p className="text-[11px] text-ink-3">No AI parameter changes recorded yet.</p>
        )
      )}
    </section>
  );
}

export function TuningExperimentsSection() {
  const poll = usePoll<TuningState>(
    "engines/breakout/tuning",
    useCallback(() => api.get<TuningState>("/api/engines/breakout/tuning"), []),
    30_000,
  );
  const [error, setError] = useState<string | null>(null);

  const act = useCallback(async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    await poll.refresh();
  }, [poll]);

  if (!poll.data) return null;
  return (
    <div className="space-y-1">
      {error && <p className="text-[11px] text-loss">{error}</p>}
      <TuningExperimentsView
        state={poll.data}
        onApprove={(id) => void act(() => api.post(`/api/engines/breakout/tuning/${id}/approve`))}
        onReject={(id) => void act(() => api.post(`/api/engines/breakout/tuning/${id}/reject`))}
        onSetApproval={(on) => void act(() =>
          api.put("/api/engines/breakout/tuning/mode", { approval_required: on }))}
      />
    </div>
  );
}
