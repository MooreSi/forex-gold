import { useState } from "react";
import { GLOSSARY } from "../content/glossary";
import { Tooltip } from "@/components/shared/Tooltip";

/** Every term the app uses, in plain English, with a filter. */
export function GlossarySection() {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();

  const sections = GLOSSARY.map((s) => ({
    ...s,
    terms: needle
      ? s.terms.filter(
          (t) =>
            t.term.toLowerCase().includes(needle) ||
            t.definition.toLowerCase().includes(needle),
        )
      : s.terms,
  })).filter((s) => s.terms.length > 0);

  return (
    <div className="space-y-5">
      <Tooltip label="Narrow the glossary to terms whose name or explanation contains what you type.">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter terms"
          aria-label="Filter terms"
          className="w-full max-w-sm rounded border border-line bg-surface-1 px-3 py-1.5 text-sm text-ink-1 placeholder:text-ink-3"
        />
      </Tooltip>
      {sections.length === 0 && (
        <p className="text-xs text-ink-3">No term matches “{query}”.</p>
      )}
      {sections.map((section) => (
        <section key={section.title}>
          <h3 className="mb-2 text-sm font-semibold text-accent">{section.title}</h3>
          <dl className="space-y-2.5">
            {section.terms.map((t) => (
              <div key={t.term}>
                <dt className="text-sm font-semibold text-remote">{t.term}</dt>
                <dd className="text-xs leading-relaxed text-ink-2">{t.definition}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
    </div>
  );
}
