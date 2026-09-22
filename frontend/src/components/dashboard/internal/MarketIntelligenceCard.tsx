import { CalendarClock } from "lucide-react";
import type { NewsEvent, NewsState, SetForgetState } from "@/api/types";
import { formatPrice, formatUtcTime } from "@/components/shared/format";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { DashCard, Pill, Reading } from "./DashCard";

interface MarketIntelligenceCardProps {
  setforget: SetForgetState | null;
  news: NewsState | null;
  /** `markets` from the schedule payload: which session it is, and whether
   *  live execution is allowed in it. */
  markets: Record<string, unknown>;
}

/** What the backend calls each session, in the words the Trading tab uses. */
const SESSION_LABEL: Record<string, string> = {
  asian: "Asia", london: "London", overlap: "London + New York",
  ny: "New York", closed: "Markets closed",
};

/**
 * The read of the market itself: structure, the instruments, the session and
 * what is on the calendar.
 *
 * Every number is measured by the backend and passed through. The session in
 * particular comes from the schedule payload, which reads it through the same
 * `is_session_allowed` the execution gate uses — the NiceGUI page worked its
 * own out from the hour, which is a second opinion about the one question the
 * gate has already answered.
 */
export function MarketIntelligenceCard({
  setforget, news, markets,
}: MarketIntelligenceCardProps) {
  const e = setforget?.evidence;
  const session = String(markets["session"] ?? "");
  const allowedNow = markets["allowed_now"] === true;
  const nextHigh = asArray<NewsEvent>(news?.events)
    .find((ev) => String(ev.impact ?? "").toLowerCase() === "high");
  const blackout = news?.blackout;
  const trendUp = e?.ema_fast != null && e?.ema_slow != null
    ? e.ema_fast > e.ema_slow : null;

  return (
    <DashCard
      title="Market intelligence"
      icon="evidence"
      badge={session
        ? <Pill tone={allowedNow ? "accent" : "neutral"}>
            {SESSION_LABEL[session] ?? session}
          </Pill>
        : null}
      footnote={e
        ? `Measured on the ${e.entry_timeframe} chart. Zone widths come from ATR.`
        : "These are read from candles by the backend, never derived in the browser."}
    >
      <dl className="grid grid-cols-3 gap-x-3 gap-y-2">
        <Reading
          label="Structure"
          value={e ? `${short(e.weekly_bias)} / ${short(e.daily_bias)}` : "—"}
          hint="Weekly bias over daily bias — the two reads the method stacks."
        />
        <Reading
          label="Trend (EMA)"
          value={trendUp == null ? "—" : trendUp ? "50 above 200" : "50 below 200"}
          tone={trendUp == null ? undefined : trendUp ? "text-profit" : "text-loss"}
        />
        <Reading
          label="Zones"
          value={e ? String(e.zones?.length ?? 0) : "—"}
          hint="Supply and demand bands the backend found on this read."
        />
        <Reading
          label="ATR 14"
          value={e?.atr ? e.atr.toFixed(2) : "—"}
          hint="Volatility on the entry timeframe."
        />
        <Reading
          label="Daily range"
          value={e?.daily_atr ? e.daily_atr.toFixed(2) : "—"}
          hint="The average DAILY range — what turns a distance into a wait."
        />
        <Reading
          label="RSI 14"
          value={e?.rsi == null ? "—" : e.rsi.toFixed(1)}
          tone={e?.rsi == null ? undefined
            : e.rsi >= 70 ? "text-loss" : e.rsi <= 30 ? "text-profit" : undefined}
          hint={e?.rsi == null ? undefined
            : e.rsi >= 70 ? "overbought" : e.rsi <= 30 ? "oversold" : "mid-range"}
        />
      </dl>

      <div className="mt-2.5 flex items-start gap-1.5 border-t border-line/70 pt-2
                      text-[11px]">
        <CalendarClock size={12} className="mt-0.5 shrink-0 text-ink-3" aria-hidden />
        <p className="text-ink-2">
          {nextHigh ? (
            <>
              Next high impact:{" "}
              <span className="font-medium text-ink-1">{nextHigh.title}</span>{" "}
              <span className="text-ink-3">
                ({nextHigh.currency}, {formatUtcTime(nextHigh.ts)} UTC)
              </span>
            </>
          ) : (
            <span className="text-ink-3">No high-impact release on the calendar.</span>
          )}
          {blackout && (
            <span className={cn("ml-1", blackout.enabled ? "text-warning" : "text-ink-3")}>
              {blackout.enabled
                ? `Blackout holds entries ${blackout.minutes_before}m before and ${blackout.minutes_after}m after.`
                : "News blackout is off."}
            </span>
          )}
        </p>
      </div>

      {e?.price != null && (
        <p className="num mt-1 text-[10px] text-ink-3">
          read at {formatPrice(e.price)}
        </p>
      )}
    </DashCard>
  );
}

/** "bullish" → "Bull", so three readings fit across a card. */
function short(bias: string | null | undefined): string {
  const b = String(bias ?? "").toLowerCase();
  if (b.includes("bull")) return "Bull";
  if (b.includes("bear")) return "Bear";
  return b ? b.charAt(0).toUpperCase() + b.slice(1) : "—";
}
