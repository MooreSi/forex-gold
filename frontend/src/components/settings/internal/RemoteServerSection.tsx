import { useState } from "react";
import { api } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { asObject } from "@/lib/asArray";
import { RemoteReachabilitySection, type Reachability } from "./RemoteReachabilitySection";

export interface ServerState {
  enabled: boolean; port: number; running: boolean;
  fingerprint: string; token_set: boolean;
  reachability?: Reachability;
}

interface Props {
  server: ServerState;
  busy: boolean;
  act: (fn: () => Promise<{ note?: string }>) => Promise<void>;
}

/**
 * This machine's VPS role.
 *
 * One press makes it the VPS: a pairing token if there is none, the sync port
 * opened in the Windows firewall (with the normal admin prompt), and the
 * listener started. The installer no longer opens that port (owner,
 * 2026-09-25), because most Windows installs are someone's main PC and should
 * accept nothing inbound. "Stop being a VPS" undoes both and keeps the token.
 */
export function RemoteServerSection({ server, busy, act }: Props) {
  const [newToken, setNewToken] = useState<string | null>(null);
  const port = Number(server.port);
  const isVps = Boolean(server.enabled);

  const makeVps = () => act(async () => {
    const res = await api.post<{ note?: string; token?: string }>(
      "/api/remote/make-vps", { port });
    if (res?.token) setNewToken(res.token);
    return res;
  });

  return (
    <section data-testid="remote-server" className="rounded border border-line p-3">
      <h3 className="text-xs font-semibold text-ink-1">This machine accepts connections</h3>
      <p className="mb-2 text-[11px] text-ink-3">
        {isVps
          ? "This machine is the VPS: it listens for the other machine on the port below."
          : "Only for the machine that trades unattended, such as a Windows VPS. If this is your main computer, leave it off: the port stays closed."}
      </p>
      <div className="flex flex-wrap items-end gap-4">
        {isVps ? (
          <Button variant="ghost" disabled={busy}
            onClick={() => void act(() => api.post("/api/remote/stop-vps", {}))}>
            Stop being a VPS
          </Button>
        ) : (
          <Button disabled={busy} onClick={() => void makeVps()}>
            Make this node a VPS
          </Button>
        )}
        <label className="text-xs text-ink-2">
          Listen port
          <Tooltip label="The TCP port this machine listens on for the other node. It must match the port the other node dials. Saved when you leave the box.">
            <input
              aria-label="Listen port"
              className="num mt-0.5 w-28 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
              defaultValue={String(server.port ?? "")}
              onBlur={(e) => void act(() =>
                api.put("/api/remote/server", { enabled: isVps, port: Number(e.target.value) }))}
            />
          </Tooltip>
        </label>
        <span className="text-[11px] text-ink-3">
          {server.running ? "listening" : "stopped"}
        </span>
      </div>

      {newToken && (
        <div data-testid="remote-new-token" className="mt-2 rounded border border-warning p-2 text-[11px]">
          <p className="text-warning">
            Pairing token. Paste it into the other machine's "Shared token" box
            now: it is not shown again.
          </p>
          <code className="mt-1 block break-all font-mono text-ink-1">{newToken}</code>
        </div>
      )}

      {isVps && (
        <>
          <p className="mt-2 break-all font-mono text-[10px] text-ink-3">
            Cert fingerprint: {server.fingerprint || "(generated on first start)"}
          </p>
          <RemoteReachabilitySection
            reach={asObject<Reachability>(server.reachability)}
            port={port}
            onOpenPort={busy ? undefined : () => void makeVps()}
          />
        </>
      )}
    </section>
  );
}
