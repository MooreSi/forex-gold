import { CircleDot, LogOut, Server, ShieldCheck, TriangleAlert } from "lucide-react";
import { ActiveTraderControl } from "./ActiveTraderControl";
import { EnvironmentControl } from "./EnvironmentControl";
import { HeaderStats } from "./HeaderStats";
import { PauseControl } from "./PauseControl";
import { PowerControl } from "./PowerControl";
import { formatClock } from "@/components/shared/format";
import { useAuth } from "@/contexts/AuthContext";
import { useAdminConsole } from "@/hooks/useAdminConsole";
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
 * The bar on every tab: who is trading, on what account, at what price, what
 * the account is worth, and whether trading is halted.
 *
 * It renders one shared poll's payload (`useHeaderState`) rather than fetching
 * anything itself.
 *
 * Laid out in the NiceGUI original's three groups — brand, then the numbers,
 * then status and controls on the right. The account badge is the environment
 * switch; there is no second "DEMO" label beside it any more.
 */
export function AppHeader() {
  const { data, error, updatedAt, refresh } = useHeaderState();
  const { logout } = useAuth();
  const adminConsole = useAdminConsole();

  const stale = updatedAt !== null && Date.now() - updatedAt > 20_000;
  const bridgeUp = data?.bridge?.["connected"] === true;

  return (
    // `overflow-hidden` is the backstop. Every group inside shrinks or hides
    // before it matters, but a header that CAN exceed the viewport scrolls the
    // whole application sideways and clips the panel below it, which is a much
    // worse failure than a figure dropping off the end.
    <header className="flex shrink-0 items-center gap-2 overflow-hidden border-b border-line bg-surface-1 px-3 py-2 lg:gap-3 lg:px-4">
      <div className="flex shrink-0 flex-col justify-center leading-none">
        <span className="text-sm font-bold tracking-tight text-accent">FOREX Trader</span>
        <span className="hidden text-[9px] text-remote lg:inline">by Simon Moore</span>
      </div>

      <EnvironmentControl account={data?.account ?? null} />

      <HeaderStats
        tick={data?.tick ?? null}
        account={data?.account ?? null}
        lifetimePnl={data?.lifetime_pnl ?? null}
        stale={stale || Boolean(error)}
      />

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

      <div className="ml-auto flex shrink-0 items-center gap-2 text-xs text-ink-2 lg:gap-3">
        {data?.remote_connected && (
          <span className="flex items-center gap-1 text-remote" title="Linked to the remote node">
            <Server size={13} /> <span className="hidden xl:inline">remote</span>
          </span>
        )}
        <span
          className="flex items-center gap-1"
          title={bridgeUp ? "MT5 bridge connected" : "MT5 bridge not connected"}
        >
          <CircleDot size={13} className={bridgeUp ? "text-profit" : "text-loss"} />
          <span className="hidden xl:inline">bridge</span>
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
        {data?.active_trader && (
          <ActiveTraderControl
            activeTrader={data.active_trader}
            remoteConnected={data.remote_connected === true}
            onChanged={() => void refresh()}
          />
        )}

        <span aria-hidden className="h-5 w-px bg-line" />

        <PauseControl
          paused={data?.pause?.paused === true}
          onChanged={() => void refresh()}
        />
        <PowerControl />
        {/* Only on the licence-issuer machine, and only when the console
            actually mounted -- useAdminConsole probes the route rather than
            reading a flag, so the button cannot appear pointing at a 404. */}
        {adminConsole.available ? (
          <a
            href="/admin/"
            target="_blank"
            rel="noreferrer"
            aria-label="Licence admin"
            title={
              adminConsole.canSign
                ? "Licence admin"
                : "Licence admin (no signing key on this machine)"
            }
            className="rounded p-1.5 text-accent transition-colors hover:bg-surface-2"
          >
            <ShieldCheck size={15} />
          </a>
        ) : null}
        <button
          type="button"
          onClick={() => void logout()}
          aria-label="Sign out"
          title="Sign out"
          className="rounded p-1.5 text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink-1"
        >
          <LogOut size={15} />
        </button>
      </div>
    </header>
  );
}
