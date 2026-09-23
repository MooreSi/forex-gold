import { useState } from "react";
import { Dices } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney, pnlColour } from "@/components/shared/format";
import { asArray, asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

interface Group {
  name: string;
  n: number;
  expected_win_pct: number | null;
  actual_win_pct: number | null;
  excess_pct: number | null;
  z: number | null;
  mean_net: number | null;
  verdict: string;
}

const GROUPINGS: [string, string][] = [
  ["level_type", "Level type"], ["session", "Session"],
  ["bias", "Trend bias"], ["hour", "Hour (UTC)"],
];

const VERDICT_TONE: Record<string, string> = {
  "beats chance": "text-profit",
  "worse than chance": "text-loss",
};

function pct(v: number | null | undefined): string {
  return typeof v === "number" ? `${v.toFixed(1)}%` : "—";
}

function fixed(v: unknown, dp: number): string {
  // A missing AUC or z is "never measured", and 0.000 would read as a
  // specific, terrible result -- ModelSection's rule.
  return typeof v === "number" ? v.toFixed(dp) : "—";
}

/**
 * Does the engine beat chance? docs/todo/reversal-engine/220.
 *
 * A no-edge entry between a stop SL away and a target TP away wins
 * SL / (SL + TP) of the time. That is the bar, not 50%: a 75% win rate with
 * a stop three times the target is exactly what a coin would score. The
 * verdict needs z of 3 or more because ~40 groups are tested at once.
 *
 * Executed trades are shown apart and never given a verdict: the EA template
 * closes them at its own stop and target, not the engine's, so the chance
 * rate they are compared to here is the wrong one.
 */
export function BenchmarkSection({ benchmark }: { benchmark: unknown }) {
  const [windowKey, setWindowKey] = useState<"all" | "recent">("all");
  const [grouping, setGrouping] = useState("level_type");

  const report = asObject(benchmark);
  const win = asObject(report[windowKey]);
  const overall = asObject(win["overall"]) as Partial<Group>;
  const executed = asObject(win["executed"]) as Partial<Group>;
  const ml = asObject(win["ml_auc"]);
  const rows = asArray<Group>(asObject(win["groups"])[grouping]);
  const zBar = typeof report["z_bar"] === "number" ? report["z_bar"] : 3;

  const header = (
    <div className="flex flex-wrap items-center gap-2">
      <Dices size={14} className="text-accent" aria-hidden />
      <h3 className="text-xs font-semibold text-ink-1">Does it beat chance?</h3>
      <div className="ml-auto flex gap-1">
        {([["all", "All history"], ["recent", "Last 14 days"]] as const).map(([k, label]) => (
          <button key={k} type="button" onClick={() => setWindowKey(k)}
            className={cn("rounded px-2 py-0.5 text-[11px]",
              windowKey === k ? "bg-accent/15 text-accent" : "text-ink-3 hover:text-ink-1")}>
            {label}
          </button>
        ))}
      </div>
    </div>
  );

  if (!overall.n) {
    return (
      <div className="space-y-2">
        {header}
        <EmptyState title="No closed signals to score yet"
          hint="Each closed signal is compared to the win rate its own stop and target would give with no edge." />
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {header}
      <p className="text-[11px] text-ink-3">
        With no edge, a trade wins stop ÷ (stop + target) of the time. Beating
        that is the only evidence of skill; a high win rate alone is not.
        Verdicts need z ≥ {zBar} and 100 signals.
      </p>

      <p data-testid="benchmark-overall" className="text-[11px] text-ink-2">
        {overall.n} simulated signals: chance says{" "}
        <span className="num font-semibold">{pct(overall.expected_win_pct)}</span>, the
        engine won <span className="num font-semibold">{pct(overall.actual_win_pct)}</span>{" "}
        (z <span className="num">{fixed(overall.z, 2)}</span>) —{" "}
        <span className={cn("font-semibold", VERDICT_TONE[overall.verdict ?? ""] ?? "text-ink-1")}>
          {overall.verdict}
        </span>
        . Mean net{" "}
        <span className={cn("num", pnlColour(overall.mean_net ?? null))}>
          {formatMoney(overall.mean_net ?? null)}
        </span>{" "}per signal.
      </p>

      <p data-testid="benchmark-ml" className="text-[11px] text-ink-2">
        ML gate: its score ranked winners above losers at AUC{" "}
        <span className="num font-semibold">{fixed(ml["auc"], 3)}</span> over{" "}
        <span className="num">{typeof ml["n"] === "number" ? ml["n"] : 0}</span> signals
        (0.5 is a coin flip).
      </p>

      {typeof executed.n === "number" && executed.n > 0 && (
        <p data-testid="benchmark-executed" className="text-[11px] text-ink-3">
          {executed.n} executed trades won {pct(executed.actual_win_pct)}. Not scored:
          the EA template closes them at its own stop and target, so the
          engine's geometry is the wrong chance rate for them.
        </p>
      )}

      <div className="flex flex-wrap gap-1">
        {GROUPINGS.map(([key, label]) => (
          <button key={key} type="button" onClick={() => setGrouping(key)}
            className={cn("rounded px-2 py-0.5 text-[11px]",
              grouping === key ? "bg-accent/15 text-accent" : "text-ink-3 hover:text-ink-1")}>
            {label}
          </button>
        ))}
      </div>

      <table data-testid="benchmark-table" className="w-full text-left text-[11px]">
        <thead className="text-ink-3">
          <tr>
            {["Group", "Signals", "Chance", "Actual", "z", "Mean net", "Verdict"].map((h) => (
              <th key={h} className="px-2 py-1 font-normal">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody className="num">
          {rows.map((r) => (
            <tr key={r.name} data-testid={`benchmark-row-${r.name}`} className="border-t border-line">
              <td className="px-2 py-1 text-ink-1">{r.name}</td>
              <td className="px-2 py-1">{r.n}</td>
              <td className="px-2 py-1">{pct(r.expected_win_pct)}</td>
              <td className="px-2 py-1">{pct(r.actual_win_pct)}</td>
              <td className="px-2 py-1">{fixed(r.z, 2)}</td>
              <td className={cn("px-2 py-1", pnlColour(r.mean_net))}>{formatMoney(r.mean_net)}</td>
              <td className={cn("px-2 py-1 font-sans", VERDICT_TONE[r.verdict] ?? "text-ink-3")}>
                {r.verdict}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
