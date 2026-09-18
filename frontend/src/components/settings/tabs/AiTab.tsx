import { useState } from "react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { asArray } from "@/lib/asArray";
import { useSettingsResource } from "../hooks/useSettingsResource";

interface AiSettings {
  provider: string;
  providers: string[];
  claude_model: string;
  deepseek_model: string;
  anthropic_api_key_set: boolean;
  deepseek_api_key_set: boolean;
  claude_models: string[];
  deepseek_models: string[];
  configured: boolean;
}

const LABEL: Record<string, string> = { claude: "Claude", deepseek: "DeepSeek" };
const KEY_FIELD: Record<string, "anthropic_api_key" | "deepseek_api_key"> = {
  claude: "anthropic_api_key",
  deepseek: "deepseek_api_key",
};

/**
 * Which model answers, and the key that lets it.
 *
 * Restored 2026-09-18. The React port shipped without this tab, so on a fresh
 * install there was no way to enter an API key at all and every AI feature —
 * the Analysis tab, trade commentary, the channel-strategy recommendations,
 * the reversal tuner — was unreachable without editing `config.yaml` by hand.
 *
 * A key can be tested BEFORE it is saved. That is the whole shape of the
 * screen: type, test, then save. Saving first and finding out hours later that
 * an analysis failed is the version this replaces.
 */
export function AiTab() {
  const res = useSettingsResource<AiSettings>("/api/ai/settings");
  const [draftKey, setDraftKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  const data = res.data;
  const provider = data?.provider ?? "claude";

  async function call(path: string, body: unknown, ok: (r: never) => string) {
    setBusy(true);
    setNote(null);
    try {
      const r = await api.post(path, body);
      setNote({ ok: true, text: ok(r as never) });
      await res.reload();
    } catch (e) {
      setNote({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  if (!data) {
    return (
      <EmptyState
        title={res.error ? "Could not load the AI settings" : "Loading"}
        hint={res.error ?? undefined}
      />
    );
  }

  const models = asArray<string>(
    provider === "claude" ? data.claude_models : data.deepseek_models);
  const model = provider === "claude" ? data.claude_model : data.deepseek_model;
  const keyStored = provider === "claude"
    ? data.anthropic_api_key_set : data.deepseek_api_key_set;

  return (
    <div className="space-y-4">
      {!data.configured && (
        <p className="rounded border border-warning/40 bg-warning/10 px-3 py-2 text-[11px] text-warning">
          No AI provider is configured. Trade commentary, the Analysis tab and
          the strategy recommendations all need a key before they can answer.
        </p>
      )}

      <section className="rounded border border-line p-3">
        <h3 className="text-xs font-semibold text-ink-1">Provider</h3>
        <p className="mb-2 text-[11px] text-ink-3">
          Which service answers. Each keeps its own key and model below.
        </p>
        <select
          aria-label="AI provider"
          value={provider}
          onChange={(e) => void res.save({ provider: e.target.value })}
          className="w-full max-w-xs rounded border border-line bg-surface-1 px-2 py-1 text-xs text-ink-1"
        >
          {asArray<string>(data.providers).map((p) => (
            <option key={p} value={p}>{LABEL[p] ?? p}</option>
          ))}
        </select>
      </section>

      <section data-testid="ai-provider-card" className="rounded border border-line p-3">
        <h3 className="text-xs font-semibold text-ink-1">{LABEL[provider] ?? provider}</h3>

        <div className="mt-2 grid gap-3 sm:grid-cols-2">
          <label className="block text-xs text-ink-2">
            API key
            <input
              aria-label="API key"
              type="password"
              value={draftKey}
              onChange={(e) => setDraftKey(e.target.value)}
              className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
            />
            <span className="mt-0.5 block text-[10px] text-ink-3">
              {keyStored ? "stored; leave blank to keep it" : "not set"}
            </span>
          </label>

          <label className="block text-xs text-ink-2">
            Model
            <select
              aria-label="Model"
              value={model}
              onChange={(e) => void res.save(
                provider === "claude"
                  ? { claude_model: e.target.value }
                  : { deepseek_model: e.target.value })}
              className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
            >
              {/* The stored value first, even when it is not in the fetched
                  list: a model this key can no longer use is a real state, and
                  a dropdown that silently showed something else would have the
                  operator believe they are on a model they are not. */}
              {(models.includes(model) || !model ? models : [model, ...models])
                .map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
        </div>

        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            disabled={busy || !draftKey}
            disabledReason={draftKey ? undefined : "Type a key to save."}
            onClick={() => {
              void res.save({ [KEY_FIELD[provider]]: draftKey });
              setDraftKey("");
              setNote({ ok: true, text: "Key saved." });
            }}
          >
            Save key
          </Button>
          <Button
            variant="ghost"
            disabled={busy}
            title="Sends five tokens to prove the key works. Billable."
            onClick={() => void call("/api/ai/settings/test",
              { provider, api_key: draftKey },
              (r: { note?: string }) => r.note ?? "It answered.")}
          >
            Test connection
          </Button>
          <Button
            variant="ghost"
            disabled={busy}
            onClick={() => void call("/api/ai/settings/models",
              { provider, api_key: draftKey },
              (r: { models?: string[] }) => `${(r.models ?? []).length} models available.`)}
          >
            Refresh models
          </Button>
        </div>

        <p className="mt-2 text-[10px] text-ink-3">
          Test uses the key typed above when there is one, so a key can be
          checked before it is saved. It costs five tokens.
        </p>

        {note && (
          <p
            role={note.ok ? "status" : "alert"}
            className={`mt-2 text-[11px] ${note.ok ? "text-profit" : "text-loss"}`}
          >
            {note.text}
          </p>
        )}
        {res.error && <p role="alert" className="mt-1 text-xs text-loss">{res.error}</p>}
      </section>
    </div>
  );
}
