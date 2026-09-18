import { useEffect, useState } from "react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";

interface LexiconSectionProps {
  lexicons: Record<string, string[]>;
  labels: Record<string, string>;
  help: Record<string, string>;
  onSave: (category: string, phrases: string[]) => Promise<void>;
}

/**
 * The global trigger phrases the parser matches on, editable.
 *
 * One box per category, saved per category rather than all at once: a single
 * Save for everything means an edit to one list silently rewrites the others
 * from whatever the last poll happened to load.
 */
export function LexiconSection({ lexicons, labels, help, onSave }: LexiconSectionProps) {
  const categories = Object.keys(lexicons);
  if (categories.length === 0) {
    return <EmptyState title="No trigger phrases configured" />;
  }
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {categories.map((category) => (
        <LexiconBox
          key={category}
          category={category}
          label={labels[category] ?? category}
          help={help[category] ?? ""}
          phrases={lexicons[category] ?? []}
          onSave={onSave}
        />
      ))}
    </div>
  );
}

function LexiconBox({
  category, label, help, phrases, onSave,
}: {
  category: string; label: string; help: string; phrases: string[];
  onSave: (category: string, phrases: string[]) => Promise<void>;
}) {
  const [text, setText] = useState(phrases.join("\n"));
  const [saving, setSaving] = useState(false);

  useEffect(() => setText(phrases.join("\n")), [phrases]);

  const save = async () => {
    setSaving(true);
    try {
      await onSave(
        category,
        text.split("\n").map((p) => p.trim()).filter(Boolean),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded border border-line bg-surface-2 p-3">
      <h4 className="text-xs font-semibold text-ink-1">{label}</h4>
      {help && <p className="mt-0.5 text-[11px] leading-snug text-ink-3">{help}</p>}
      <textarea
        aria-label={label}
        value={text}
        rows={4}
        onChange={(e) => setText(e.target.value)}
        className="num mt-2 w-full rounded border border-line bg-surface-1 px-2 py-1 text-xs text-ink-1"
      />
      <Button className="mt-1.5" onClick={() => void save()} disabled={saving}>
        {saving ? "Saving…" : "Save"}
      </Button>
    </div>
  );
}
