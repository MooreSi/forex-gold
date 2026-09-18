import { useState } from "react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";

interface UnrecognisedSectionProps {
  pending: Record<string, unknown>[];
  onResolve: (
    rowId: number, status: string, channel?: string, rule?: Record<string, unknown>,
  ) => Promise<void>;
}

const MEANINGS = [
  { value: "entry", label: "A new signal" },
  { value: "close_all", label: "Close everything" },
  { value: "risk_free", label: "Move to breakeven" },
  { value: "noise", label: "Not a signal" },
];

/**
 * Messages the parser could not read, and the one place an operator teaches it.
 *
 * "Not a signal" dismisses without saving a rule — teaching the parser from a
 * message somebody explicitly said was not a signal is how a channel's chatter
 * starts opening trades.
 */
export function UnrecognisedSection({ pending, onResolve }: UnrecognisedSectionProps) {
  const [busy, setBusy] = useState<number | null>(null);

  if (pending.length === 0) {
    return (
      <EmptyState
        title="Nothing waiting"
        hint="Messages the parser cannot read appear here so you can tell it what they were."
      />
    );
  }

  const answer = async (row: Record<string, unknown>, meaning: string) => {
    const id = Number(row["id"]);
    setBusy(id);
    try {
      if (meaning === "noise") {
        await onResolve(id, "dismissed");
      } else {
        await onResolve(id, "resolved", String(row["channel_name"] ?? row["channel"] ?? ""), {
          pattern: String(row["raw_text"] ?? ""),
          means: meaning,
        });
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <ul className="space-y-2">
      {pending.map((row, i) => (
        <li
          key={String(row["id"] ?? i)}
          data-testid={`unrecognised-${String(row["id"] ?? i)}`}
          className="rounded border border-line bg-surface-2 px-3 py-2"
        >
          <p className="text-[11px] text-ink-3">
            {String(row["channel_name"] ?? row["channel"] ?? "—")}
          </p>
          <p className="whitespace-pre-wrap text-xs text-ink-1">
            {String(row["raw_text"] ?? "")}
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {MEANINGS.map((m) => (
              <Button
                key={m.value}
                variant={m.value === "noise" ? "ghost" : "primary"}
                disabled={busy === Number(row["id"])}
                onClick={() => void answer(row, m.value)}
              >
                {m.label}
              </Button>
            ))}
          </div>
        </li>
      ))}
    </ul>
  );
}
