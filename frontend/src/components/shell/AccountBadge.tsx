import { cn } from "@/lib/cn";

interface AccountBadgeProps {
  account: Record<string, unknown> | null;
  /**
   * What the app's own config says it is pointed at, used ONLY as a fallback
   * when the bridge has not answered — and only in the safe direction. See
   * below.
   */
  configured?: string | null;
}

/**
 * Demo or live, and never a guess.
 *
 * Frontend conventions §8: "Make demo vs live unmistakable." The third state
 * matters as much as the other two — a bridge that cannot answer renders
 * UNKNOWN in warning amber, because a missing answer shown as "DEMO" is how
 * somebody places a live order believing otherwise.
 *
 * `is_demo` from the bridge is the ground truth and always wins. The app's
 * configured environment is a fallback for the unanswered case, and it is used
 * in ONE direction only: config saying "live" turns an unknown into a red LIVE
 * badge, because over-warning is free. Config saying "demo" leaves it UNKNOWN,
 * because that is the direction the rule above exists to stop.
 *
 * This is the header's environment switch as well as its badge — `parent`
 * EnvironmentControl wraps it in the button — so it is the one green box, not
 * a green box beside a separate "DEMO" label.
 */
export function AccountBadge({ account, configured }: AccountBadgeProps) {
  const raw = account?.["is_demo"];
  const unconfirmedLive = raw !== true && raw !== false && configured === "live";
  const kind = raw === true
    ? "demo"
    : raw === false || unconfirmedLive
      ? "live"
      : "unknown";
  const label = kind === "demo" ? "DEMO" : kind === "live" ? "LIVE" : "ACCOUNT UNKNOWN";
  const login = account?.["login"];

  return (
    <span
      data-testid="account-badge"
      data-account-kind={kind}
      className={cn(
        "num inline-block whitespace-nowrap rounded border px-2 py-0.5 text-[11px] font-bold tracking-wide",
        kind === "live" && "border-loss/50 bg-loss/15 text-loss",
        kind === "demo" && "border-profit/50 bg-profit/15 text-profit",
        kind === "unknown" && "border-warning/50 bg-warning/15 text-warning",
      )}
      title={
        unconfirmedLive
          ? "This app is configured for the LIVE account. The bridge has not confirmed which account is connected."
          : kind === "unknown"
            ? "The bridge has not reported which account is connected."
            : `Account ${String(login ?? "?")}`
      }
    >
      {label}
      {login != null && raw != null ? ` ${String(login)}` : ""}
    </span>
  );
}
