import { CheckCircle2, MinusCircle, TrendingDown, TrendingUp, XCircle } from "lucide-react";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";

interface AnswerSectionProps {
  answer: string;
}

interface EngineVerdict {
  name?: string;
  verdict?: string;
  trend?: string;
  ml_contribution?: string;
  self_learning_progress?: string;
  acting_like_pro_trader?: boolean;
  key_strength?: string;
  key_weakness?: string;
  recommendation?: string;
}

/**
 * The model's answer, broken into the sections it was asked for.
 *
 * **The prompt has always demanded JSON.** `_SIGNAL_GEN_SYSTEM` ends with
 * "Respond ONLY with a single minified JSON object matching this exact
 * schema", so what the React panel was rendering in a `whitespace-pre-wrap`
 * block was a minified JSON object, on screen, as text. The NiceGUI page
 * rendered the same answer as a score card and a section per engine; this is
 * that, for the schema the prompt actually asks for.
 *
 * **A prose answer is shown, not hidden.** A model can always return something
 * else — a refusal, a wrapped code fence, an apology — and an answer the
 * operator paid for must reach the screen whatever shape it arrived in.
 * Falling back to the raw text is the whole reason this is safe to add.
 */
const TREND_ICON: Record<string, typeof TrendingUp> = {
  improving: TrendingUp,
  declining: TrendingDown,
  stable: MinusCircle,
  insufficient_data: MinusCircle,
};

const TREND_TONE: Record<string, string> = {
  improving: "text-profit",
  declining: "text-loss",
  stable: "text-ink-2",
  insufficient_data: "text-ink-3",
};

/** The JSON object the prompt asked for, or null for anything else. */
export function parseAnswer(answer: string): Record<string, unknown> | null {
  const text = (answer ?? "").trim();
  if (!text) return null;
  // A model that wraps its JSON in a ```json fence has still answered.
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/);
  const body = (fenced?.[1] ?? text).trim();
  // No `startsWith("{")` pre-check: the object test below already rejects a
  // bare list, and mutation testing showed the two masked each other -- with
  // both present, neither could be shown to matter.
  try {
    const parsed: unknown = JSON.parse(body);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  if (!children) return null;
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-ink-3">{title}</p>
      <p className="text-xs leading-relaxed text-ink-1">{children}</p>
    </div>
  );
}

function EngineCard({ engine }: { engine: EngineVerdict }) {
  const trend = String(engine.trend ?? "");
  const Icon = TREND_ICON[trend] ?? MinusCircle;
  const pro = engine.acting_like_pro_trader === true;

  return (
    <section
      data-testid={`engine-verdict-${engine.name ?? "?"}`}
      className="rounded-lg border border-line bg-surface-1 p-3"
    >
      <header className="mb-2 flex flex-wrap items-center gap-2">
        <h4 className="text-xs font-semibold text-ink-1">{engine.name || "Unnamed engine"}</h4>
        <span className={cn("flex items-center gap-1 text-[10px]", TREND_TONE[trend] ?? "text-ink-3")}>
          <Icon size={11} />
          {trend.replace(/_/g, " ") || "no trend given"}
        </span>
        <span
          data-testid={`pro-${engine.name ?? "?"}`}
          className={cn("ml-auto flex items-center gap-1 text-[10px]",
            pro ? "text-profit" : "text-ink-3")}
        >
          {pro ? <CheckCircle2 size={11} /> : <XCircle size={11} />}
          {pro ? "trading like a professional" : "not yet a professional"}
        </span>
      </header>

      <div className="grid gap-2 sm:grid-cols-2">
        <Block title="Verdict">{engine.verdict}</Block>
        <Block title="What the ML is contributing">{engine.ml_contribution}</Block>
        <Block title="Self-learning progress">{engine.self_learning_progress}</Block>
        <Block title="Strongest thing it does">{engine.key_strength}</Block>
        <Block title="Weakest thing it does">{engine.key_weakness}</Block>
        <Block title="What to change">{engine.recommendation}</Block>
      </div>
    </section>
  );
}

export function AnswerSection({ answer }: AnswerSectionProps) {
  const parsed = parseAnswer(answer);

  if (!parsed) {
    // Not an error, and not hidden. The operator paid for this.
    return (
      <article
        data-testid="ai-answer"
        className="whitespace-pre-wrap rounded border border-line bg-surface-2 px-4 py-3 text-xs leading-relaxed text-ink-1"
      >
        {answer}
      </article>
    );
  }

  const engines = asArray<EngineVerdict>(parsed["engines"]);

  return (
    <div data-testid="ai-answer" className="space-y-3">
      {typeof parsed["overall_assessment"] === "string" && (
        <p data-testid="overall-assessment"
          className="rounded-lg border border-accent/30 bg-accent/5 px-4 py-3 text-xs leading-relaxed text-ink-1">
          {parsed["overall_assessment"]}
        </p>
      )}

      {engines.map((e, i) => <EngineCard key={String(e.name ?? i)} engine={e} />)}

      <div className="grid gap-3 sm:grid-cols-2">
        {typeof parsed["collective_verdict"] === "string" && (
          <div data-testid="collective-verdict"
            className="rounded-lg border border-line bg-surface-1 p-3">
            <p className="text-[10px] uppercase tracking-wider text-ink-3">
              Collective verdict
            </p>
            <p className="text-xs leading-relaxed text-ink-1">
              {parsed["collective_verdict"]}
            </p>
          </div>
        )}
        {typeof parsed["what_would_make_them_professional"] === "string" && (
          <div data-testid="what-would-make-them-professional"
            className="rounded-lg border border-line bg-surface-1 p-3">
            <p className="text-[10px] uppercase tracking-wider text-ink-3">
              What would make them professional
            </p>
            <p className="text-xs leading-relaxed text-ink-1">
              {parsed["what_would_make_them_professional"]}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
