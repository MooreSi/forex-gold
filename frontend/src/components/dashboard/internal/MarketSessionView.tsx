import { Building2, Clock, Globe, Landmark, Moon, Sunrise, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

/** What the backend calls each session, in the words the Trading tab uses. */
export const SESSION_LABEL: Record<string, string> = {
  asian: "Asia", london: "London", overlap: "London + New York",
  ny: "New York", closed: "Markets closed", off: "Out of session",
};

const SESSION_ICON: Record<string, LucideIcon> = {
  asian: Sunrise, london: Landmark, overlap: Globe, ny: Building2,
  closed: Moon, off: Clock,
};

/**
 * Which market the clock is in, and whether automated entries are allowed in
 * it (owner, 2026-10-09: an icon above Lifetime P&L).
 *
 * Both facts come from the schedule payload's `markets`, which asks the same
 * `is_session_allowed` the execution gate asks. "Trading" therefore means
 * the gate would let an entry through on session grounds, not merely that
 * the hour falls in London; a session the operator switched off says so.
 * Other holds (a halt, a news blackout) are the header badge's job.
 *
 * Renders nothing when the payload has no session: the backend sends
 * `markets: null` when the toggles are unreadable, and a default here would
 * claim a market is live that may be switched off.
 */
export function MarketSessionView({ markets }: { markets: Record<string, unknown> }) {
  const session = typeof markets["session"] === "string" ? markets["session"] : "";
  if (!session) return null;

  const allowed = markets["allowed_now"] === true;
  const Icon = SESSION_ICON[session] ?? Globe;
  const status = allowed ? "Trading"
    : session === "closed" ? "Weekend"
    : session === "off" ? "Outside trading hours"
    : "Switched off";
  const name = session === "off" ? "No market" : SESSION_LABEL[session] ?? session;

  return (
    <div
      data-testid="dash-market"
      title="The market session now, and whether Trading Markets lets automated entries open in it."
      className={cn(
        "flex items-center gap-2 rounded-full border py-1 pl-1 pr-3",
        allowed ? "border-profit/40 bg-profit/10" : "border-line bg-surface-2",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "flex size-6 items-center justify-center rounded-full",
          allowed ? "bg-profit/20 text-profit" : "bg-surface-3 text-ink-3",
        )}
      >
        <Icon size={13} />
      </span>
      <span className="text-[11px] font-semibold leading-none text-ink-1">{name}</span>
      <span className={cn("flex items-center gap-1 text-[10px] leading-none",
                           allowed ? "text-profit" : "text-ink-3")}>
        {allowed && (
          <span aria-hidden className="relative flex size-1.5">
            <span className="absolute inline-flex size-full animate-ping rounded-full
                             bg-profit opacity-70" />
            <span className="relative inline-flex size-1.5 rounded-full bg-profit" />
          </span>
        )}
        {status}
      </span>
    </div>
  );
}
