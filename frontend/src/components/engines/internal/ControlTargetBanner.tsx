import { Server, Laptop } from "lucide-react";

/**
 * Which machine these controls will actually reach.
 *
 * Not decoration. When the remote node is the active trader this machine's
 * engines are stood down, so a Start pressed here starts something that
 * generates nothing — the sync server's own handler calls that *"does nothing
 * useful while looking like it worked"*. The backend routes the command to the
 * right node; this is the panel saying so, because an operator who does not
 * know which machine they are driving cannot tell a working button from a
 * broken one.
 *
 * Three states, not two. "centralized" is the one that would otherwise be
 * misread: the remote node is trading, so the header says REMOTE, and these
 * engines are still the live ones because signal generation moved here.
 */
export function ControlTargetBanner({ target }: { target: string }) {
  if (target === "local") return null;

  const remote = target === "remote";
  return (
    <p
      data-testid="control-target"
      className={`flex items-start gap-2 rounded border px-3 py-2 text-[11px] ${
        remote
          ? "border-remote/40 bg-remote/10 text-remote"
          : "border-line bg-surface-2 text-ink-2"
      }`}
    >
      {remote ? <Server size={13} className="mt-0.5" /> : <Laptop size={13} className="mt-0.5" />}
      {remote ? (
        <span>
          The remote node is trading, so these controls act on <strong>it</strong>,
          not on this machine. Its settings are what is shown below. Tunables
          other than the AI switch have no route between nodes and would only
          change this machine's copy.
        </span>
      ) : (
        <span>
          The remote node is trading, but signal generation has moved here, so
          these engines are the live ones and these controls act on{" "}
          <strong>this machine</strong>.
        </span>
      )}
    </p>
  );
}
