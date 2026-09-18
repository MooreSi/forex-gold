import { EmptyState } from "@/components/shared/EmptyState";

interface Release {
  version?: string;
  date?: string;
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
          <h3 className="flex items-baseline gap-2">
            <span className="num text-sm font-semibold text-accent">
              {String(r.version ?? "—")}
            </span>
            {r.date && <span className="text-[11px] text-ink-3">{String(r.date)}</span>}
            {String(r.version) === version && (
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
