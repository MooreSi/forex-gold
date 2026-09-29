import { asArray } from "@/lib/asArray";

/**
 * What the VPS did with this Mac's Telegram channel set-up: the slot
 * channels, parser config and Logic Keywords sent on every connect and change
 * (docs/todo/010). Nothing before the first answer.
 *
 * Red when the VPS reported a problem. A slot it could not start listening on
 * is a channel whose signals will not trade from the VPS, while this machine
 * goes on showing them for display, so the problem would otherwise be
 * invisible here.
 */
export function RemoteChannelSetupLine({ result }: { result: Record<string, unknown> }) {
  if (!("errors" in result)) return null;
  const errors = asArray<string>(result.errors);
  return (
    <p data-testid="remote-channel-setup" className={errors.length ? "text-loss" : "text-ink-3"}>
      {errors.length
        ? `Telegram channel set-up on the VPS: ${errors.join("; ")}`
        : "Telegram channel set-up: in step with this machine"}
    </p>
  );
}
