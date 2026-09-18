/**
 * Read a poll result as a list, whatever actually arrived.
 *
 * The API is typed on the Python side, but what reaches `fetch` is untyped
 * JSON and the failure mode is not theoretical: a response that is an object
 * where a list was expected makes `.map` throw inside render, and React tears
 * down the whole tree — one endpoint returning the wrong shape blanks the
 * entire dashboard rather than one panel. Found by
 * `AppShell.test.tsx > tabs the port has not reached`, where a stubbed fetch
 * returned `{}` for everything.
 *
 * This is not a licence to be sloppy about types. It is the boundary between
 * a typed client and an untyped wire, and every boundary needs one.
 */
export function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

/**
 * The same boundary, for a nested object.
 *
 * `asArray` closed half the hole: a response that is an object where a list was
 * expected no longer throws. A response MISSING an object field still did,
 * because `Object.keys(undefined)` and `settings[key]` both raise inside
 * render — and React tears down the whole tree, so one absent field blanks the
 * entire dashboard rather than one panel. Found by the shell test, whose
 * stubbed fetch answers `{}` for every endpoint: which is exactly what a
 * half-deployed backend looks like from the browser.
 */
export function asObject<T extends object = Record<string, unknown>>(value: unknown): T {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as T)
    : ({} as T);
}
