import { useEffect, useState } from "react";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import type { JournalDraft, JournalEntry } from "../hooks/useCalendarController";

const MOODS = [1, 2, 3, 4, 5];

/** "a, b ,, c" -> ["a", "b", "c"]. Order kept, blanks and repeats dropped. */
export function parseTags(text: string): string[] {
  return [...new Set(text.split(",").map((t) => t.trim()).filter(Boolean))];
}

/**
 * The day's journal: a note, tags and a 1-5 mood (docs/todo/023, #8). Kept
 * per account (demo and live do not share notes), in the engine database.
 * Saved on the button, not as you type: one decision, one write.
 */
export function DayJournalForm({ day, entry, onSave }: {
  day: string; entry: JournalEntry | null;
  onSave: (day: string, draft: JournalDraft) => Promise<void>;
}) {
  const [note, setNote] = useState(entry?.note ?? "");
  const [tags, setTags] = useState((entry?.tags ?? []).join(", "));
  const [mood, setMood] = useState<number | null>(entry?.mood ?? null);
  const [state, setState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  useEffect(() => {
    setNote(entry?.note ?? "");
    setTags((entry?.tags ?? []).join(", "));
    setMood(entry?.mood ?? null);
    setState("idle");
  }, [day, entry]);

  const save = async () => {
    setState("saving");
    try {
      await onSave(day, { note, tags: parseTags(tags), mood });
      setState("saved");
    } catch {
      setState("failed");
    }
  };

  return (
    <section className="space-y-1.5" data-testid="day-journal">
      <p className="text-[10px] uppercase tracking-wider text-ink-3">Journal</p>
      <Tooltip label="Your note on this trading day. Kept for this account only: demo and live notes are separate.">
        <textarea aria-label="Journal note" value={note} maxLength={4000} rows={3}
          onChange={(e) => setNote(e.target.value)}
          placeholder="What happened, what you would do differently"
          className="w-full rounded border border-line bg-surface-1 p-2 text-xs text-ink-1" />
      </Tooltip>
      <Tooltip label="Up to 10 short labels, separated by commas. Repeats are dropped.">
        <input aria-label="Tags" value={tags} onChange={(e) => setTags(e.target.value)}
          placeholder="Tags, comma separated (e.g. overtraded, news day)"
          className="w-full rounded border border-line bg-surface-1 px-2 py-1 text-xs text-ink-1" />
      </Tooltip>
      <div className="flex items-center gap-1">
        <span className="mr-1 text-[11px] text-ink-3">Mood</span>
        {MOODS.map((m) => (
          <button key={m} type="button" aria-pressed={mood === m}
            aria-label={`Mood ${m} of 5`} onClick={() => setMood(mood === m ? null : m)}
            className={cn("num h-6 w-6 rounded border text-[11px]",
              mood === m ? "border-accent bg-accent/15 text-ink-1" : "border-line text-ink-3")}>
            {m}
          </button>
        ))}
        <span className="ml-auto text-[11px] text-ink-3" role="status">
          {state === "saved" ? "Saved" : state === "failed" ? "Could not save" : ""}
        </span>
        <Button onClick={() => void save()} disabled={state === "saving"}>
          {state === "saving" ? "Saving" : "Save"}
        </Button>
      </div>
    </section>
  );
}
