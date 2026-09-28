import type { ReactNode } from "react";
import { Gauge, Play, RefreshCw } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { cn } from "@/lib/cn";
import {
  fmtMs, probeRows, vpsLinkRow, type CheckResult, type PipelineView, type RowState,
} from "./latency_rows";
import { STATE_BG, STATE_TEXT, STATE_WORD, endToEnd } from "./latency_viz";

interface Props {
  pipelines: Record<string, PipelineView> | undefined;
  result: CheckResult | null;
  paired: boolean;
  running: boolean;
  error: string | null;
  onRun: () => void;
}

const LIVE_KEYS = ["telegram", "loop", "db", "ea", "bridge", "tick"];

/**
 * The answer first: how long each route takes, whether every connection is
 * up, and how the broker is filling. Everything below it is the working.
 */
export function LatencyOverview(p: Props) {
  const tg = endToEnd(p.pipelines?.telegram);
  const en = endToEnd(p.pipelines?.engine);
  const live = p.result
    ? [...probeRows(p.result.local.probes, LIVE_KEYS), ...(p.result.vps ? [vpsLinkRow(p.result.vps)] : [])]
    : [];
  const count = (s: RowState) => live.filter((r) => r.state === s).length;
  const servers = Object.values(p.result?.local.broker?.servers ?? {});
  const fills = servers.reduce((a, s) => a + s.n, 0);
  const over5 = servers.reduce((a, s) => a + s.over_5s, 0);
  const busiest = [...servers].sort((a, b) => b.n - a.n)[0];

  return (
    <div className="relative overflow-hidden rounded-xl border border-line bg-surface-1 p-4">
      <div aria-hidden
        className="pointer-events-none absolute inset-0 bg-gradient-to-br from-accent/10 via-transparent to-transparent" />
      <div className="relative flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="flex size-9 items-center justify-center rounded-lg bg-accent/15 text-accent" aria-hidden>
            <Gauge size={18} />
          </span>
          <div>
            <h2 className="text-sm font-semibold text-ink-1">Signal-to-fill latency</h2>
            <p className="text-[11px] text-ink-3">Read-only: places, changes and closes nothing.</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Legend />
          <Button disabledReason={p.running ? "A check is already running." : null} onClick={p.onRun}>
            {p.running
              ? <RefreshCw size={12} className="mr-1.5 inline animate-spin" aria-hidden />
              : <Play size={12} className="mr-1.5 inline" aria-hidden />}
            {p.running ? "Checking…" : "Run check"}
          </Button>
        </div>
      </div>
      {p.error && <p className="relative mt-2 text-[11px] text-loss">{p.error}</p>}

      <div className={cn("relative mt-4 grid grid-cols-2 gap-2", p.paired ? "lg:grid-cols-5" : "lg:grid-cols-4")}>
        <Tile label="Telegram → MT5" value={fmtMs(tg.ms)} state={tg.ms == null ? "none" : tg.slow ? "slow" : "ok"}
          sub={tg.ms == null ? "not timed yet" : tg.measured ? "end to end" : `${tg.legs - tg.unmeasured} of ${tg.legs} legs timed`} />
        <Tile label="Engine → MT5" value={fmtMs(en.ms)} state={en.ms == null ? "none" : en.slow ? "slow" : "ok"}
          sub={en.ms == null ? "not timed yet" : en.measured ? "end to end" : `${en.legs - en.unmeasured} of ${en.legs} legs timed`} />
        <Tile label="Live checks"
          value={live.length ? `${count("ok")}/${live.length}` : "—"}
          state={!live.length ? "none" : count("fail") ? "fail" : count("slow") ? "slow" : "ok"}
          sub={live.length ? `${count("slow")} slow · ${count("fail")} no answer` : "press Run check"}>
          {live.length > 0 && (
            <div className="mt-1.5 flex gap-0.5">
              {live.map((r) => (
                <span key={r.key} title={`${r.label}: ${fmtMs(r.p50)}`}
                  className={cn("h-1.5 flex-1 rounded-full", STATE_BG[r.state])} />
              ))}
            </div>
          )}
        </Tile>
        <Tile label="Broker fills" value={busiest ? fmtMs(busiest.median_ms) : "—"}
          state={!busiest ? "none" : servers.some((s) => s.slow) ? "slow" : "ok"}
          sub={busiest ? `median · ${fills ? Math.round((over5 / fills) * 100) : 0}% over 5 s` : "Run check reads the MT5 logs"}>
          {fills > 0 && (
            <div className="mt-1.5 flex h-1.5 overflow-hidden rounded-full bg-profit/70"
              title={`${over5} of ${fills} fills over 5 s, last ${p.result?.local.broker?.days ?? 7} days`}>
              <span className="ml-auto h-full bg-warning" style={{ width: `${(over5 / fills) * 100}%` }} />
            </div>
          )}
        </Tile>
        {p.paired && (
          <Tile label="Mac ↔ VPS link" remote value={fmtMs(p.result?.vps?.rtt_ms)}
            state={!p.result?.vps ? "none" : p.result.vps.rtt_ms == null ? "fail"
              : p.result.vps.rtt_ms > p.result.vps.amber_ms ? "slow" : "ok"}
            sub={p.result?.vps ? "round trip" : "press Run check"} />
        )}
      </div>
    </div>
  );
}

function Tile({ label, value, sub, state, remote = false, children }: {
  label: string; value: string; sub: string; state: RowState; remote?: boolean; children?: ReactNode;
}) {
  return (
    <div className="min-w-0 rounded-lg border border-line bg-surface-2/70 px-3 py-2.5 backdrop-blur-sm">
      <div className={cn("flex items-center gap-1.5 text-[10px] uppercase tracking-wider",
        remote ? "text-remote" : "text-ink-3")}>
        <span aria-hidden className={cn("size-1.5 rounded-full", STATE_BG[state])} />
        <span className="truncate">{label}</span>
      </div>
      <div className={cn("num mt-1 text-2xl font-bold leading-none", STATE_TEXT[state])}>{value}</div>
      <div className="mt-1 truncate text-[10.5px] text-ink-3">{sub}</div>
      {children}
    </div>
  );
}

function Legend() {
  const states: RowState[] = ["ok", "slow", "fail", "none"];
  return (
    <ul className="hidden items-center gap-3 text-[10.5px] text-ink-3 md:flex">
      {states.map((s) => (
        <li key={s} className="flex items-center gap-1">
          <span aria-hidden className={cn("size-2 rounded-full", STATE_BG[s])} />{STATE_WORD[s]}
        </li>
      ))}
    </ul>
  );
}
