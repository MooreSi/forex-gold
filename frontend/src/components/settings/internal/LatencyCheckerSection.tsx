import type { ReactNode } from "react";
import { ChevronRight, Clock, Cpu, Radar, Server } from "lucide-react";
import { cn } from "@/lib/cn";
import { LatencyHopTable } from "./LatencyHopTable";
import { LatencyPipeline } from "./LatencyPipeline";
import { LatencyRecentView } from "./LatencyRecentView";
import {
  brokerRows, fmtMs, hopRows, probeRows, vpsLinkRow,
  type Broker, type PipelineView, type Probe, type Row, type VpsReport, type Wait,
} from "./latency_rows";
import { STATE_BG, STATE_TEXT, endToEnd } from "./latency_viz";

interface Props {
  id: string;
  index: number;
  title: string;
  /** The stations along the route, for the header's path. */
  path: string[];
  intro: string;
  view: PipelineView | undefined;
  waits?: Wait[];
  probes?: Record<string, Probe>;
  probeKeys: string[];
  broker?: Broker;
  paired: boolean;
  vps: VpsReport | null | undefined;
  /** The VPS's own traces that belong to this checker. */
  vpsPipelines: string[];
}

/** Only a VPS pipeline that has seen a signal: an empty one is not this node's job. */
function vpsRows(vps: VpsReport, pipelines: string[], probeKeys: string[]): Row[] {
  const remote = vps.remote;
  const rows = [vpsLinkRow(vps)];
  if (!remote) return rows;
  for (const p of pipelines) {
    const view = remote.pipelines?.[p];
    if (view?.hops.some((h) => h.stats?.n != null)) rows.push(...hopRows(view, "VPS: "));
  }
  rows.push(...probeRows(remote.probes, probeKeys, "VPS: "));
  rows.push(...brokerRows(remote.broker, "VPS: "));
  if (remote.error) {
    rows.push({ key: "vps-error", label: "VPS report", detail: "", p50: null, p90: null,
      max: null, n: null, state: "fail", note: remote.error, kind: "probe", amber: null });
  }
  return rows;
}

/** One checker: the route drawn, its hops from real signals, the live checks, and the VPS. */
export function LatencyCheckerSection(p: Props) {
  const live = [...probeRows(p.probes, p.probeKeys), ...brokerRows(p.broker)];
  const e2e = endToEnd(p.view);
  return (
    <section data-testid={`checker-${p.id}`} aria-label={p.title}
      className="overflow-hidden rounded-xl border border-line bg-surface-1 shadow-[0_1px_2px_rgba(0,0,0,0.12)]">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line/70
                         bg-gradient-to-r from-accent/8 to-transparent px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <span className="num flex size-8 shrink-0 items-center justify-center rounded-lg bg-accent/15
                           text-sm font-bold text-accent">{p.index}</span>
          <div className="min-w-0">
            <h3 className="flex flex-wrap items-center gap-1 text-[13px] font-semibold text-ink-1">
              {p.path.map((s, i) => (
                <span key={s} className="flex items-center gap-1">
                  {i > 0 && <ChevronRight size={12} className="text-ink-3" aria-hidden />}{s}
                </span>
              ))}
            </h3>
            <p className="text-[11px] text-ink-3">{p.intro}</p>
          </div>
        </div>
        <div className="text-right" title={e2e.measured
          ? "Measured end to end on real signals."
          : "The sum of the typical time of each leg that has been timed. Legs not yet timed are not in it."}>
          <div className={cn("num text-xl font-bold leading-none",
            e2e.ms == null ? "text-ink-3" : STATE_TEXT[e2e.slow ? "slow" : "ok"])}>
            {fmtMs(e2e.ms)}
          </div>
          <div className="mt-0.5 text-[10px] text-ink-3">
            {e2e.measured ? "typical, end to end"
              : e2e.ms == null ? "not timed yet"
              : `typical · ${e2e.legs - e2e.unmeasured} of ${e2e.legs} legs timed`}
          </div>
        </div>
      </header>

      <div className="space-y-4 p-4">
        <LatencyPipeline view={p.view} />

        {p.waits && p.waits.length > 0 && (
          <ul className="flex flex-wrap gap-1.5">
            {p.waits.map((w) => (
              <li key={w.id} data-testid={`wait-${w.id}`} title={w.detail}
                className="flex items-center gap-1.5 rounded-full border border-line bg-surface-2 px-2.5 py-1 text-[11px]">
                <Clock size={11} className="text-accent" aria-hidden />
                <span className="text-ink-2">{w.label}</span>
                <span className="num font-semibold text-ink-1">every {w.seconds} s</span>
              </li>
            ))}
          </ul>
        )}

        <Block icon={<Radar size={12} />} title="Measured on real signals">
          <LatencyHopTable rows={hopRows(p.view)} testId={`hops-${p.id}`} />
        </Block>

        <Block icon={<Cpu size={12} />} title="Live checks">
          {live.length > 0
            ? <LatencyHopTable rows={live} testId={`probes-${p.id}`} />
            : <Hint>Press Run check to time each connection now.</Hint>}
        </Block>

        {p.paired && (
          <Block icon={<Server size={12} />} title="VPS" remote>
            {p.vps
              ? <VpsFold rows={vpsRows(p.vps, p.vpsPipelines, p.probeKeys)} testId={`vps-${p.id}`} />
              : <Hint>Run check includes the VPS: the link, its own checks and its forwarded orders.</Hint>}
          </Block>
        )}

        <LatencyRecentView view={p.view} testId={`recent-${p.id}`} />
      </div>
    </section>
  );
}

