import { useCallback, useState } from "react";
import { ArrowUpCircle } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { DialogShell } from "@/components/shared/DialogShell";
import { Notice } from "@/components/shared/Notice";
import { usePoll } from "@/hooks/usePoll";
import { useNoteUntilPeerReturns } from "../hooks/useNoteUntilPeerReturns";

// The fallback for a VPS that never drops the link. An upgrade reinstalls
// requirements before it restarts, so this allows for a slow one.
const NOTE_TTL_MS = 15 * 60_000;

interface NodeVersion { commit: string; git_version: string }

export interface VersionReport {
  local: NodeVersion;
  remote: NodeVersion | null;
  in_sync: boolean | null;
  last_update: { ok: boolean; note: string } | null;
  /** Why `remote` is missing: "older_build", "not_connected" or "reported".
   *  Absent from a backend older than 2026-09-28. */
  remote_reason?: string;
  /** The VPS pulled but still runs the old code. Absent before 2026-09-28. */
  restart_pending?: boolean | null;
  /** What this machine last saw on origin/main, which is what Upgrade VPS
   *  pulls. "" or absent when unknown (2026-09-29). */
  origin_commit?: string;
}

/** What to say when the VPS's commit is unknown. The two causes need
 *  different fixes, so they are named separately (2026-09-28). */
function unknownRemote(reason: string | undefined): string {
  if (reason === "older_build") {
    return "The VPS is connected but runs an older version that does not report its commit. "
      + "Update it once from the admin console or its own Settings > Update; "
      + "after that Upgrade VPS works from here.";
  }
  if (reason === "not_connected") return "Not connected to the VPS, so its commit is unknown.";
  return "The VPS has not reported its commit (not connected, or an older version).";
}

/** Why the nodes differ. Upgrade VPS pulls origin/main, so when the VPS
 *  already runs that and this machine does not, only a push closes the gap;
 *  the plain wording read as "the upgrade did not take" (2026-09-29). */
function outOfSync(report: VersionReport, remoteCommit: string | undefined): string {
  if (report.restart_pending === true) {
    return "Not in sync: the VPS has pulled new code but not restarted. Restart VPS to run it.";
  }
  const origin = report.origin_commit;
  if (origin && remoteCommit === origin && report.local.commit !== origin) {
    return "Not in sync: the VPS runs the latest commit on GitHub; this machine has "
      + "commits that are not pushed yet. Push them, then Upgrade VPS.";
  }
  if (origin && remoteCommit !== origin && report.local.commit === origin) {
    return "Not in sync: the VPS is behind GitHub. Upgrade VPS to bring it up to date.";
  }
  if (origin && remoteCommit !== origin) {
    return "Not in sync: the nodes run different commits. Upgrade VPS brings the VPS to GitHub's latest.";
  }
  return "Not in sync: the nodes run different commits.";
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
        {inSync === false && outOfSync(report, remote?.commit)}
        {inSync === null && (remote
          ? "This machine's commit is unreadable, so sync is unknown."
          : unknownRemote(report.remote_reason))}
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
  const { note, setNote, clear } = useNoteUntilPeerReturns(connected);

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
      {note && <Notice ttlMs={NOTE_TTL_MS} onDismiss={clear}>{note}</Notice>}
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
