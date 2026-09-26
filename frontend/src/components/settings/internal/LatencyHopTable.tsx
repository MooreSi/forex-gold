import { cn } from "@/lib/cn";
import { fmtMs, type Row } from "./latency_rows";

const NUM = "num whitespace-nowrap py-1 pr-2 text-right";

const STATE_CLASS: Record<Row["state"], string> = {
  ok: "text-ink-1",
  slow: "text-warning",
  fail: "text-loss",
  none: "text-ink-3",
};

/**
 * One table for every hop, probe and broker figure, in the order a signal
 * travels. A single measurement (a probe) fills the first column only; a hop
 * measured over many signals fills all four.
 */
export function LatencyHopTable({ rows, testId }: { rows: Row[]; testId?: string }) {
  if (rows.length === 0) return null;
  return (
    <table data-testid={testId} className="w-full text-[11px]">
      <thead>
        <tr className="text-left text-ink-3">
          <th className="py-1 pr-2 font-normal">Hop</th>
          <th className="py-1 pr-2 text-right font-normal">typical</th>
          <th className="py-1 pr-2 text-right font-normal">p90</th>
          <th className="py-1 pr-2 text-right font-normal">worst</th>
          <th className="py-1 text-right font-normal">n</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key} data-testid={`hop-${r.key}`} data-state={r.state}
            className="border-t border-line align-top">
            <td className="py-1 pr-2">
              <div className={cn(STATE_CLASS[r.state])}>{r.label}</div>
              {r.detail && <div className="text-[10px] text-ink-3">{r.detail}</div>}
              {r.note && (
                <div className={cn("text-[10px]", r.state === "fail" ? "text-loss" : "text-ink-2")}>
                  {r.note}
                </div>
              )}
            </td>
            <td className={cn(NUM, STATE_CLASS[r.state])}>{fmtMs(r.p50)}</td>
            <td className={cn(NUM, STATE_CLASS[r.state])}>{fmtMs(r.p90)}</td>
            <td className={cn(NUM, "text-ink-2")}>{fmtMs(r.max)}</td>
            <td className="num whitespace-nowrap py-1 text-right text-ink-3">{r.n ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
