import { CircleDot, LogOut, Server, TriangleAlert } from "lucide-react";
import { AccountBadge } from "./AccountBadge";
import { ActiveTraderControl } from "./ActiveTraderControl";
import { EnvironmentControl } from "./EnvironmentControl";
import { PauseControl } from "./PauseControl";
import { Button } from "@/components/shared/Button";
import { formatClock, formatPrice } from "@/components/shared/format";
import { useAuth } from "@/contexts/AuthContext";
import { useHeaderState } from "@/hooks/useHeaderState";
import { cn } from "@/lib/cn";

// The service names a colour; this is the only place that decides what the
// name looks like. Amber is "connected but stale", which is neither green nor
// red on purpose.
const EA_COLOURS: Record<string, string> = {
  green: "text-profit",
  red: "text-loss",
  orange: "text-warning",
  amber: "text-warning",
  grey: "text-ink-3",
  gray: "text-ink-3",
};

/**
 * The bar on every tab: who is trading, on what account, at what price, and
 * whether trading is halted.
 *
 * It renders one shared poll's payload (`useHeaderState`) rather than fetching
 * anything itself.
 */
export function AppHeader() {
  const { data, error, updatedAt, refresh } = useHeaderState();
  const { logout } = useAuth();

  const stale = updatedAt !== null && Date.now() - updatedAt > 20_000;
  const bridgeUp = data?.bridge?.["connected"] === true;

  return (
    <header className="flex shrink-0 items-center gap-3 border-b border-line bg-surface-1 px-4 py-2">
      <span className="text-sm font-bold tracking-tight text-accent">FOREX Trader</span>
      <AccountBadge account={data?.account ?? null} />
      <EnvironmentControl />

      {data?.tick && (
        <span className="num flex items-center gap-2 text-xs text-ink-2">
          <span className="text-profit">{formatPrice(data.tick.bid)}</span>
          <span className="text-ink-3">/</span>
          <span className="text-remote">{formatPrice(data.tick.ask)}</span>
          {(stale || error) && (
            // A number that stopped updating reads as a number that stopped
            // moving. Say which it is.
            <span className="text-warning" title="This price has stopped updating.">
              stale
            </span>
          )}
        </span>
      )}

      {data?.pause?.paused && (
        // Both halts. The circuit breaker writes a different key from the risk
        // governor, so a header that read only the governor said nothing while
        // automated entries were being refused.
        <span
          data-testid="pause-badge"
          className="flex items-center gap-1 rounded border border-warning/40 bg-warning/10 px-2 py-0.5 text-[11px] text-warning"
          title={data.pause.reason}
        >
          <TriangleAlert size={12} />
          {data.pause.reason}
          {data.pause.until && (
            // "Halted" without a resume time leaves the operator watching the
            // screen to find out when it lifts.
            <span className="text-ink-3">· until {formatClock(data.pause.until)}</span>
          )}
        </span>
      )}

      <div className="ml-auto flex items-center gap-3 text-xs text-ink-2">
        {data?.remote_connected && (
          <span className="flex items-center gap-1 text-remote" title="Linked to the remote node">
            <Server size={13} /> remote
          </span>
        )}
        <span
          className="flex items-center gap-1"
          title={bridgeUp ? "MT5 bridge connected" : "MT5 bridge not connected"}
        >
          <CircleDot size={13} className={bridgeUp ? "text-profit" : "text-loss"} />
          bridge
        </span>
        {data?.ea_badge && (
          // Colour and words come from the backend. A stale EA build shown as
          // a green badge is the screen contradicting the log — the bug
          // `ea_badge_state` was extracted for. The UI renders the decision.
          <span
            data-testid="ea-badge"
            className={cn("flex items-center gap-1", EA_COLOURS[data.ea_badge.colour] ?? "text-ink-3")}
            title={data.ea_badge.tooltip}
          >
            <CircleDot size={13} />
            {data.ea_badge.text}
          </span>
        )}
        <PauseControl
          paused={data?.pause?.paused === true}
          onChanged={() => void refresh()}
        />
        {data?.active_trader && (
          <ActiveTraderControl
            activeTrader={data.active_trader}
            remoteConnected={data.remote_connected === true}
            onChanged={() => void refresh()}
          />
        )}
        <Button variant="ghost" onClick={() => void logout()} title="Sign out">
          <LogOut size={13} /> Sign out
        </Button>
      </div>
    </header>
  );
}
