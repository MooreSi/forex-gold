import { Building2, MapPin, Moon } from "lucide-react";
import { asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

interface TradingMarketsCardProps {
  markets: Record<string, unknown>;
  onSetMarket: (market: string, enabled: boolean) => void;
}

/**
 * Which sessions will accept and execute signals.
 *
 * Signals are still GENERATED at any time; these decide whether one may
 * trigger a live trade. That distinction is the whole card — switching London
 * off does not stop the engines working, it stops them trading.
 *
 * The live session badge comes from the backend, which reads it through the
 * same `is_session_allowed` the gate uses. The NiceGUI page computed its own
 * from the hour, which is a second opinion about the one question the gate
 * already answers.
 */
const MARKETS: { key: string; label: string; hours: string; Icon: typeof Moon }[] = [
  { key: "asia", label: "Asia", hours: "21:00–07:00 UTC", Icon: Moon },
  { key: "london", label: "London", hours: "07:00–16:00 UTC", Icon: Building2 },
  { key: "new_york", label: "New York", hours: "12:00–21:00 UTC", Icon: MapPin },
];

const SESSION_LABEL: Record<string, string> = {
  asian: "Asia", london: "London", overlap: "Overlap (London + NY)",
  ny: "New York", closed: "Markets Closed",
};

export function TradingMarketsCard({ markets, onSetMarket }: TradingMarketsCardProps) {
  const m = asObject(markets);
  const session = String(m["session"] ?? "");
  const allowedNow = m["allowed_now"] === true;
  const active = MARKETS.filter((x) => m[x.key] === true).map((x) => x.label);

  return (
    <section className="rounded-lg border border-line bg-surface-1 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-ink-1">Trading Markets</h3>
        <span className="text-[11px] text-ink-3">Current session:</span>
        <span
          data-testid="current-session"
          className={cn(
            "rounded px-1.5 py-0.5 text-[11px] font-medium",
            allowedNow
              ? "bg-accent/15 text-accent"
              : "bg-surface-3 text-ink-3",
          )}
        >
          {SESSION_LABEL[session] ?? session ?? "—"}
        </span>
      </div>

      <div className="flex flex-wrap gap-2">
        {MARKETS.map(({ key, label, hours, Icon }) => {
          const on = m[key] === true;
          return (
            <button
              key={key}
              type="button"
              aria-pressed={on}
              title={`${label}: ${hours}`}
              onClick={() => onSetMarket(key, !on)}
              className={cn(
                "flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs font-medium transition",
                on
                  ? "border-profit/40 bg-profit/15 text-profit"
                  : "border-line bg-surface-2 text-ink-3 hover:text-ink-2",
              )}
            >
              <Icon className="h-3.5 w-3.5" aria-hidden />
              {label}
            </button>
          );
        })}
      </div>

      <p className={cn("mt-1.5 text-[11px]",
        active.length ? "text-ink-3" : "text-loss")}>
        {active.length
          ? `Active: ${active.join(", ")}`
          : "No sessions active — every automated entry is blocked"}
      </p>
    </section>
  );
}
