import {
  ArrowRight, ChartCandlestick, FileText, Radio, ShieldAlert, Target, TriangleAlert, Zap,
} from "lucide-react";
import type { ReactNode } from "react";
import type { MarketAnalysis } from "../hooks/useMarketResearch";

/** Icon and colour per card. The colours are the frozen semantic ones: amber
 *  for what moves price, red for risk, gold for the app's own advice. */
function Card({ title, children, testId, Icon, tone = "text-accent" }: {
  title: string; children: ReactNode; testId?: string;
  Icon: typeof ArrowRight; tone?: string;
}) {
  return (
    <div
      data-testid={testId}
      className="min-w-56 flex-1 rounded-lg border border-line bg-surface-2 p-4 shadow-sm"
    >
      <div className="flex items-center gap-2">
        <span
          aria-hidden
          className={`flex size-6 shrink-0 items-center justify-center rounded-md bg-surface-3 ${tone}`}
        >
          <Icon size={13} />
        </span>
        <h3 className="text-[10px] font-semibold uppercase tracking-wider text-ink-3">
          {title}
        </h3>
      </div>
      <div className="mt-2">{children}</div>
    </div>
  );
}

function Bullets({ items, tone, Icon, limit }: {
  items: string[]; tone: string; Icon: typeof ArrowRight; limit: number;
}) {
  return (
    <>
      {items.slice(0, limit).map((item, i) => (
        <div key={i} className="mb-1 flex items-start gap-2">
          <Icon size={13} className={`mt-0.5 shrink-0 ${tone}`} />
          <span className="text-xs leading-relaxed text-ink-1">{item}</span>
        </div>
      ))}
    </>
  );
}

/**
 * Everything the model said in words, as cards rather than a wall of prose.
 *
 * This is the part of the tab the owner reported: the port printed whatever
 * the model returned as one block of text. The model is asked for a
 * structured answer and returns one, so each field goes in the card it
 * belongs to, and a field that came back empty renders nothing rather than an
 * empty heading.
 */
export function ResearchNarrativeSection({ analysis }: { analysis: MarketAnalysis }) {
  const drivers = analysis.key_drivers ?? [];
  const risks = analysis.risk_factors ?? [];
  const strategyName = analysis.strategy_label
    || analysis.strategy_recommendation
    || "";

  return (
    <div className="space-y-3">
      {analysis.summary && (
        <Card title="Executive summary" Icon={FileText}>
          <p className="text-xs leading-relaxed text-ink-1">{analysis.summary}</p>
        </Card>
      )}

      <div className="flex flex-wrap items-stretch gap-3">
        {drivers.length > 0 && (
          <Card title="What could move gold today" Icon={Zap} tone="text-warning">
            {/* Eight, as the NiceGUI card showed. Past that it stops being a
                list of what matters. */}
            <Bullets items={drivers} tone="text-warning" Icon={ArrowRight} limit={8} />
          </Card>
        )}
        {analysis.technical_summary && (
          <Card title="Technical analysis" Icon={ChartCandlestick} tone="text-series-2">
            <p className="text-xs leading-relaxed text-ink-1">
              {analysis.technical_summary}
            </p>
          </Card>
        )}
        {risks.length > 0 && (
          <Card title="Risk factors" Icon={ShieldAlert} tone="text-loss">
            <Bullets items={risks} tone="text-loss" Icon={TriangleAlert} limit={6} />
          </Card>
        )}
      </div>

      {strategyName && (
        <Card title="Recommended strategy" testId="strategy-recommendation" Icon={Target}>
          <p className="text-sm font-semibold text-accent">{strategyName}</p>
          {analysis.strategy_reason && (
            <p className="mt-1 text-xs leading-relaxed text-ink-1">
              {analysis.strategy_reason}
            </p>
          )}
          {analysis.strategy_summary && (
            <p className="mt-1 text-[11px] italic text-ink-3">
              {analysis.strategy_summary}
            </p>
          )}
        </Card>
      )}

      {/* "—" is what the service returns when it has nothing to say about the
          signals, and a card containing a dash is a card that wasted a line. */}
      {analysis.signal_analysis && analysis.signal_analysis !== "—" && (
        <Card title="Signal analysis" Icon={Radio} tone="text-ink-2">
          <p className="text-xs leading-relaxed text-ink-1">{analysis.signal_analysis}</p>
        </Card>
      )}
    </div>
  );
}
