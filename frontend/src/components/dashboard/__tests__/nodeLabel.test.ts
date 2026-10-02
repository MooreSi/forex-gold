import { describe, expect, it } from "vitest";
import { nodeBadge, nodeReadError } from "../internal/nodeLabel";

describe("nodeBadge", () => {
  it("says the figures are the trading node's when they came from the peer", () => {
    expect(nodeBadge("14 days", "remote")).toBe("14 days · trading node");
  });

  it("says this node when it is the one that trades", () => {
    expect(nodeBadge("14 days", "local")).toBe("14 days · this node");
  });

  it("claims nothing before the first answer", () => {
    expect(nodeBadge("14 days", undefined)).toBe("14 days");
  });
});

describe("nodeReadError", () => {
  it("passes the reason on, so an unreachable trader is not read as no data", () => {
    expect(nodeReadError("fill costs", new Error("The trading node could not be reached (x).")))
      .toBe("Could not read fill costs: The trading node could not be reached (x).");
  });

  it("still says something when there is no reason", () => {
    expect(nodeReadError("GEX", null)).toBe("Could not read GEX.");
  });
});
