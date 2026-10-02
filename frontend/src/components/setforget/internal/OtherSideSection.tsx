import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import type { SetForgetCandidate } from "@/api/types";
import { formatPrice } from "@/components/shared/format";
import { waitFor } from "./waitFor";

const STAGE_WORDS = {
  armed: "price has not reached the zone",
  waiting: "at the zone, waiting for the 30m",
  triggered: "triggered",
} as const;

/**
 * The direction the page is NOT showing, in one line.
 *
 * Both directions since 2026-10-02: the backend builds a BUY and a SELL and
 * ranks them. Before, it built only the Weekly/Daily side, so a SELL armed far
 * above price hid a BUY the market was sitting on, and nothing said so.
 */
export function OtherSideSection({ candidate }: { candidate: SetForgetCandidate | null }) {
  if (!candidate) return null;
  const long = candidate.direction === "BUY";
  const Arrow = long ? ArrowUpRight : ArrowDownRight;
  return (
    <p
      role="note"
      className="flex flex-wrap items-center gap-1.5 rounded-md border border-line
                 bg-surface-2/50 px-3 py-2 text-[11px] text-ink-2"
    >
      <span className="text-ink-3">Also watching:</span>
      <span className={long ? "font-semibold text-profit" : "font-semibold text-loss"}>
        <Arrow size={12} className="inline" /> {candidate.direction}
      </span>
      <span className="num">at {formatPrice(candidate.entry)}</span>
      <span className="text-ink-3">·</span>
      <span>{STAGE_WORDS[candidate.stage] ?? STAGE_WORDS.armed}</span>
      {candidate.stage === "armed" && (
        <>
          <span className="text-ink-3">·</span>
          <span className="num">{candidate.distance.toFixed(2)} points away</span>
          <span className="text-ink-3">({waitFor(candidate.distance_days)})</span>
        </>
      )}
    </p>
  );
}
