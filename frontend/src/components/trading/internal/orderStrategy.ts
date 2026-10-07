/**
 * The strategy a manual Market or Limit order runs under (owner, 2026-10-07).
 *
 * Blank is a single take profit: the whole position closes at TP, with no
 * partials, no breakeven and no trailing. The backend's name for that
 * management is `orb_fixed` (`manual_market_order.SINGLE_TP_STRATEGY`), the
 * same one-target full close the ORB/IVB report trades under, so it is the
 * blank choice here and not listed a second time.
 */
export const SINGLE_TP = "orb_fixed";

export interface StrategyChoice {
  key: string;
  label: string;
}

interface CatalogueEntry {
  key: string;
  label: string;
  kind: string;
}

/**
 * What both dialogs offer, from `GET /api/trading/strategies`: built-in
 * strategies and EA templates. A grid template is refused by the limit order
 * with its reason (it stages its own legs). Custom strategies are left out:
 * neither manual order path resolves a custom strategy id.
 */
export function strategyChoices(catalogue: CatalogueEntry[]): StrategyChoice[] {
  return catalogue
    .filter((e) => e.key !== SINGLE_TP)
    .filter((e) => e.kind === "builtin" || e.kind === "template")
    .map((e) => ({ key: e.key, label: e.label }));
}

/** The words the confirm step uses for how the position will be managed. */
export function managementSentence(strategy: string, choices: StrategyChoice[]): string {
  if (!strategy) {
    return "No strategy: the whole position closes at the take profit, with no partials, breakeven or trailing.";
  }
  const label = choices.find((c) => c.key === strategy)?.label ?? strategy;
  return `Managed by ${label}.`;
}
