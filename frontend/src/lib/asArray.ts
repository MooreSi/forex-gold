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
