import { cn } from "@/lib/cn";

interface AccountBadgeProps {
  account: Record<string, unknown> | null;
}

/**
 * Demo or live, and never a guess.
 *
 * Frontend conventions §8: "Make demo vs live unmistakable." The third state
 * matters as much as the other two — a bridge that cannot answer renders
 * UNKNOWN in warning amber, because a missing answer shown as "DEMO" is how
 * somebody places a live order believing otherwise.
 */
export function AccountBadge({ account }: AccountBadgeProps) {
  const raw = account?.["is_demo"];
  const kind = raw === true ? "demo" : raw === false ? "live" : "unknown";
  const label = kind === "demo" ? "DEMO" : kind === "live" ? "LIVE" : "ACCOUNT UNKNOWN";
  const login = account?.["login"];

  return (
    <span
      data-testid="account-badge"
      data-account-kind={kind}
      className={cn(
        "num rounded border px-2 py-0.5 text-[11px] font-bold tracking-wide",
        kind === "live" && "border-loss/50 bg-loss/15 text-loss",
        kind === "demo" && "border-profit/50 bg-profit/15 text-profit",
        kind === "unknown" && "border-warning/50 bg-warning/15 text-warning",
      )}
      title={
        kind === "unknown"
          ? "The bridge has not reported which account is connected."
          : `Account ${String(login ?? "?")}`
      }
    >
      {label}
      {login != null && kind !== "unknown" ? ` ${String(login)}` : ""}
    </span>
  );
}
