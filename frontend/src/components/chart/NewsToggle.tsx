import { Newspaper } from "lucide-react";
import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import type { ChartNews } from "./hooks/useChartNews";
import { NEWS_IMPACTS } from "./internal/newsMarks";

const IMPACT_LABEL: Record<string, { short: string; help: string; on: string }> = {
  high: { short: "H", help: "High-impact releases", on: "bg-loss/20 text-loss" },
  medium: { short: "M", help: "Medium-impact releases", on: "bg-warning/20 text-warning" },
  low: { short: "L", help: "Low-impact releases", on: "bg-accent/20 text-accent" },
};

/**
 * The charts' NEWS button and, while it is on, which events it marks. At the
 * chart domain's root rather than in internal/ because the Dashboard's chart
 * card uses it too.
 */
export function NewsToggle({ news, compact = false }: { news: ChartNews; compact?: boolean }) {
  const chip = compact ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-1 text-[11px]";
  return (
    <div className="flex items-center gap-0.5">
      <Tooltip
        label="Mark economic-calendar releases on the chart, coloured by impact. Hover a marked candle for the events and how price moved after them."
        side="bottom"
      >
        <button
          onClick={news.toggle}
          aria-pressed={news.on}
          className={cn(
            "flex items-center gap-1 rounded font-semibold tracking-wide transition-colors", chip,
            news.on ? "bg-accent/20 text-accent" : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
          )}
        >
          <Newspaper size={compact ? 10 : 12} /> NEWS
        </button>
      </Tooltip>
      {news.on && (
        <>
          {NEWS_IMPACTS.map((impact) => (
            <Tooltip key={impact} label={IMPACT_LABEL[impact].help} side="bottom">
              <button
                onClick={() => news.toggleImpact(impact)}
                aria-pressed={news.impacts.includes(impact)}
                aria-label={`${impact} impact`}
                className={cn(
                  "num rounded transition-colors", chip,
                  news.impacts.includes(impact)
                    ? IMPACT_LABEL[impact].on
                    : "text-ink-3 hover:bg-surface-2",
                )}
              >
                {IMPACT_LABEL[impact].short}
              </button>
            </Tooltip>
          ))}
          <Tooltip
            label={news.allCurrencies
              ? "Every currency's releases. Click for USD and gold only."
              : "USD and gold releases only, as the news blackout uses. Click for every currency."}
            side="bottom"
          >
            <button
              onClick={() => news.setAllCurrencies(!news.allCurrencies)}
              aria-label="Currencies"
              className={cn("rounded text-ink-2 hover:bg-surface-2", chip)}
            >
              {news.allCurrencies ? "All" : "USD"}
            </button>
          </Tooltip>
        </>
      )}
    </div>
  );
}
