import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { cn } from "@/lib/cn";
import { useAiController } from "./hooks/useAiController";
import { EvidenceSection } from "./internal/EvidenceSection";
import { GeneratorEvidence } from "./internal/GeneratorEvidence";
import { StrategyEvidence } from "./internal/StrategyEvidence";
import { SubjectSection } from "./internal/SubjectSection";

const WINDOWS = [7, 30, 90];

/**
 * One page, three subjects — the shape the NiceGUI page had.
 *
 * All three sets of numbers load together because reading them is free. No
 * model is called until a specific section's Ask button is pressed, so one
 * page does not mean three bills.
 *
 * **This is not the AI Analysis tab.** In the NiceGUI app it was a section of
 * the Analysis tab (`frontend/pages/history/__init__.py` renders
 * `ai_trade_analysis`), and the AI tab was the single-call market research
 * this port put back on 2026-09-20. The React port had them the wrong way
 * round, so the tab named for one showed the other.
 */
const SUBJECTS: Record<string, { blurb: string; icon: string }> = {
  channels: {
    icon: "channels",
    blurb: "What each Telegram channel sent, claimed, and actually produced.",
  },
  strategies: {
    icon: "percent",
    blurb: "Adaptive position management against the fixed exit strategies.",
  },
  generator: {
    icon: "flask",
    blurb: "Whether this app's own engines are getting better over time.",
  },
};

export function TradeAnalysisPanel() {
  const c = useAiController();

  const cannotAsk = !c.meta?.configured
    ? "No AI provider is configured. Add one under Settings → AI."
    : null;

  return (
    <PanelShell
      icon="bot"
      title="AI trade analysis"
      subtitle={
        c.meta?.configured
          ? `${c.meta.provider} · ${c.meta.model}`
          : "no provider configured"
      }
      actions={WINDOWS.map((d) => (
        <button
          key={d}
          onClick={() => c.setDays(d)}
          aria-pressed={d === c.days}
          className={cn(
            "num rounded px-2 py-1 text-[11px] transition-colors",
            d === c.days
              ? "bg-surface-3 text-ink-1"
              : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
          )}
        >
          {d}d
        </button>
      ))}
    >
      {!c.meta ? (
        <EmptyState title={c.refusal ? "Could not load this tab" : "Loading"}
          hint={c.refusal ?? undefined} />
      ) : (
        <div className="space-y-4">
          {c.meta.subjects.map(({ id, label }) => {
            const spec = SUBJECTS[id] ?? { blurb: "", icon: "bot" };
            const state = c.stateFor(id);
            return (
              <SubjectSection
                key={id}
                id={id}
                label={label}
                blurb={spec.blurb}
                icon={spec.icon}
                state={state}
                provider={c.meta!.provider}
                cannotAsk={cannotAsk}
                onAsk={() => void c.analyse(id)}
              >
                {/* Each subject's evidence has its own shape and its own
                    table. A shared renderer here would be raw JSON again. */}
                {id === "strategies" ? <StrategyEvidence evidence={state.evidence} />
                  : id === "generator" ? <GeneratorEvidence evidence={state.evidence} />
                    : <EvidenceSection subject={id} evidence={state.evidence} />}
              </SubjectSection>
            );
          })}
        </div>
      )}
    </PanelShell>
  );
}
