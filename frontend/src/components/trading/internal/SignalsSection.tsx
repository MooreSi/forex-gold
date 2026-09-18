import { useState } from "react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatPrice } from "@/components/shared/format";
import { SignalEditorDialog } from "./SignalEditorDialog";

/**
 * Signals the engines and the Telegram reader have produced.
 *
 * Read-only here on purpose: acting on a signal opens a position, and that
 * control belongs with the rest of the money path rather than on a list row
 * where it is one mis-click from a trade.
 */
interface SignalsSectionProps {
  signals: Record<string, unknown>[];
  onChanged: () => void;
}

export function SignalsSection({ signals, onChanged }: SignalsSectionProps) {
  const [editing, setEditing] = useState<Record<string, unknown> | null>(null);

  if (signals.length === 0) {
    return (
      <EmptyState
        title="No signals yet"
        hint="Engine and Telegram signals appear here as they are produced."
      />
    );
  }
  return (
    <>
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
          <th className="py-1 font-medium">Source</th>
          <th className="py-1 font-medium">Side</th>
          <th className="py-1 font-medium">Entry</th>
          <th className="py-1 font-medium">Status</th>
          <th className="py-1" />
        </tr>
      </thead>
      <tbody>
        {signals.map((s, i) => (
          <tr key={String(s["id"] ?? i)} className="border-t border-line">
            <td className="py-1.5 text-ink-2">{String(s["source"] ?? s["channel"] ?? "—")}</td>
            <td className={s["direction"] === "SELL" ? "py-1.5 text-loss" : "py-1.5 text-profit"}>
              {String(s["direction"] ?? "—")}
            </td>
            <td className="num py-1.5 text-ink-1">
              {formatPrice(typeof s["entry"] === "number" ? (s["entry"] as number) : null)}
            </td>
            <td className="py-1.5 text-ink-3">{String(s["status"] ?? "—")}</td>
            <td className="py-1.5 text-right">
              <Button variant="ghost" onClick={() => setEditing(s)}>Edit</Button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
      {editing && (
        // Keyed on the signal id so the dialog REMOUNTS per row. Without it a
        // second Edit would reuse the first row's draft state — React's version
        // of the loop-capture bug the NiceGUI editor had.
        <SignalEditorDialog
          key={String(editing["signal_id"] ?? editing["id"] ?? "")}
          signal={editing}
          onClose={() => setEditing(null)}
          onSaved={onChanged}
        />
      )}
    </>
  );
}
