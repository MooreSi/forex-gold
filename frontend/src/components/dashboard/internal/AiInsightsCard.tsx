import { Check, Minus } from "lucide-react";
import type { SetForgetState } from "@/api/types";
import { formatPrice } from "@/components/shared/format";
import type { MarketAnalysis } from "@/components/ai/hooks/useMarketResearch";
import { DashCard, Pill, Reading } from "./DashCard";

interface AiInsightsCardProps {
  setforget: SetForgetState | null;
  analysis: MarketAnalysis | null;
  savedAt: string;
}

const GRADE_TONE: Record<string, "profit" | "warning" | "loss"> = {
  high: "profit", moderate: "warning", low: "loss",
};

/**
 * What the measured read and the last model call each say.
 *
 * Two different kinds of statement, kept visibly apart:
 *
 * - the **checklist** figures — bias, confluence, the zone price is nearest —
 *   are measured by the backend from candles. They cost nothing and they are
 *   the same numbers the Set & Forget section shows, through the same key.
 * - the **model's** view is read back from whatever the AI Analysis tab last
 *   produced. This card never asks for a new one: the dashboard is the tab
 *   left open all day, and a screen that bills by being looked at is a screen
 *   nobody can leave open.
 *
 * Neither is in the order path, and the footnote says so. The AI advises; the
 * rules and the risk governor decide.
 */
export function AiInsightsCard({ setforget, analysis, savedAt }: AiInsightsCardProps) {
  const evidence = setforget?.evidence;
  const confluence = setforget?.confluence;
  // The span the zones cover, NOT "the nearest zone". The backend picks the
  // nearest few by its own measure (`aoi.distance`) and then sorts what it
  // picked by price, so `zones[0]` is the LOWEST of them and calling it the
  // nearest would be a confident, wrong label. Re-deriving the distance here
  // would be a second answer to a question the backend has answered.
  const zones = evidence?.zones ?? [];
  const band = zones.length
    ? { low: zones[0].low, high: zones[zones.length - 1].high }
    : null;
  const grade = confluence?.grade ?? null;

  return (
    <DashCard
      title="AI & method insights"
      icon="ai"
      badge={grade
        ? <Pill tone={GRADE_TONE[grade] ?? "neutral"}>{grade} confluence</Pill>
        : null}
      footnote="Advisory only — no AI output reaches the order path. Ask for fresh research on the AI Analysis tab."
    >
      <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
        <Reading
          label="Entry bias"
          value={titleCase(evidence?.entry_bias)}
          tone={biasTone(evidence?.entry_bias)}
          hint="The 4H read the method enters on."
        />
        <Reading
          label="Daily bias"
          value={titleCase(evidence?.daily_bias)}
          tone={biasTone(evidence?.daily_bias)}
        />
        <Reading
          label="Zones in play"
          value={band ? `${formatPrice(band.low)}–${formatPrice(band.high)}` : "—"}
          hint={band
            ? `The range the ${zones.length} supply and demand bands cover. The Set & Forget section draws each one.`
            : "No zone has formed on this read."}
        />
        <Reading
          label="Confluence"
          value={confluence?.max
            ? `${confluence.score}/${confluence.max}`
            : "—"}
          hint="How many of the method's checks this setup passes."
        />
      </dl>

      <div className="mt-2.5 space-y-1 border-t border-line/70 pt-2">
        {analysis ? (
          <>
            <Line
              ok
              label="Model sentiment"
              value={[titleCase(analysis.sentiment),
                      analysis.sentiment_confidence != null
                        ? `${Math.round(analysis.sentiment_confidence * 100)}% confident`
                        : null].filter(Boolean).join(" · ") || "—"}
            />
            <Line
              ok
              label="Today's bias"
              value={titleCase(analysis.today_bias)}
            />
            {analysis.price_low != null && analysis.price_high != null && (
              <Line
                ok
                label="Expected range"
                value={`${formatPrice(analysis.price_low)} – ${formatPrice(analysis.price_high)}`}
              />
            )}
            {savedAt && (
              <p className="pt-0.5 text-[10px] text-ink-3">
                from the research of {describe(savedAt)}
              </p>
            )}
          </>
        ) : (
          <Line
            ok={false}
            label="Model view"
            value="none stored — run it on the AI Analysis tab"
          />
        )}
      </div>
    </DashCard>
  );
}

function Line({ ok, label, value }: { ok: boolean; label: string; value: string }) {
  return (
    <p className="flex items-center gap-1.5 text-[11px]">
      {ok
        ? <Check size={11} className="shrink-0 text-profit" aria-hidden />
        : <Minus size={11} className="shrink-0 text-ink-3" aria-hidden />}
      <span className="text-ink-3">{label}</span>
      <span className="ml-auto truncate text-right font-medium text-ink-1">{value}</span>
    </p>
  );
}

/** Bullish green, bearish red, anything else neutral — the app's one meaning
 *  for those colours, not a new one. */
function biasTone(bias: string | null | undefined): string | undefined {
  const b = String(bias ?? "").toLowerCase();
  if (b.includes("bull")) return "text-profit";
  if (b.includes("bear")) return "text-loss";
  return undefined;
}

function titleCase(value: string | null | undefined): string {
  const v = String(value ?? "").trim();
  if (!v) return "—";
  return v.charAt(0).toUpperCase() + v.slice(1);
}

function describe(iso: string): string {
  const when = Date.parse(iso);
  if (Number.isNaN(when)) return iso;
  return new Date(when).toLocaleString(undefined, {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}