function Block({ icon, title, remote = false, children }: {
  icon: ReactNode; title: string; remote?: boolean; children: ReactNode;
}) {
  return (
    <div className="space-y-2">
      <div className={cn("flex items-center gap-1.5", remote ? "text-remote" : "text-ink-2")}>
        <span aria-hidden>{icon}</span>
        <h4 className="text-[11px] font-semibold uppercase tracking-wider">{title}</h4>
        <span aria-hidden className="h-px flex-1 bg-line/70" />
      </div>
      {children}
    </div>
  );
}

/**
 * The VPS's rows folded to one line: the link time and a dot per check. The
 * same node serves both checkers, so opened in full twice it doubled the
 * page; the fold keeps a slow or dead check visible without the list.
 */
function VpsFold({ rows, testId }: { rows: Row[]; testId: string }) {
  const link = rows.find((r) => r.key === "vps-link");
  const bad = rows.filter((r) => r.state === "slow" || r.state === "fail").length;
  return (
    <details className="group rounded-lg border border-remote/30 bg-remote/5">
      <summary className="flex cursor-pointer select-none flex-wrap items-center gap-3 px-3 py-2 text-[11px]">
        <span className="text-ink-2">Link <span className={cn("num font-semibold",
          STATE_TEXT[link?.state ?? "none"])}>{fmtMs(link?.p50)}</span></span>
        <span className="flex items-center gap-0.5" aria-hidden>
          {rows.map((r) => <span key={r.key} className={cn("size-2 rounded-full", STATE_BG[r.state])} />)}
        </span>
        <span className={bad ? "text-warning" : "text-ink-3"}>
          {rows.length} readings{bad ? ` · ${bad} slow or down` : " · all healthy"}
        </span>
        <span className="ml-auto text-ink-3 group-open:hidden">show</span>
        <span className="ml-auto hidden text-ink-3 group-open:inline">hide</span>
      </summary>
      <div className="border-t border-remote/20 p-3">
        <LatencyHopTable rows={rows} testId={testId} stripPrefix="VPS: " />
      </div>
    </details>
  );
}

function Hint({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-dashed border-line px-3 py-2 text-[11px] text-ink-3">{children}</p>
  );
}
