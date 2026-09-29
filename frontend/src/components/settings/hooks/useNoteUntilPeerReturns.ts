import { useCallback, useEffect, useRef, useState } from "react";

/**
 * A note about a VPS restart or upgrade, kept until the VPS has gone and come
 * back (owner, 2026-09-29: "VPS: Restarting app in 5 seconds" stayed under
 * Restart VPS for good). The link dropping and returning is the moment the
 * note stops being true. `Notice`'s own timeout covers a VPS that never drops.
 */
export function useNoteUntilPeerReturns(connected: boolean) {
  const [note, setNoteState] = useState<string | null>(null);
  const sawDrop = useRef(false);

  const setNote = useCallback((text: string | null) => {
    sawDrop.current = false;
    setNoteState(text);
  }, []);

  useEffect(() => {
    if (note === null) return;
    if (!connected) sawDrop.current = true;
    else if (sawDrop.current) setNote(null);
  }, [connected, note, setNote]);

  const clear = useCallback(() => setNote(null), [setNote]);
  return { note, setNote, clear };
}
