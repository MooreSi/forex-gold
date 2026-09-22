import { CircleDot, LogOut, Server, ShieldCheck } from "lucide-react";
import { ActiveTraderControl } from "./ActiveTraderControl";
import { EaBadge } from "./EaBadge";
import { EnvironmentControl } from "./EnvironmentControl";
import { HeaderStats } from "./HeaderStats";
import { PowerControl } from "./PowerControl";
import { TradingStatusBadge } from "./TradingStatusBadge";
import { UpdateBadge } from "./UpdateBadge";
import { Tooltip } from "@/components/shared/Tooltip";
import { useAuth } from "@/contexts/AuthContext";
import { useAdminConsole } from "@/hooks/useAdminConsole";
import { useHeaderState } from "@/hooks/useHeaderState";

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

      {/* The halt used to be stated HERE as well, in a badge of its own, and
          a third time on the Pause button's label. Three versions of one fact
          across a bar that is already tight (owner report, 2026-09-21). There
          is one now: `TradingStatusBadge` on the right, which reads the
          backend's own decision and covers all FOUR mechanisms rather than the
          two this badge knew about. */}

      <div className="ml-auto flex shrink-0 items-center gap-2 text-xs text-ink-2 lg:gap-3">
        <UpdateBadge update={data?.update ?? null} />
        {data?.remote_connected && (
          <Tooltip label="This node is linked to the other paired node.">
            <span className="flex items-center gap-1 text-remote">
              <Server size={13} /> <span className="hidden xl:inline">remote</span>
            </span>
          </Tooltip>
        )}
        <Tooltip
          label={bridgeUp
            ? "The MT5 bridge is connected. Prices, orders and positions can reach MetaTrader."
            : "The MT5 bridge is NOT connected. No price, order or position can reach MetaTrader."}
        >
          <span className="flex items-center gap-1">
            <CircleDot size={13} className={bridgeUp ? "text-profit" : "text-loss"} />
            <span className="hidden xl:inline">Bridge</span>
          </span>
        </Tooltip>
        {/* Colour and words come from the backend. A stale EA build shown as
            a green badge is the screen contradicting the log — the bug
            `ea_badge_state` was extracted for. The UI renders the decision,
            and when it says stale, opens the dialog that fixes it. */}
        <EaBadge badge={data?.ea_badge} />
        {data?.active_trader && (
          <ActiveTraderControl
            activeTrader={data.active_trader}
            remoteConnected={data.remote_connected === true}
            onChanged={() => void refresh()}
          />
        )}

        <span aria-hidden className="h-5 w-px bg-line" />

        {/* Whether anything is holding automated entries, and which of the
            four mechanisms it is. Always visible, and since 2026-09-22 the
            ONLY control for it as well: click it to pause or to resume. The
            separate Pause button beside it read one of the four mechanisms,
            so it offered "Pause" while a profit target already held every
            entry. */}
        <TradingStatusBadge />

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
