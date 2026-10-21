import { Server } from "lucide-react";

/**
 * Says a panel's figures came from the VPS (owner, 2026-10-21).
 *
 * The backend answers a report from the node doing the work and tags it
 * `node: "remote"` when that is the VPS. Without a line saying so, the VPS's
 * numbers and this machine's look identical. Draws nothing otherwise.
 */
export function NodeSourceTag({ node, what = "these figures" }: { node?: unknown; what?: string }) {
  if (node !== "remote") return null;
  return (
    <p data-testid="node-source"
      className="flex items-center gap-1.5 rounded border border-remote/40 bg-remote/10 px-2 py-1 text-[11px] text-remote">
      <Server size={12} aria-hidden />
      From the VPS: {what} are what its own engine is doing, not this machine.
    </p>
  );
}
