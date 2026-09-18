import { EmptyState } from "@/components/shared/EmptyState";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { SettingsField } from "../internal/SettingsField";

interface DomainSpec {
  path: string;
  title: string;
  blurb: string;
  fields: { key: string; label: string; secret?: boolean; hint?: string }[];
}

const DOMAINS: DomainSpec[] = [
  {
    path: "/api/settings/telegram",
    title: "Telegram",
    blurb: "The account the reader signs in as. Credentials are write-only.",
    fields: [
      { key: "api_id", label: "API ID" },
      { key: "api_hash", label: "API hash", secret: true },
      { key: "phone", label: "Phone number", hint: "Used once, for the login code." },
    ],
  },
  {
    path: "/api/settings/email",
    title: "Email alerts",
    blurb: "Where the scheduled report and alerts are sent from.",
    fields: [
      { key: "smtp_host", label: "SMTP host" },
      { key: "smtp_port", label: "SMTP port" },
      { key: "smtp_user", label: "Username" },
      { key: "smtp_password", label: "Password", secret: true },
      { key: "recipient", label: "Send to" },
    ],
  },
];

/**
 * The two outbound connections, each with its own read and its own write.
 *
 * One endpoint per domain rather than a single settings blob: `settings.py`
 * reached 3,112 lines because everything that needed a setting was added to one
 * surface, and one shared write here would put that back.
 */
export function ConnectionsTab() {
  return (
    <div className="space-y-5">
      {DOMAINS.map((domain) => (
        <DomainSection key={domain.path} domain={domain} />
      ))}
    </div>
  );
}

function DomainSection({ domain }: { domain: DomainSpec }) {
  const resource = useSettingsResource<Record<string, unknown>>(domain.path);

  if (!resource.data) {
    return (
      <EmptyState
        title={resource.error ? `Could not load ${domain.title}` : `Loading ${domain.title}`}
        hint={resource.error ?? undefined}
      />
    );
  }

  return (
    <section data-testid={`domain-${domain.title}`}>
      <h3 className="text-xs font-semibold text-ink-1">{domain.title}</h3>
      <p className="mb-2 text-[11px] text-ink-3">{domain.blurb}</p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {domain.fields.map((f) => {
          const isSet = resource.data?.[`${f.key}_set`] === true;
          return (
            <SettingsField
              key={f.key}
              label={f.label}
              version={resource.version}
              type={f.secret ? "password" : "text"}
              // A secret is never echoed back, so its field starts empty and
              // the hint says whether one is stored.
              value={f.secret ? "" : String(resource.data?.[f.key] ?? "")}
              hint={
                f.secret
                  ? isSet
                    ? "stored; leave blank to keep it"
                    : "not set"
                  : f.hint
              }
              onCommit={(v) => void resource.save({ [f.key]: v })}
            />
          );
        })}
      </div>
      {resource.error && <p role="alert" className="mt-1 text-xs text-loss">{resource.error}</p>}
    </section>
  );
}
