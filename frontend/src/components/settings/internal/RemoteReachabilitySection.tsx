export interface Reachability {
  addresses?: string[];
  behind_nat?: boolean;
  firewall?: "open" | "missing" | "unknown" | "not-applicable";
  firewall_command?: string;
  security?: string;
}

/**
 * What the paired machine needs to reach this one: the address and port to
 * type into its "Connect to a remote VPS" box, whether the Windows firewall
 * lets it in, and what protects the link. A fresh VPS said only "listening"
 * (2026-09-25), which left the operator to find all three elsewhere.
 */
export function RemoteReachabilitySection({ reach, port }: { reach: Reachability; port: number }) {
  if (!reach || !Array.isArray(reach.addresses)) return null;
  const addresses = reach.addresses;

  return (
    <div data-testid="remote-reachability" className="mt-2 space-y-1 text-[11px] text-ink-2">
      <p>
        On the other machine, connect to{" "}
        {addresses.length > 0 ? (
          <span className="font-mono text-ink-1">
            {addresses.map((a) => `${a}:${port}`).join(" or ")}
          </span>
        ) : (
          <span className="text-ink-3">this machine's IP (not detected), port {port}</span>
        )}
      </p>
      {reach.behind_nat && (
        <p className="text-warning">
          That is a private address. If your VPS provider gives this machine a
          public IP, the other machine dials that instead — it is on the
          provider's control panel.
        </p>
      )}
      {reach.firewall === "open" && (
        <p className="text-profit">Port {port} is open in the Windows firewall.</p>
      )}
      {(reach.firewall === "missing" || reach.firewall === "unknown") && (
        <div className="text-warning">
          <p>
            {reach.firewall === "missing"
              ? `Port ${port} is not open in the Windows firewall yet.`
              : `Could not check the Windows firewall for port ${port}.`}{" "}
            Run this once in an administrator command prompt:
          </p>
          <code className="mt-0.5 block break-all rounded bg-surface-1 px-2 py-1 font-mono text-[10px] text-ink-1">
            {reach.firewall_command}
          </code>
        </div>
      )}
      <p className="text-ink-3">
        A firewall at your VPS provider, if it has one, must allow TCP {port} too.
      </p>
      {reach.security && <p className="text-ink-3">{reach.security}</p>}
    </div>
  );
}
