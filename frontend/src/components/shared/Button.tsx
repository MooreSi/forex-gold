import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Tooltip } from "./Tooltip";
import { cn } from "@/lib/cn";

type Variant = "primary" | "ghost" | "danger" | "success";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  children: ReactNode;
  /**
   * Why the button is disabled, in the user's words.
   *
   * Not optional decoration: a greyed-out Execute button with no explanation
   * is indistinguishable from a broken one. When this is set the button is
   * disabled AND the reason is exposed as its title and to assistive tech, so
   * the caller cannot disable a control and forget to say why.
   */
  disabledReason?: string | null;
  /** Hover help for an ENABLED button. `disabledReason` covers the other case
   *  and wins, because why a control is unavailable outranks what it does. */
  tooltip?: ReactNode;
}

const VARIANTS: Record<Variant, string> = {
  primary: "bg-surface-3 text-ink-1 hover:bg-line border-line",
  ghost: "bg-transparent text-ink-2 hover:bg-surface-2 border-transparent",
  danger: "bg-loss/15 text-loss hover:bg-loss/25 border-loss/40",
  success: "bg-profit/15 text-profit hover:bg-profit/25 border-profit/40",
};

export function Button({
  variant = "primary",
  className,
  children,
  disabledReason,
  tooltip,
  disabled,
  ...rest
}: ButtonProps) {
  const isDisabled = disabled || Boolean(disabledReason);
  const button = (
    <button
      {...rest}
      disabled={isDisabled}
      // **The browser's own tooltip, not the `Tooltip` component.** A
      // disabled button fires no pointer events at all, so a hover wrapper
      // never hears about it -- and the reason a control is unavailable is the
      // one piece of hover text that must not go missing. Buttons keep the
      // native one for that reason; `Tooltip` is for the fields and the
      // indicators, which have no such hole and mostly had no hover text at
      // all.
      title={disabledReason ?? rest.title}
      aria-describedby={rest["aria-describedby"]}
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-3 py-1.5 text-xs font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-40",
        VARIANTS[variant],
        className,
      )}
    >
      {children}
    </button>
  );
  return tooltip && !isDisabled ? <Tooltip label={tooltip}>{button}</Tooltip> : button;
}
