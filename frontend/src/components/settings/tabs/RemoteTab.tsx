import { useState } from "react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney } from "@/components/shared/format";
import { asObject } from "@/lib/asArray";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { SettingsToggle } from "../internal/SettingsToggle";

interface RemoteState {
  server: {
    enabled: boolean; port: number; running: boolean;
    fingerprint: string; token_set: boolean;
  };
  client: {
    host: string; port: number; token_set: boolean;
    conn_state: string; last_error: string;
    remote_status: Record<string, unknown>;
  };
  headless: boolean;
  centralized_signal_gen: boolean;
}

/**
 * This machine's role in the Local/Remote pair.
 *
 * Exactly one role per machine: the VPS accepts the connection, the Mac
 * initiates it. Ported on 2026-09-18 — the NiceGUI page was deleted in the
 * big-bang replace and nothing replaced it, so starting the sync server,
 * connecting out, headless mode, centralized signal generation and the model
 * snapshot were all unreachable from the dashboard.
 *
 * Two of these switches change what the engines do next, and both say so
 * before and after. The backend writes those notes; this renders them.
 */
export function RemoteTab() {
  const resource = useSettingsResource<RemoteState>("/api/remote/state");
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // Every field on the payload is read through `asObject`. A missing one
  // otherwise throws inside render and React tears down the whole tab, which
  // is how one absent field blanks a screen that is mostly fine.
  const data = resource.data;
  const server = asObject<RemoteState["server"]>(data?.server);
  const client = asObject<RemoteState["client"]>(data?.client);
  const peer = asObject<Record<string, unknown>>(client.remote_status);

  const [host, setHost] = useState<string | null>(null);
  const [port, setPort] = useState<string | null>(null);
  const [token, setToken] = useState("");

  async function act(fn: () => Promise<{ note?: string }>) {
    setBusy(true);
    setNote(null);
    try {
      const res = await fn();
      if (res?.note) setNote({ ok: true, text: res.note });
      await resource.reload();
    } catch (e) {
      setNote({ ok: false, text: e instanceof ApiError ? e.message : String(e) });
      await resource.reload();
    } finally {
      setBusy(false);
    }
  }

  if (!data) {
    return (
      <EmptyState
        title={resource.error ? "Could not load the remote settings" : "Loading"}
        hint={resource.error ?? undefined}
      />
    );
  }

  const connected = client.conn_state === "connected";

  return (
    <div className="space-y-4">
      <p className="text-[11px] text-ink-3">
        Configure exactly one role per machine: the VPS accepts the connection,
        this Mac initiates it. Both use the same token — generate it on the VPS
        under Node &amp; updates, then paste it here.
      </p>

      {note && (
        <p
          role={note.ok ? "status" : "alert"}
          className={`whitespace-pre-line text-[11px] ${note.ok ? "text-profit" : "text-loss"}`}
        >
          {note.text}
        </p>
      )}

      {/* ── This machine is the VPS ─────────────────────────────────────── */}
      <section data-testid="remote-server" className="rounded border border-line p-3">
        <h3 className="text-xs font-semibold text-ink-1">This machine accepts connections</h3>
        <p className="mb-2 text-[11px] text-ink-3">
          Turn this on only on the VPS. It needs a pairing token first.
        </p>
        <div className="flex flex-wrap items-end gap-4">
          <SettingsToggle
            label="Accept remote connections"
            checked={Boolean(server.enabled)}
            onChange={(on) => void act(() =>
              api.put("/api/remote/server", { enabled: on, port: Number(server.port) }))}
          />
          <label className="text-xs text-ink-2">
            Listen port
            <Tooltip label="The TCP port this machine listens on for the other node. It must match the port the other node dials, and be open in this machine's firewall. Saved when you leave the box.">
              <input
                aria-label="Listen port"
                className="num mt-0.5 w-28 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
                defaultValue={String(server.port ?? "")}
                onBlur={(e) => void act(() =>
                  api.put("/api/remote/server",
                    { enabled: Boolean(server.enabled), port: Number(e.target.value) }))}
              />
            </Tooltip>
          </label>
          <span className="text-[11px] text-ink-3">
            {server.running ? "listening" : "stopped"}
            {!server.token_set && " — no token generated yet"}
          </span>
        </div>
        <p className="mt-2 break-all font-mono text-[10px] text-ink-3">
          Cert fingerprint: {server.fingerprint || "(generated on first start)"}
        </p>
      </section>

      {/* ── This machine connects out ───────────────────────────────────── */}
      <section data-testid="remote-client" className="rounded border border-line p-3">
        <h3 className="text-xs font-semibold text-ink-1">Connect to a remote VPS</h3>
        <p className="mb-2 text-[11px] text-ink-3">
          Saving and connecting are one action: a stored address that was never
          dialled looks exactly like a working pair.
        </p>
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="block text-xs text-ink-2">
            VPS address
            <Tooltip label="The other node's hostname or IP address. Nothing is dialled until you press Save and connect — a stored address that was never tried looks exactly like a working pair.">
              <input
                aria-label="VPS address"
                className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
                value={host ?? client.host ?? ""}
                onChange={(e) => setHost(e.target.value)}
              />
            </Tooltip>
          </label>
          <label className="block text-xs text-ink-2">
            Port
            <Tooltip label="The port the other node is listening on. It has to match that machine's Listen port above.">
              <input
                aria-label="VPS port"
                className="num mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
                value={port ?? String(client.port ?? "")}
                onChange={(e) => setPort(e.target.value)}
              />
            </Tooltip>
          </label>
          <label className="block text-xs text-ink-2">
            Shared token
            <Tooltip label="The pairing token, generated on the OTHER node under Node & updates and shown there exactly once. It is stored but never shown again here, so reconnecting means pasting it in fresh.">
              <input
                aria-label="Shared token"
                type="password"
                className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
            </Tooltip>
            <span className="mt-0.5 block text-[10px] text-ink-3">
              {client.token_set
                ? "one is stored; re-enter it to reconnect"
                : "not set"}
            </span>
          </label>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button
            disabled={busy}
            onClick={() => void act(() => api.put("/api/remote/client", {
              host: host ?? client.host ?? "",
              port: Number(port ?? client.port ?? 0),
              token,
            }))}
          >
            Save and connect
          </Button>
          <Button variant="ghost" disabled={busy}
            onClick={() => void act(() => api.post("/api/remote/client/disconnect", {}))}>
            Disconnect
          </Button>
          <span className={`text-[11px] ${connected ? "text-profit" : "text-ink-3"}`}>
            {client.conn_state}
            {client.last_error && !connected && ` (${client.last_error})`}
          </span>
        </div>

        {connected && Object.keys(peer).length > 0 && (
          // Only while connected. A balance left on screen from a link that
          // has since dropped is a number the operator will act on.
          <div className="mt-2 space-y-0.5 text-[11px] text-ink-2">
            <p>
              VPS balance {formatMoney(Number(peer.balance ?? 0))}
              {" · equity "}{formatMoney(Number(peer.equity ?? 0))}
            </p>
            <p className="text-ink-3">
              Open positions {Array.isArray(peer.open_positions) ? peer.open_positions.length : 0}
              {" · active trader "}{String(peer.active_trader ?? "?")}
            </p>
          </div>
        )}
      </section>

      {/* ── The two switches that bite ──────────────────────────────────── */}
      <section data-testid="remote-behaviour" className="space-y-3 rounded border border-line p-3">
        <div>
          <SettingsToggle
            label="Generate signals on this node only"
            checked={Boolean(data.centralized_signal_gen)}
            hint="With this on and the VPS trading, the VPS stops analysing and parsing entirely and only executes what this machine forwards. If this machine goes offline it alerts and waits — it does not fall back to generating its own."
            onChange={(on) => void act(() =>
              api.put("/api/remote/centralized-signals", { enabled: on }))}
          />
        </div>
        <div>
          <SettingsToggle
            label="Run headless (no web UI)"
            checked={Boolean(data.headless)}
            hint="Runs the engine, Telegram reader, bridge and sync with no dashboard. Restarts the app now, because it only takes effect on a restart."
            onChange={(on) => void act(() =>
              api.put("/api/remote/headless", { enabled: on }))}
          />
        </div>
      </section>

      {/* ── Model snapshot ──────────────────────────────────────────────── */}
      <section data-testid="remote-models" className="rounded border border-line p-3">
        <h3 className="text-xs font-semibold text-ink-1">ML model snapshot</h3>
        <p className="mb-2 text-[11px] text-ink-3">
          A one-off copy of the trained model files, never automatic. It
          overwrites the destination's models — use it to seed a fresh node from
          the more mature side.
        </p>
        <div className="flex gap-2">
          {(["download", "upload"] as const).map((direction) => (
            <Button
              key={direction}
              variant="ghost"
              disabled={busy}
              disabledReason={connected ? undefined : "Not connected to a remote node."}
              onClick={() => void act(() =>
                api.post("/api/remote/model-snapshot", { direction }))}
            >
              {direction === "download" ? "Download from VPS" : "Upload to VPS"}
            </Button>
          ))}
        </div>
      </section>
    </div>
  );
}
