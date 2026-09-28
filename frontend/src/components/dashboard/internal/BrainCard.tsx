import { useEffect, useState } from "react";
import { Maximize2, X } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { BrainView } from "@/components/brain/BrainView";
import { useBrain } from "@/components/brain/hooks/useBrain";
import { DashCard } from "./DashCard";

/**
 * The brain on the Dashboard, with a full-screen "war room" mode (Esc closes).
 * Read-only: nothing on it can place, close or change anything.
 */
export function BrainCard() {
  const poll = useBrain();
  const [full, setFull] = useState(false);

  useEffect(() => {
    if (!full) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setFull(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [full]);

  const view = (large: boolean) => (
    <BrainView gates={poll.data?.gates} events={poll.data?.events}
      error={poll.error?.message ?? null} large={large} />
  );

  if (full) {
    return (
      <div role="dialog" aria-label="Brain, full screen"
        className="fixed inset-0 z-50 flex flex-col bg-surface-0 p-4">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold tracking-wide text-accent">FOREX GOLD · BRAIN</h2>
          <Button variant="ghost" onClick={() => setFull(false)}>
            <X className="mr-1 inline h-3 w-3" aria-hidden />Close
          </Button>
        </div>
        <div className="min-h-0 flex-1">{view(true)}</div>
      </div>
    );
  }

  return (
    <DashCard title="Brain" icon="ai" badge="live"
      actions={(
        <Button variant="ghost" onClick={() => setFull(true)}>
          <Maximize2 className="mr-1 inline h-3 w-3" aria-hidden />Full screen
        </Button>
      )}
      footnote="Every entry decision the app records, and the gate that made it. Read-only.">
      {view(false)}
    </DashCard>
  );
}
