import { Sparkles, TriangleAlert } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { PanelShell } from "@/components/shared/PanelShell";
import { cn } from "@/lib/cn";
import { useAiController } from "./hooks/useAiController";
import { AnswerSection } from "./internal/AnswerSection";
import { EvidenceSection } from "./internal/EvidenceSection";

const WINDOWS = [7, 30, 90];

export function AiPanel() {
  const c = useAiController();

  const cannotAsk = !c.meta?.configured
    ? "No AI provider is configured. Add one under Settings → AI."
    : null;

  return (
    <PanelShell
      icon="bot"
      title="AI Analysis"
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
        <EmptyState title={c.refusal ? "Could not load this tab" : "Loading"} hint={c.refusal ?? undefined} />
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-1.5">
            {c.meta.subjects.map((s) => (
              <button
                key={s.id}
                onClick={() => c.setSubject(s.id)}
                aria-pressed={s.id === c.subject}
                className={cn(
                  "rounded border px-2 py-1 text-[11px] transition-colors",
                  s.id === c.subject
                    ? "border-accent bg-accent/15 text-ink-1"
                    : "border-line bg-surface-1 text-ink-3 hover:text-ink-2",
                )}
              >
                {s.label}
              </button>
            ))}
          </div>

          <EvidenceSection subject={c.subject} evidence={c.evidence} />

          <div className="flex items-center gap-3 border-t border-line pt-3">
            <Button
              variant="primary"
              onClick={() => void c.analyse()}
              disabled={c.asking}
              disabledReason={cannotAsk}
            >
              <Sparkles size={13} />
              {c.asking ? "Asking the model…" : "Ask the model"}
            </Button>
            <span className="flex items-center gap-1 text-[11px] text-warning">
              <TriangleAlert size={12} />
              This call is billed by {c.meta.provider || "your provider"}. The numbers above are free.
            </span>
          </div>

          {c.refusal && (
            <p role="alert" className="rounded border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
              {c.refusal}
            </p>
          )}
          {/* The prompt asks for JSON, so the answer arrives as a minified
              object -- which is what this panel used to render on screen, as
              text. AnswerSection breaks it into the sections it was asked for,
              and falls back to the raw text for anything else. */}
          {c.answer && <AnswerSection answer={c.answer} />}
        </div>
      )}
    </PanelShell>
  );
}
