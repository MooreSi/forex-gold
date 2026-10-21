import { EmptyState } from "@/components/shared/EmptyState";

/** One entry of `/api/system/releases` (backend/src/api/schemas/system.py).
 *  `version` has no "v": it compares equal to the running version. */
interface Release {
  version?: string;
  title?: string;
  badge?: string;
  notes?: string[];
  [key: string]: unknown;
}

interface VersionSectionProps {
  version: string | null;
  releases: Release[];
}

export function VersionSection({ version, releases }: VersionSectionProps) {
  if (releases.length === 0) {
    return <EmptyState title="No release notes are bundled with this build" />;
  }
  return (
    <div className="space-y-4">
      <p className="text-xs text-ink-3">
        Running <span className="num text-ink-1">{version ?? "unknown"}</span>
      </p>
      {releases.map((r, i) => (
        <section key={String(r.version ?? i)}>
          <h3 className="flex flex-wrap items-baseline gap-2">
            <span className="num text-sm font-semibold text-accent">
              {r.version ? `v${String(r.version)}` : "—"}
            </span>
            {r.title && <span className="text-xs font-medium text-ink-1">{String(r.title)}</span>}
            {r.badge && (
              <span className="rounded border border-line px-1.5 text-[10px] text-ink-3">
                {String(r.badge)}
              </span>
            )}
            {version !== null && String(r.version) === version && (
              <span className="rounded border border-profit/40 bg-profit/10 px-1.5 text-[10px] text-profit">
                running
              </span>
            )}
          </h3>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-ink-2">
            {(r.notes ?? []).map((n, j) => (
              <li key={j}>{n}</li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
