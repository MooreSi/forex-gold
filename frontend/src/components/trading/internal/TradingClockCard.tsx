import { Clock } from "lucide-react";
import { asObject } from "@/lib/asArray";
import { offsetChoices, offsetToSave, selectedOffset } from "./clockOffsets";

interface TradingClockCardProps {
  clock: Record<string, unknown>;
  onSetOffset: (minutes: number | null) => void;
}

/**
 * The clock every window time below is read against.
 *
 * Leaving this unstated is how a schedule screen ends up describing hours in
 * an unknown timezone (simon-handover/017). On the machine the user is sitting
 * at it is simply that machine's clock; on a VPS in another country it is that
 * country's time, so "09:00" in a window would mean 09:00 there.
 */
export function TradingClockCard({ clock, onSetOffset }: TradingClockCardProps) {
  const desc = asObject(clock);
  const configured = desc["configured"];
  const following = desc["following_machine"] === true;
  const label = String(desc["label"] ?? "an unknown clock");
  const now = typeof desc["now"] === "string" ? desc["now"] : null;

  return (
    <section className="rounded-lg border border-line bg-surface-1 p-3">
      <div className="mb-1 flex items-center gap-2">
        <Clock className="h-4 w-4 text-accent" aria-hidden />
        <h3 className="text-sm font-semibold text-ink-1">Trading Clock</h3>
      </div>
      <p className="mb-2 text-[11px] text-ink-3">
        {now ? `Now ${now.replace("T", " ").slice(0, 16)} — ` : ""}
        {label}, {following ? "following this machine's own clock" : "set here"}.
        {" "}A fixed offset does not follow daylight saving on its own.
      </p>
      <label className="text-xs text-ink-2">
        Clock
        <select
          aria-label="Trading clock"
          value={selectedOffset(
            typeof configured === "number" ? configured : null,
          )}
          onChange={(e) => onSetOffset(offsetToSave(e.target.value))}
          className="ml-2 rounded border border-line bg-surface-2 px-2 py-1 text-xs text-ink-1"
        >
          {offsetChoices().map((c) => (
            <option key={c.value} value={c.value}>{c.label}</option>
          ))}
        </select>
      </label>
    </section>
  );
}
