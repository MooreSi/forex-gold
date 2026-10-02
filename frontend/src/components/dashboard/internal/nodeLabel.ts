/** Which node a trading-node report came from, as the card's badge says it. */
export type ReportNode = "remote" | "local";

export function nodeBadge(base: string, node: ReportNode | undefined): string {
  if (!node) return base;
  return `${base} · ${node === "remote" ? "trading node" : "this node"}`;
}

/**
 * An unreachable trading node is not an empty one, and its figures are never
 * replaced by this node's own, so the reason is shown rather than a blank card.
 */
export function nodeReadError(what: string, error: Error | null | undefined): string {
  return error?.message ? `Could not read ${what}: ${error.message}` : `Could not read ${what}.`;
}
