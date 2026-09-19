import { Sparkles, TriangleAlert } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { iconFor } from "@/components/shared/icons";
import { AnswerSection } from "./AnswerSection";
import type { SubjectState } from "../hooks/useAiController";

interface SubjectSectionProps {
  id: string;
  label: string;
  blurb: string;
  icon: string;
  state: SubjectState;
  provider: string;
  cannotAsk: string | null;
  onAsk: () => void;
  children: React.ReactNode;
}

/**
 * One analysis subject: its icon, its free numbers, its own Ask button, its
 * answer.
 *
 * **Its own Ask button is the point of the section.** The page shows all three
 * subjects at once, which is what the NiceGUI page did and what the owner
 * asked for back — but one page must not mean three bills, so nothing is sent
 * to a model until a specific section is asked.
 *
 * The free/paid line is stated at the button rather than once at the top. An
 * operator scrolling to the third section should not have to remember a
 * warning from the first.
 */
export function SubjectSection({
  id, label, blurb, icon, state, provider, cannotAsk, onAsk, children,
}: SubjectSectionProps) {
  const Icon = iconFor(icon);

  return (
    <section data-testid={`subject-${id}`}
      className="rounded-lg border border-line bg-surface-1">
      <header className="flex flex-wrap items-center gap-2 border-b border-line bg-surface-2/40 px-4 py-2.5">
        {Icon && (
          <span aria-hidden
            className="flex size-7 shrink-0 items-center justify-center rounded-md bg-accent/10 text-accent">
            <Icon size={15} />
          </span>
        )}
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-ink-1">{label}</h3>
          <p className="truncate text-[11px] text-ink-3">{blurb}</p>
        </div>
        <Button
          className="ml-auto"
          onClick={onAsk}
          disabled={state.asking}
          disabledReason={cannotAsk}
          title={`Sends these numbers to ${provider || "the configured model"}. This costs money.`}
        >
          <Sparkles size={13} />
          {state.asking ? "Asking the model…" : "Ask the model"}
        </Button>
      </header>

      <div className="space-y-3 p-4">
        {children}

        {state.refusal && (
          <p role="alert"
            className="flex items-start gap-1.5 rounded border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            <TriangleAlert size={13} className="mt-0.5 shrink-0" />
            {state.refusal}
          </p>
        )}

        {state.answer
          ? <AnswerSection answer={state.answer} />
          : (
            <p className="text-[11px] text-ink-3">
              The numbers above are free. Asking {provider || "a model"} to read
              them is billed.
            </p>
          )}
      </div>
    </section>
  );
}
