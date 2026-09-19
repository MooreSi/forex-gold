import "@testing-library/jest-dom/vitest";

/**
 * jsdom implements no ResizeObserver, and anything that draws a chart uses one.
 *
 * This lives here rather than in each chart test because per-file
 * `stubGlobal`/`unstubAllGlobals` is what produced a one-run-in-three flake:
 * React passive effects can land after a test's own teardown has cleared the
 * global, and the failure then points at whichever test happened to be last
 * rather than at the missing stub.
 *
 * A test that needs to prove behaviour with the observer ABSENT deletes it
 * deliberately — see `useChartGeometry.test.tsx`.
 */
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
