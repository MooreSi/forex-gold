import { Brain } from "lucide-react";
import { formatClock } from "@/components/shared/format";
import { asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

interface ModelSectionProps {
  model: Record<string, unknown>;
}

/**
 * What the pro-signal model actually is: fitted or not, how well, and why not.
 *
 * **It was reporting the wrong thing entirely.** This section read `trained`
 * and `samples`, and `pro_model.status()` returns neither — the real keys are
 * `ready`, `auc`, `n`, `reason`, `fitted_at`, `corpus` and `min_auc`. So the
 * panel said "not trained yet" on every install regardless, and hid the fact
 * that a live account had 10,116 labelled samples waiting, an AUC gate at
 * 0.55, and a stated reason. The same class of bug as the Trading tab reading
 * `circuit_breaker.tripped`, a key that has never existed.
 *
 * `reason` is shown whenever the model is not ready. "Not ready" with no
 * reason is the difference between "give it time" and "it will never fit
 * because the corpus is one-sided".
 */
function Figure({ label, value, hint, tone }: {
  label: string; value: string; hint?: string; tone?: string;
}) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-ink-3">{label}</p>
      <p className={cn("num text-sm font-semibold", tone ?? "text-ink-1")}>{value}</p>
      {hint && <p className="text-[10px] text-ink-3">{hint}</p>}
    </div>
  );
}

function num(value: unknown): number | null {
  // `Number(null)` is 0 and `Number("")` is 0, both finite. Without the first
  // line an absent AUC renders as 0.000 -- a specific, terrible model --
  // which is exactly the wrong answer for "never measured". Caught by
  // ModelSection.test.tsx > "shows a dash rather than a zero".
  if (value == null || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

export function ModelSection({ model }: ModelSectionProps) {
  const ready = model["ready"] === true;
  const auc = num(model["auc"]);
  const minAuc = num(model["min_auc"]);
  const fittedAt = num(model["fitted_at"]);
  const corpus = asObject(model["corpus"]);

  const labelled = (num(corpus["pos"]) ?? 0) + (num(corpus["neg"]) ?? 0);
  const settled = (num(corpus["wins"]) ?? 0) + (num(corpus["losses"]) ?? 0);

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Brain size={14} className="text-accent" aria-hidden />
        <h3 className="text-xs font-semibold text-ink-1">Pro-signal model</h3>
        <span
          data-testid="model-state"
          className={cn(
            "rounded px-1.5 py-0.5 text-[10px] font-semibold",
            ready ? "bg-profit/15 text-profit" : "bg-warning/15 text-warning",
          )}
        >
          {ready ? "in use" : "not in use"}
        </span>
      </div>

      {!ready && model["reason"] != null && String(model["reason"]) && (
        // "Not ready" with no reason is the difference between "give it time"
        // and "it will never fit".
        <p data-testid="model-reason" className="text-[11px] text-warning">
          {String(model["reason"])}
        </p>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Figure
          label="AUC"
          value={auc == null ? "—" : auc.toFixed(3)}
          hint={minAuc == null ? undefined : `gate ${minAuc.toFixed(2)}`}
          tone={auc == null ? "text-ink-3"
            : minAuc != null && auc < minAuc ? "text-loss" : "text-profit"}
        />
        <Figure label="Fitted on" value={num(model["n"]) == null ? "—" : String(num(model["n"]))}
          hint="samples" />
        <Figure label="Labelled corpus" value={labelled ? labelled.toLocaleString("en-GB") : "—"}
          hint={`${num(corpus["pos"]) ?? 0} pro / ${num(corpus["neg"]) ?? 0} not`} />
        <Figure label="Settled outcomes" value={settled ? settled.toLocaleString("en-GB") : "—"}
          hint={`${num(corpus["pending"]) ?? 0} still open`} />
      </div>

      <p className="text-[10px] text-ink-3">
        {fittedAt
          ? `Last fitted at ${formatClock(fittedAt)}.`
          : "Never fitted on this install."}{" "}
        A model below the AUC gate is not used at all — it is not a weaker
        filter, it is no filter.
      </p>
    </div>
  );
}
