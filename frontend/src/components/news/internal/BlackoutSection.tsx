import { useEffect, useState } from "react";
import { Button } from "@/components/shared/Button";
import type { BlackoutSettings } from "@/api/types";

interface BlackoutSectionProps {
  settings: BlackoutSettings;
  onSave: (next: {
    enabled: boolean; minutes_before: number; minutes_after: number;
  }) => Promise<void>;
}

/**
 * The window that holds automated entries around a release.
 *
 * The saved values are read back from the calendar rather than assumed: it
 * clamps the minutes, so what the operator typed and what the engine uses are
 * not always the same number.
 */
export function BlackoutSection({ settings, onSave }: BlackoutSectionProps) {
  const [enabled, setEnabled] = useState(settings.enabled);
  const [before, setBefore] = useState(String(settings.minutes_before));
  const [after, setAfter] = useState(String(settings.minutes_after));
  const [saving, setSaving] = useState(false);

  // The server is the source of truth; a poll that lands after a save must be
  // able to correct what is on screen.
  useEffect(() => {
    setEnabled(settings.enabled);
    setBefore(String(settings.minutes_before));
    setAfter(String(settings.minutes_after));
  }, [settings.enabled, settings.minutes_before, settings.minutes_after]);

  const save = async () => {
    setSaving(true);
    try {
      await onSave({
        enabled,
        minutes_before: Number(before) || 0,
        minutes_after: Number(after) || 0,
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-wrap items-end gap-3">
      <label className="flex items-center gap-2 text-xs text-ink-2">
        <input
          type="checkbox"
          checked={enabled}
          aria-label="Hold automated entries around high-impact news"
          onChange={(e) => setEnabled(e.target.checked)}
          className="accent-accent"
        />
        Hold automated entries around high-impact news
      </label>
      <label className="text-xs text-ink-2">
        Minutes before
        <input
          value={before}
          inputMode="numeric"
          aria-label="Minutes before"
          onChange={(e) => setBefore(e.target.value)}
          className="num ml-2 w-16 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
        />
      </label>
      <label className="text-xs text-ink-2">
        Minutes after
        <input
          value={after}
          inputMode="numeric"
          aria-label="Minutes after"
          onChange={(e) => setAfter(e.target.value)}
          className="num ml-2 w-16 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
        />
      </label>
      <Button onClick={() => void save()} disabled={saving}>
        {saving ? "Saving…" : "Save"}
      </Button>
      <p className="w-full text-[11px] text-ink-3">
        Manual orders are never held. This only pauses the engines, and the risk
        governor makes the decision from the same calendar.
      </p>
    </div>
  );
}
