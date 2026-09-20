import { Search } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { useMarketResearch } from "./hooks/useMarketResearch";
import { PriceTargetSection } from "./internal/PriceTargetSection";
import { ResearchNarrativeSection } from "./internal/ResearchNarrativeSection";
import { SentimentSection } from "./internal/SentimentSection";

/** When the stored analysis was produced, in the operator's own timezone. */
function describeSavedAt(iso: string): string {
  if (!iso) return "";
  const when = Date.parse(iso);
  if (Number.isNaN(when)) return "";
  return `Last research: ${new Date(when).toLocaleString(undefined, {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  })}`;
}

/**
 * AI Analysis — one call, one readable answer.
 *
 * The NiceGUI tab this restores had a single Research Now button: it asked
 * the configured model ONCE about sentiment, the day's range, the drivers,
 * the risks, the levels and which strategy to run, and rendered the structured
 * answer as cards. The React port shipped a different page here entirely (the
 * three-subject trade analysis, which in the original lives under the Analysis
 * tab) and printed the model's raw prose. Both were reported on 2026-09-20.
 *
 * **Opening this tab costs nothing.** The last analysis is read back from the
 * database; only the button bills.
 */
export function AiPanel() {
  const r = useMarketResearch();

  return (
    <PanelShell
      icon="bot"
      title="AI Market Analysis"
      subtitle={describeSavedAt(r.savedAt) || "XAUUSD Gold"}
      actions={
        <Button
          variant="primary"
          onClick={() => void r.research()}
          disabled={r.researching}
          className="gap-1.5"
        >
          <Search size={13} />
          {r.researching ? "Researching..." : "Research Now"}
        </Button>
      }
    >
      {r.refusal && (
        <p className="mb-3 rounded border border-loss/40 bg-loss/10 px-3 py-2 text-xs text-loss">
          {r.refusal}
        </p>
      )}

      {r.researching && !r.analysis && (
        <EmptyState
          title="Researching gold market conditions..."
          hint="The model is reading price, candles, signals and the news. This can take up to 30 seconds."
        />
      )}

      {!r.analysis ? (
        !r.researching && (
          <EmptyState
            title="Press Research Now for an AI analysis of the gold market"
            hint="Sentiment, today's range, what could move it, the risks, the levels and a strategy recommendation — one model call, not one per metric."
          />
        )
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-stretch gap-3">
            <div className="min-w-56 flex-1">
              <SentimentSection analysis={r.analysis} />
            </div>
            <div className="min-w-56 flex-1">
              <PriceTargetSection analysis={r.analysis} />
            </div>
          </div>

          <ResearchNarrativeSection analysis={r.analysis} />

          <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
            <span className="text-[11px] text-ink-3">
              {[
                describeSavedAt(r.savedAt),
                r.analysis.fetched_news
                  ? `${r.analysis.news_count ?? 0} news headlines included`
                  : "",
                // The service sets `error` when it answered with a partial
                // schema. Saying so is the difference between a short
                // analysis and a broken one.
                r.analysis.error ? "Analysis may be incomplete" : "",
              ].filter(Boolean).join("  |  ")}
            </span>
            {analysisDisclaimer(r.analysis.disclaimer)}
          </div>
        </div>
      )}
    </PanelShell>
  );
}

/** Never dropped when the model returns one: this tab is one step from an
 *  order ticket, and the wording is the provider's, not ours. */
function analysisDisclaimer(text: string | undefined) {
  if (!text) return null;
  return <span className="text-[11px] italic text-ink-3">{text}</span>;
}
