import { Globe } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";
import { asArray, asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

interface Fit {
  ts: number;
  n: number;
  auc_base: number | null;
  auc_xasset: number | null;
  installed: string;
  per_peer: Record<string, { auc_z60: number | null; n: number }>;
}

const W = 520;
const H = 110;
const PAD = 4;

// Theme tokens (index.css), not hex: a new colour is a token change.
const COLOURS = [1, 2, 3, 4, 5, 6, 7].map((i) => `var(--color-series-${i})`);
const BASE = "var(--color-ink-3)";
const WITH = "var(--color-accent)";

const PEER_NAMES: Record<string, string> = {
  XAGUSD: "Silver", XPTUSD: "Platinum", USDX: "Dollar index", USDJPY: "USDJPY",
  SP500: "S&P 500", VIX: "VIX", USOUSD: "Oil",
};

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function fixed(v: unknown, dp = 3): string {
  const n = num(v);
  return n == null ? "—" : n.toFixed(dp);
}

/** An SVG path through the points, broken wherever a value is missing. A gap
 *  drawn as a line to zero would claim a measurement nobody made. */
function path(values: (number | null)[], lo: number, hi: number): string {
  const n = values.length;
  let d = "";
  let pen = false;
  values.forEach((v, i) => {
    if (v == null) {
      pen = false;
      return;
    }
    const x = n <= 1 ? W / 2 : PAD + (i * (W - 2 * PAD)) / (n - 1);
    const y = PAD + ((hi - v) * (H - 2 * PAD)) / (hi - lo);
    d += `${pen ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)} `;
    pen = true;
  });
  return d.trim();
}

function yOf(v: number, lo: number, hi: number): number {
  return PAD + ((hi - v) * (H - 2 * PAD)) / (hi - lo);
}

/**
 * Other markets against gold. docs/todo/reversal-engine/230.
 *
 * Two different questions, two charts, and they must not be confused:
 *
 * 1. **How closely does each market move with gold?** Each peer's six-hour
 *    correlation with gold, averaged per day. A strong correlation is NOT
 *    evidence it helps: silver moving with gold says nothing about whether
 *    a gold entry works.
 * 2. **Does knowing it help the meta-labeller?** Out-of-sample AUC with and
 *    without the peers, on the same signals, at every refit. Only this one
 *    measures impact, and it is measured whatever the toggle says.
 */
export function CrossAssetSection({ data }: { data: unknown }) {
  const report = asObject(data);
  const peers = asArray<string>(report["peers"]);
  const coverage = asObject(report["coverage"]);
  const days = asArray<Record<string, unknown>>(report["daily_corr"]);
  const fits = asArray<Fit>(report["fits"]);
  const measured = num(coverage["measured"]) ?? 0;
  const missing = num(coverage["missing"]) ?? 0;
  const latest = fits.length ? fits[fits.length - 1] : null;

  const header = (
    <div className="flex items-center gap-2">
      <Globe size={14} className="text-accent" aria-hidden />
      <h3 className="text-xs font-semibold text-ink-1">Other markets against gold</h3>
    </div>
  );

  if (!peers.length || (measured === 0 && !days.length)) {
    return (
      <div className="space-y-2">
        {header}
        <EmptyState title="Other markets are being measured"
          hint="Each signal is measured against silver, platinum, the dollar, USDJPY, the S&P 500, VIX and oil. History fills in about one day per minute." />
      </div>
    );
  }

  const delta = latest && num(latest.auc_base) != null && num(latest.auc_xasset) != null
    ? (latest.auc_xasset as number) - (latest.auc_base as number) : null;

  return (
    <div className="space-y-3">
      {header}
      <p data-testid="xasset-coverage" className="text-[11px] text-ink-3">
        {measured} signals measured{missing > 0 ? `, ${missing} still to do` : ""}.
      </p>

      <div>
        <p className="mb-1 text-[11px] text-ink-2">
          How closely each market moved with gold, by day (1 = together, -1 = opposite)
        </p>
        <svg data-testid="xasset-corr-chart" viewBox={`0 0 ${W} ${H}`}
          className="h-28 w-full rounded border border-line bg-surface-1" role="img"
          aria-label="Daily correlation of each market with gold">
          <line x1={0} x2={W} y1={yOf(0, -1, 1)} y2={yOf(0, -1, 1)}
            stroke="currentColor" strokeDasharray="3 3" className="text-ink-3" />
          {peers.map((p, i) => (
            <path key={p} data-testid={`xasset-corr-line-${p}`} fill="none"
              stroke={COLOURS[i % COLOURS.length]} strokeWidth={1.5}
              d={path(days.map((d) => num(d[p])), -1, 1)} />
          ))}
        </svg>
        <div className="mt-1 flex flex-wrap gap-3 text-[10px] text-ink-3">
          {peers.map((p, i) => (
            <span key={p} data-testid={`xasset-legend-${p}`} className="flex items-center gap-1">
              <span className="inline-block h-0.5 w-3" style={{ background: COLOURS[i % COLOURS.length] }} />
              {PEER_NAMES[p] ?? p}
            </span>
          ))}
        </div>
      </div>

      <div>
        <p className="mb-1 text-[11px] text-ink-2">
          Does it help? Meta-labeller accuracy on unseen signals at each refit (0.5 = coin flip)
        </p>
        {fits.length === 0 ? (
          <p className="text-[11px] text-ink-3">No refit has had measured signals yet.</p>
        ) : (
          <>
            <svg data-testid="xasset-auc-chart" viewBox={`0 0 ${W} ${H}`}
              className="h-28 w-full rounded border border-line bg-surface-1" role="img"
              aria-label="Meta-labeller AUC with and without other markets">
              {[0.5, 0.55].map((ref) => (
                <line key={ref} x1={0} x2={W} y1={yOf(ref, 0.4, 0.7)} y2={yOf(ref, 0.4, 0.7)}
                  stroke="currentColor" strokeDasharray="3 3" className="text-ink-3" />
              ))}
              <path data-testid="xasset-auc-base" fill="none" stroke={BASE} strokeWidth={1.5}
                d={path(fits.map((f) => num(f.auc_base)), 0.4, 0.7)} />
              <path data-testid="xasset-auc-xasset" fill="none" stroke={WITH} strokeWidth={1.5}
                d={path(fits.map((f) => num(f.auc_xasset)), 0.4, 0.7)} />
            </svg>
            <div className="mt-1 flex gap-3 text-[10px] text-ink-3">
              <span className="flex items-center gap-1">
                <span className="inline-block h-0.5 w-3" style={{ background: BASE }} />without</span>
              <span className="flex items-center gap-1">
                <span className="inline-block h-0.5 w-3" style={{ background: WITH }} />with other markets</span>
              <span>dashed: 0.50 coin flip, 0.55 the bar to arm</span>
            </div>
          </>
        )}
        {latest && (
          <p data-testid="xasset-latest" className="mt-1 text-[11px] text-ink-2">
            Latest refit, {latest.n} signals: <span className="num">{fixed(latest.auc_base)}</span>{" "}
            without, <span className="num">{fixed(latest.auc_xasset)}</span> with (
            <span className={cn("num font-semibold",
              delta == null ? "text-ink-3" : delta > 0 ? "text-profit" : "text-loss")}>
              {delta == null ? "—" : `${delta > 0 ? "+" : ""}${delta.toFixed(3)}`}
            </span>
            ). The model in use: {latest.installed === "xasset" ? "with other markets" : "without"}.
          </p>
        )}
      </div>

      {latest && (
        <table className="w-full text-left text-[11px]">
          <thead className="text-ink-3">
            <tr>
              <th className="px-2 py-1 font-normal">Market</th>
              <th className="px-2 py-1 font-normal">Its last-hour move alone, AUC</th>
              <th className="px-2 py-1 font-normal">Signals</th>
            </tr>
          </thead>
          <tbody className="num">
            {peers.map((p) => {
              const s = asObject(latest.per_peer?.[p]);
              return (
                <tr key={p} data-testid={`xasset-peer-${p}`} className="border-t border-line">
                  <td className="px-2 py-1 font-sans text-ink-1">{PEER_NAMES[p] ?? p}</td>
                  <td className="px-2 py-1">{fixed(s["auc_z60"])}</td>
                  <td className="px-2 py-1">{num(s["n"]) ?? 0}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
