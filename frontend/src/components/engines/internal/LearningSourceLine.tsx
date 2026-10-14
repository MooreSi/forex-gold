interface LearningSourceLineProps {
  /** `ml.metrics.learning` from either engine's report (docs/todo/017). */
  learning: Record<string, unknown> | undefined;
}

function num(value: unknown): number | null {
  if (value == null || value === "") return null;
  const v = Number(value);
  return Number.isFinite(v) ? v : null;
}

/**
 * What the model learns from: its label, how its training rows are weighted
 * and how many rows those weights are worth, whether real trades are on the
 * chart and in the training set, and (Breakout) whether it has earned an
 * opinion. The learning switches change all of this without changing how
 * the chart looks.
 */
export function LearningSourceLine({ learning }: LearningSourceLineProps) {
  if (!learning) return null;
  const parts: string[] = [];
  if (learning["label"]) parts.push(`Learns ${String(learning["label"])}`);
  if (learning["weights"]) parts.push(`weights ${String(learning["weights"])}`);
  const eff = num(learning["effective_n"]);
  if (eff != null) parts.push(`last fit worth about ${Math.round(eff)} rows`);
  const executed = num(learning["executed_plotted"]);
  if (executed != null && executed > 0) {
    parts.push(
      `${executed} real trades plotted; the model ` +
        (learning["learns_from_executed"] ? "learns from them" : "does not learn from them"),
    );
  }
  if (learning["armed"] === false) {
    const auc = num(learning["holdout_auc"]);
    parts.push(
      "No opinion yet" + (auc != null ? ` (newest-quarter AUC ${auc.toFixed(2)})` : "") +
        ", so the gate lets signals through",
    );
  }
  if (parts.length === 0) return null;
  return (
    <p data-testid="learning-source" className="mt-1 text-[10px] leading-relaxed text-ink-2">
      {parts.join(" · ")}.
    </p>
  );
}
