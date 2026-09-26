import { LatencyHopTable } from "./LatencyHopTable";
import { LatencyRecentView } from "./LatencyRecentView";
import {
  brokerRows, hopRows, probeRows, vpsLinkRow,
  type Broker, type PipelineView, type Probe, type Row, type VpsReport, type Wait,
} from "./latency_rows";

interface Props {
  id: string;
  title: string;
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
      max: null, n: null, state: "fail", note: remote.error });
  }
  return rows;
}

/** One checker: its hops from real signals, the live checks along its path, and the VPS. */
export function LatencyCheckerSection(p: Props) {
  const live = [...probeRows(p.probes, p.probeKeys), ...brokerRows(p.broker)];
  return (
    <section data-testid={`checker-${p.id}`} className="space-y-2 rounded border border-line bg-surface-2 p-3">
      <div>
        <h3 className="text-xs font-semibold text-ink-1">{p.title}</h3>
        <p className="text-[11px] text-ink-3">{p.intro}</p>
      </div>

      {p.waits && p.waits.length > 0 && (
        <ul className="space-y-0.5 text-[11px]">
          {p.waits.map((w) => (
            <li key={w.id} data-testid={`wait-${w.id}`}>
              <span className="text-ink-1">{w.label}: every {w.seconds} s</span>
              <span className="text-ink-3"> — {w.detail}</span>
            </li>
          ))}
        </ul>
      )}

      <h4 className="text-[11px] font-semibold text-ink-2">Measured on real signals, this machine</h4>
      <LatencyHopTable rows={hopRows(p.view)} testId={`hops-${p.id}`} />

      <h4 className="text-[11px] font-semibold text-ink-2">Live checks along this path</h4>
      {live.length > 0
        ? <LatencyHopTable rows={live} testId={`probes-${p.id}`} />
        : <p className="text-[11px] text-ink-3">Press Run check to time each connection now.</p>}

      {p.paired && (
        <>
          <h4 className="text-[11px] font-semibold text-remote">VPS</h4>
          {p.vps
            ? <LatencyHopTable rows={vpsRows(p.vps, p.vpsPipelines, p.probeKeys)} testId={`vps-${p.id}`} />
            : <p className="text-[11px] text-ink-3">Run check includes the VPS: the link, its own checks and its execution of forwarded orders.</p>}
        </>
      )}

      <LatencyRecentView view={p.view} testId={`recent-${p.id}`} />
    </section>
  );
}
