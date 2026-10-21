import { useState } from "react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { EmptyState } from "@/components/shared/EmptyState";
import { asArray } from "@/lib/asArray";
import { useSettingsResource } from "../hooks/useSettingsResource";

interface ProviderInfo {
  label: string;
  model: string;
  key_set: boolean;
  models: string[];
}

interface AiSettings {
  provider: string;
  providers: string[];
  /** One entry per provider (2026-10-21). Older backends sent only the flat fields below. */
  provider_info?: Record<string, ProviderInfo>;
  claude_model: string;
  deepseek_model: string;
  anthropic_api_key_set: boolean;
  deepseek_api_key_set: boolean;
  claude_models: string[];
  deepseek_models: string[];
  configured: boolean;
}

/** Claude's key is Anthropic's own name for it; every other is `<name>_api_key`. */
const keyField = (p: string) => (p === "claude" ? "anthropic_api_key" : `${p}_api_key`);

function infoFor(data: AiSettings, p: string): ProviderInfo {
  const given = data.provider_info?.[p];
  if (given) return { ...given, models: asArray<string>(given.models) };
  const claude = p === "claude";
  return {
    label: claude ? "Claude" : p === "deepseek" ? "DeepSeek" : p,
    model: (claude ? data.claude_model : data.deepseek_model) ?? "",
    key_set: claude ? data.anthropic_api_key_set : data.deepseek_api_key_set,
    models: asArray<string>(claude ? data.claude_models : data.deepseek_models),
  };
}

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
  const [draftModel, setDraftModel] = useState("");
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

  const info = infoFor(data, provider);
  const { models, model } = info;
  const keyStored = info.key_set;
  const saveModel = (m: string) => res.save({ [`${provider}_model`]: m });

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
        <Tooltip label="Which service answers for trade commentary, the Analysis tab and the strategy recommendations. Each provider keeps its own key and model, so switching does not lose the other one's settings.">
        <select
          aria-label="AI provider"
          value={provider}
          onChange={(e) => void res.save({ provider: e.target.value })}
          className="w-full max-w-xs rounded border border-line bg-surface-1 px-2 py-1 text-xs text-ink-1"
        >
          {asArray<string>(data.providers).map((p) => (
            <option key={p} value={p}>{infoFor(data, p).label}</option>
          ))}
        </select>
        </Tooltip>
      </section>

      <section data-testid="ai-provider-card" className="rounded border border-line p-3">
        <h3 className="text-xs font-semibold text-ink-1">{info.label}</h3>

        <div className="mt-2 grid gap-3 sm:grid-cols-2">
          <label className="block text-xs text-ink-2">
            API key
            <Tooltip label="This provider's key. It is saved in config.yaml on this machine (not encrypted) and never shown again, so the box is blank even when one is set — leaving it blank keeps the stored key.">
              <input
                aria-label="API key"
                type="password"
                value={draftKey}
                onChange={(e) => setDraftKey(e.target.value)}
                className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
              />
            </Tooltip>
            <span className="mt-0.5 block text-[10px] text-ink-3">
              {keyStored ? "stored; leave blank to keep it" : "not set"}
            </span>
          </label>

          {models.length > 0 || model ? (
          <label className="block text-xs text-ink-2">
            Model
            <Tooltip label="Which model this provider answers with. A bigger model costs more per call and every AI feature here is billable. A stored model the key can no longer use is still listed, so the box never shows a model you are not on.">
            <select
              aria-label="Model"
              value={model}
              onChange={(e) => void saveModel(e.target.value)}
              className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
            >
              {/* The stored value first, even when it is not in the fetched
                  list: a model this key can no longer use is a real state, and
                  a dropdown that silently showed something else would have the
                  operator believe they are on a model they are not. */}
              {(models.includes(model) || !model ? models : [model, ...models])
                .map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            </Tooltip>
          </label>
          ) : (
          <div className="block text-xs text-ink-2">
            {/* No list yet: nothing has been fetched for this key, and no
                fallback is shipped where a current id could not be confirmed.
                "Refresh models" fills the dropdown once a key is saved. */}
            <label htmlFor="ai-model-id">Model id</label>
            <div className="mt-0.5 flex gap-1">
              <Tooltip label="The provider's own id for the model, exactly as its docs or model list give it. Once a key is saved, Refresh models fetches the list and this box becomes a dropdown.">
              <input
                id="ai-model-id"
                value={draftModel}
                onChange={(e) => setDraftModel(e.target.value)}
                placeholder="save a key, then Refresh models"
                className="w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
              />
              </Tooltip>
              <Button
                variant="ghost"
                disabled={!draftModel.trim()}
                disabledReason={draftModel.trim() ? undefined : "Type a model id."}
                onClick={() => { void saveModel(draftModel.trim()); setDraftModel(""); }}
              >
                Use this model
              </Button>
            </div>
          </div>
          )}
        </div>

        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            disabled={busy || !draftKey}
            disabledReason={draftKey ? undefined : "Type a key to save."}
            onClick={() => {
              void res.save({ [keyField(provider)]: draftKey });
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
            tooltip="Sends about five tokens to the provider to prove the key works. Billable, but the smallest call this app can make."
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
