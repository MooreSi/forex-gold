import { useEffect, useState } from "react";

/**
 * Is the licence admin console served by this install?
 *
 * The console is not part of this repository -- it is mounted at runtime from
 * a `forex-admin` checkout, and only on the licence-issuer machine
 * (`backend/src/api/admin_console.py`). On every other install those routes
 * do not exist at all.
 *
 * So the probe IS the feature detection: a 200 from `/api/admin/status` means
 * the console is mounted and the button should appear; anything else means it
 * is not and the button must not. Deliberately not a setting -- a flag could
 * say "yes" on a machine where the mount failed, and the operator would get a
 * button leading to a 404.
 *
 * Probed once on mount. The mount happens at app startup and cannot change
 * without a restart, so polling would ask a question that cannot change its
 * answer.
 */
export function useAdminConsole(): { available: boolean; canSign: boolean } {
  const [state, setState] = useState({ available: false, canSign: false });

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch("/api/admin/status");
        if (!response.ok) return;
        const body = (await response.json()) as {
          available?: boolean;
          can_sign?: boolean;
        };
        if (!cancelled) {
          setState({
            available: body.available === true,
            canSign: body.can_sign === true,
          });
        }
      } catch {
        // No console here. That is the normal case on a customer machine and
        // is not worth a console error.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return state;
}
