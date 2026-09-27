import { useCallback, useState } from "react";
import { ArrowUpCircle } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { usePoll } from "@/hooks/usePoll";

interface NodeVersion { commit: string; git_version: string }

export interface VersionReport {
  local: NodeVersion;
  remote: NodeVersion | null;
  in_sync: boolean | null;
  last_update: { ok: boolean; note: string } | null;
}

function short(sha: string | undefined): string {
  return sha ? sha.slice(0, 7) : "unknown";
}

function git(v: string | undefined): string {
  return v ? ` · git ${v}` : "";
}

/** Which commit each node runs, and whether they match. An older VPS that
 *  does not report its commit is "unknown", never "out of sync". */
export function VersionLine({ report }: { report: VersionReport | null | undefined }) {
  // A reply without `local` is not a version report (an older backend, or a
  // proxy answering something else). Show nothing rather than crash the tab.
  if (!report || typeof report !== "object" || !report.local) return null;
  const { local } = report;
  const remote = report.remote && report.remote.commit ? report.remote : null;
  const inSync = typeof report.in_sync === "boolean" ? report.in_sync : null;
  return (
    <div data-testid="remote-versions" className="space-y-0.5 text-[11px]">
      <p className="text-ink-2">
        This machine <span className="num">{short(local.commit)}</span>{git(local.git_version)}
        {" · "}VPS <span className="num">{remote ? short(remote.commit) : "unknown"}</span>
        {remote ? git(remote.git_version) : ""}
      </p>
      <p data-testid="remote-sync-state"
        className={inSync === true ? "text-profit" : inSync === false ? "text-warning" : "text-ink-3"}>
        {inSync === true && "In sync: both nodes run the same commit."}
        {inSync === false && "Not in sync: the nodes run different commits."}
        {inSync === null && (remote
          ? "This machine's commit is unreadable, so sync is unknown."
          : "The VPS has not reported its commit (not connected, or an older version).")}
      </p>
      {report.last_update && !report.last_update.ok && (
        <p role="alert" className="text-loss">Last VPS update failed: {report.last_update.note}</p>
      )}
    </div>
  );
}

/**
 * Upgrade the VPS from here (owner, 2026-09-27): it pulls origin/main and
 * restarts, exactly as its own Settings > Update does. Like Restart it closes
 * nothing, but manages no position until it is back, so it asks first and
 * counts what is open.
 */
export function RemoteUpgradeSection({
  connected, openPositions,
}: { connected: boolean; openPositions: number }) {
  const versions = usePoll<VersionReport>(
    "remote/versions",
    useCallback(() => api.get<VersionReport>("/api/remote/versions"), []),
    15_000,
  );
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function upgrade() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<{ note?: string }>("/api/remote/update-vps", {});
      setNote(res.note ?? "VPS updating.");
      setOpen(false);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-1 pt-1">
      <Button
        variant="ghost"
        disabled={busy}
        disabledReason={connected ? undefined : "Not connected to the VPS."}
        onClick={() => { setError(null); setNote(null); setOpen(true); }}
      >
        <ArrowUpCircle size={13} /> Upgrade VPS
      </Button>
      {note && <p role="status" className="text-[11px] text-profit">{note}</p>}
      <VersionLine report={versions.data} />

      <DialogShell open={open} onOpenChange={setOpen} title="Upgrade the VPS">
        <div className="space-y-3 text-xs text-ink-2">
          <p>
            The VPS pulls the latest code from GitHub (origin/main), reinstalls
            its requirements, deploys the EA and restarts. It takes a few
            minutes. Nothing is closed.
          </p>
          <p className="text-ink-3">
            {openPositions > 0
              ? `${openPositions} open position${openPositions === 1 ? "" : "s"} keep their SL/TP at the broker, but the VPS manages none of them until it is back.`
              : "The VPS has no open positions."}
          </p>
          {error && <p role="alert" className="text-xs text-loss">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button disabled={busy} onClick={() => void upgrade()}>
              <ArrowUpCircle size={13} /> {busy ? "Asking the VPS…" : "Upgrade the VPS"}
            </Button>
          </div>
        </div>
      </DialogShell>
    </div>
  );
}
