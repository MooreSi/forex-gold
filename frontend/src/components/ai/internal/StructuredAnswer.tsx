import { cn } from "@/lib/cn";

/**
 * Any JSON answer, as labelled sections.
 *
 * There are three analysis subjects and each has its own schema — a channel
 * report with a reliability score and phantom-TP rows, a DPM-versus-fixed
 * verdict, and the signal-generator engine cards. Writing a renderer per
 * schema means a fourth subject arrives as raw JSON on screen, which is
 * exactly the state this whole area was in before 2026-09-19.
 *
 * So this renders the SHAPE rather than the schema: a string is a paragraph,
 * a nested object is a block of sub-fields, a list of objects is a small
 * table, a list of strings is a list. Keys are humanised. The one schema that
 * genuinely benefits from bespoke treatment — the engine cards — keeps it, in
 * AnswerSection.
 *
 * It is deliberately incapable of hiding a field. Anything the model was
 * asked for and returned is shown, because a field the prompt paid for and
 * the screen discards is the failure this replaced.
 */
const SCORE_KEYS = new Set(["reliability_score"]);

export function humanise(key: string): string {
  const words = key.replace(/_/g, " ").trim();
  if (!words) return key;
  // Acronyms the app uses. Left uppercase because "Rr analysis" and "Dpm
  // assessment" read as typos.
  const fixed = words
    .replace(/\brr\b/gi, "R:R")
    .replace(/\bdpm\b/gi, "DPM")
    .replace(/\bsl\b/gi, "SL")
    .replace(/\btp(\d*)\b/gi, (_m, n) => `TP${n}`)
    .replace(/\bpnl\b/gi, "P&L")
    .replace(/\bpct\b/gi, "%");
  return fixed.charAt(0).toUpperCase() + fixed.slice(1);
}

function Scalar({ value }: { value: unknown }) {
  if (typeof value === "boolean") {
    return (
      <span className={value ? "text-profit" : "text-ink-3"}>
        {value ? "yes" : "no"}
      </span>
    );
  }
  if (value == null || value === "") return <span className="text-ink-3">—</span>;
  if (typeof value === "number") {
    return <span className="num">{Number.isInteger(value) ? value : value.toFixed(2)}</span>;
  }
  return <span className="whitespace-pre-wrap">{String(value)}</span>;
}

function Rows({ rows }: { rows: Record<string, unknown>[] }) {
  // Every key any row carries, so a row missing one is a gap rather than a
  // shifted column.
  const columns = [...new Set(rows.flatMap((r) => Object.keys(r ?? {})))];
  return (
    <table className="w-full text-left text-[11px]">
      <thead className="text-ink-3">
        <tr>
          {columns.map((c) => (
            <th key={c} className="px-2 py-1 font-normal">{humanise(c)}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, i) => (
          <tr key={i} className="border-t border-line align-top">
            {columns.map((c) => (
              <td key={c} className="px-2 py-1 text-ink-1"><Scalar value={row?.[c]} /></td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Value({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    if (value.length === 0) return <p className="text-[11px] text-ink-3">none</p>;
    const objects = value.filter(
      (v): v is Record<string, unknown> => Boolean(v) && typeof v === "object" && !Array.isArray(v),
    );
    if (objects.length === value.length) return <Rows rows={objects} />;
    return (
      <ul className="list-disc pl-4 text-xs text-ink-1">
        {value.map((v, i) => <li key={i}><Scalar value={v} /></li>)}
      </ul>
    );
  }

  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return (
      <div className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
        {entries.map(([k, v]) => (
          <div key={k}>
            <p className="text-[10px] uppercase tracking-wider text-ink-3">{humanise(k)}</p>
            <p className="text-xs leading-relaxed text-ink-1"><Scalar value={v} /></p>
          </div>
        ))}
      </div>
    );
  }

  return <p className="text-xs leading-relaxed text-ink-1"><Scalar value={value} /></p>;
}

export function StructuredAnswer({ answer }: { answer: Record<string, unknown> }) {
  const entries = Object.entries(answer);

  return (
    <div data-testid="structured-answer" className="space-y-3">
      {entries.map(([key, value]) => {
        // A 0-100 score is the one field worth making big: it is the headline
        // of a channel report.
        if (SCORE_KEYS.has(key) && typeof value === "number") {
          return (
            <div key={key} data-testid={`answer-${key}`}
              className="flex items-baseline gap-2 rounded-lg border border-accent/30 bg-accent/5 px-4 py-3">
              <span className={cn("num text-2xl font-bold",
                value >= 70 ? "text-profit" : value >= 40 ? "text-warning" : "text-loss")}>
                {value}
              </span>
              <span className="text-[11px] text-ink-3">
                / 100 {humanise(key).toLowerCase()}
              </span>
              {typeof answer["reliability_label"] === "string" && (
                <span className="text-xs text-ink-2">{answer["reliability_label"]}</span>
              )}
            </div>
          );
        }
        // Rendered beside the score above rather than as a section of its own.
        if (key === "reliability_label") return null;

        return (
          <section key={key} data-testid={`answer-${key}`}
            className="rounded-lg border border-line bg-surface-1 p-3">
            <h4 className="mb-1 text-xs font-semibold text-ink-1">{humanise(key)}</h4>
            <Value value={value} />
          </section>
        );
      })}
    </div>
  );
}
