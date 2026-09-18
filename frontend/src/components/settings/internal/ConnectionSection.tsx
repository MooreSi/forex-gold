import type { ReactNode } from "react";
import { EmptyState } from "@/components/shared/EmptyState";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { SettingsField } from "./SettingsField";
import { SettingsToggle } from "./SettingsToggle";

export interface FieldSpec {
  key: string;
  label: string;
  kind?: "text" | "password" | "number" | "toggle" | "choice";
  hint?: string;
  choices?: { value: string; label: string }[];
  /**
   * The name this value is READ under, when it differs from the name it is
   * written under. The Telegram bot's token is written as `bot_token` and
   * stored in `bot_token_enc`, so its "is one stored?" flag comes back as
   * `bot_token_enc_set` — reading it under the write name reports a configured
   * bot as "not set".
   */
  storedAs?: string;
}

export interface ConnectionSpec {
  path: string;
  title: string;
  blurb: string;
  fields: FieldSpec[];
}

/**
 * One connection domain: read it, write one field at a time, show what stored.
 *
 * **Every `key` here is a column name, not a label.** The React port shipped
 * with `recipient` where the store has `to_addr`, so an operator who typed an
 * address into the box got a 500 and no saved address; the Telegram section
 * offered the Telethon reader's credentials against the alert bot's table,
 * where none of the three exist. `tests/api/test_settings_writes_reach_the_store.py`
 * reads this file's field list and checks each name against the schema, so the
 * next one goes red in CI rather than at the keyboard.
 */
export function ConnectionSection(
  { spec, children }: { spec: ConnectionSpec; children?: (saving: boolean) => ReactNode },
) {
  const resource = useSettingsResource<Record<string, unknown>>(spec.path);

  if (!resource.data) {
    return (
      <EmptyState
        title={resource.error ? `Could not load ${spec.title}` : `Loading ${spec.title}`}
        hint={resource.error ?? undefined}
      />
    );
  }

  const data = resource.data;

  return (
    <section data-testid={`domain-${spec.title}`} className="rounded border border-line p-3">
      <h3 className="text-xs font-semibold text-ink-1">{spec.title}</h3>
      <p className="mb-2 text-[11px] text-ink-3">{spec.blurb}</p>
      <div className="grid items-start gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {spec.fields.map((f) => {
          const commit = (v: unknown) => void resource.save({ [f.key]: v });

          if (f.kind === "toggle") {
            return (
              <SettingsToggle
                key={f.key}
                label={f.label}
                hint={f.hint}
                // Stored as 0/1, which is falsy-correct but not boolean.
                checked={Boolean(Number(data[f.key] ?? 0))}
                onChange={(on) => commit(on ? 1 : 0)}
              />
            );
          }

          if (f.kind === "choice") {
            return (
              <label key={f.key} className="block text-xs text-ink-2">
                {f.label}
                <select
                  aria-label={f.label}
                  value={String(data[f.key] ?? "")}
                  onChange={(e) => commit(e.target.value)}
                  className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
                >
                  {f.choices?.map((c) => (
                    <option key={c.value} value={c.value}>{c.label}</option>
                  ))}
                </select>
                {f.hint && <span className="mt-0.5 block text-[10px] text-ink-3">{f.hint}</span>}
              </label>
            );
          }

          const secret = f.kind === "password";
          return (
            <SettingsField
              key={f.key}
              label={f.label}
              version={resource.version}
              type={secret ? "password" : f.kind === "number" ? "number" : "text"}
              // A secret is never echoed back, so its field starts empty and
              // the hint says whether one is stored.
              value={secret ? "" : String(data[f.storedAs ?? f.key] ?? "")}
              hint={
                secret
                  ? data[`${f.storedAs ?? f.key}_set`] === true
                    ? "stored; leave blank to keep it"
                    : "not set"
                  : f.hint
              }
              onCommit={commit}
            />
          );
        })}
      </div>
      {children?.(resource.saving)}
      {resource.error && <p role="alert" className="mt-1 text-xs text-loss">{resource.error}</p>}
    </section>
  );
}
