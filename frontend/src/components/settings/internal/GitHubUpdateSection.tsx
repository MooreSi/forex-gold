import { useEffect } from "react";
import { Button } from "@/components/shared/Button";
import { useUpdateCheck } from "@/hooks/useUpdateCheck";

interface GitHubUpdateSectionProps {
  /** The running build's version string, from `/api/node/state`. */
  version: string;
}

/** One labelled figure in the top row. */
function Figure({ label, value, tone, testId }: {
  label: string; value: string; tone: string; testId?: string;
}) {
  return (
    <div className="flex flex-col gap-0">
      <span className="text-[10px] uppercase tracking-wide text-ink-3">{label}</span>
      <span data-testid={testId} className={`num text-sm font-semibold ${tone}`}>
        {value}
      </span>
    </div>
  );
}

/**
 * What is installed against what is on GitHub, and the button that closes the
 * gap.
 *
 * The version string alone does not answer "am I on the current build?" — it
 * only changes when someone bumps it, and this app self-updates by commit. So
 * the card shows both commits, the way the NiceGUI card did.
 *
 * **A check must look like it is happening.** It runs `git fetch`, which takes
 * as long as the network takes, and the version of this screen before
 * 2026-09-20 reloaded a resource with no visible state at all: the operator
 * pressed the button and nothing whatsoever changed on screen.
 */
export function GitHubUpdateSection({ version }: GitHubUpdateSectionProps) {
  // `false`: this card lists the commit subjects, and a plain-English summary
  // costs a paid model call. Only the popup that asks the operator to install
  // something pays for one.
  const upd = useUpdateCheck(false);
  const { check } = upd;

  useEffect(() => { void check(); }, [check]);

  const result = upd.status?.update;
  const available = result?.available === true;
  const bootstrap = result?.bootstrap === true;
  const commits = result?.commits ?? [];
  // A check that could not RUN is not an install that is up to date, and
  // `bootstrap` carries an explanation in the same field — it is the normal
  // state of an install that has never updated, not a failure.
  const failed = !bootstrap && Boolean(result?.error || upd.error);
  const reason = String(result?.error || upd.error || "");

  const status = upd.checking
    ? { text: "Checking...", tone: "text-ink-2 border-line" }
    : bootstrap
      ? { text: "Not linked", tone: "text-remote border-remote/40" }
      : failed
        ? { text: "Check failed", tone: "text-loss border-loss/40" }
        : available
          ? {
            text: `${commits.length} new commit${commits.length === 1 ? "" : "s"}`,
            tone: "text-profit border-profit/40",
          }
          : { text: "Up to date", tone: "text-ink-2 border-line" };

  const tracking = upd.status?.tracking;
  const repoUrl = String(tracking?.repo_url ?? "");
  const branch = String(tracking?.branch ?? "");
  const localSha = String(result?.local_sha ?? "");
  const remoteSha = String(result?.remote_sha ?? "");

  return (
    <section>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-xs font-semibold text-ink-1">GitHub updates</h3>
        {/* The repository this checkout's `origin` really points at, and the
            branch the check compared against — read from git, never a
            constant. They are not always the branch the app is RUNNING, and
            an install that follows one branch while running another reads as
            "up to date" forever. Saying which is how that gets noticed. */}
        {repoUrl && (
          <a
            href={repoUrl}
            target="_blank"
            rel="noreferrer"
            className="text-[11px] text-ink-3 hover:text-ink-1"
          >
            {repoUrl.replace(/^https?:\/\/(www\.)?github\.com\//, "")}
          </a>
        )}
        {branch && (
          <span data-testid="tracking-branch" className="num text-[11px] text-ink-3">
            @{branch}
          </span>
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-5">
        <Figure label="Installed version" value={`v${version}`} tone="text-accent" />
        <Figure
          label="Installed commit"
          value={localSha ? localSha.slice(0, 7) : "—"}
          tone="text-ink-1"
          testId="local-commit"
        />
        {/* Only when it differs. Two identical SHAs side by side under
            different headings reads as a discrepancy that is not there. */}
        {remoteSha && remoteSha !== localSha && (
          <Figure
            label="On GitHub"
            value={remoteSha.slice(0, 7)}
            tone="text-profit"
            testId="remote-commit"
          />
        )}
        <span
          data-testid="update-status"
          className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold ${status.tone}`}
        >
          {status.text}
        </span>
      </div>

      {available && commits.length > 0 && (
        <div className="mt-2 max-h-40 space-y-0.5 overflow-y-auto">
          {commits.map((c) => (
            <p key={c.sha} className="num text-[11px] leading-relaxed text-ink-2">
              {c.short_sha}  {c.summary}
            </p>
          ))}
        </div>
      )}

      {/* Blue-ish, not red: the button beside it is the fix, and an operator
          who reads this as a fault goes looking for one. */}
      {bootstrap && reason && (
        <p className="mt-1.5 text-[11px] text-remote">{reason}</p>
      )}
      {failed && reason && (
        <p className="mt-1.5 text-[11px] text-loss">Error: {reason}</p>
      )}

      <div className="mt-2 flex items-center gap-2">
        <Button variant="ghost" onClick={() => void check()} disabled={upd.checking}>
          {upd.checking ? "Checking..." : "Check for updates"}
        </Button>
        {available && (
          <Button variant="success" onClick={() => void upd.apply()} disabled={upd.applying}>
            {upd.applying ? "Updating..." : "Update"}
          </Button>
        )}
        {/* Named for what it does, not for what it fixes: it force-checks-out
            origin's HEAD and replaces anything changed in this folder. */}
        {bootstrap && (
          <Button onClick={() => void upd.apply()} disabled={upd.applying}>
            {upd.applying ? "Updating..." : "Update to latest"}
          </Button>
        )}
      </div>

      {upd.applying && (
        <p className="mt-1.5 text-[11px] text-warning">
          Updating — pulling the latest code and reinstalling dependencies...
        </p>
      )}
      {upd.applyError && (
        <p className="mt-1.5 text-[11px] text-loss">Update failed: {upd.applyError}</p>
      )}
      {upd.applied && (
        <p className="mt-1.5 text-[11px] text-profit">Update applied — restarting...</p>
      )}
    </section>
  );
}
