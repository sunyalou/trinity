/**
 * Tests for #1101 — operator-queue agent-scope post-filter.
 *
 * Pins `filterQueueItemsForAgentScope` so a future edit can't silently widen
 * what an agent-scoped key sees in a broad `list_operator_queue` call (the
 * load-bearing gate: the backend filters to the KEY OWNER's accessible agents,
 * so the MCP layer is the only place agent_permissions are enforced).
 *
 * Runner: built-in `node:test`. No new devDependency. Run via:
 *   node --import tsx --test src/*.test.ts
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { filterQueueItemsForAgentScope } from "./tools/operator_queue.js";

type Item = { id: string; agent_name: string };

const items: Item[] = [
  { id: "a", agent_name: "self" },
  { id: "b", agent_name: "friend" },
  { id: "c", agent_name: "stranger" },
  { id: "d", agent_name: "self" },
];

describe("#1101 filterQueueItemsForAgentScope", () => {
  it("keeps only items whose agent is in the allowed set (self + permitted)", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["self", "friend"]));
    assert.deepEqual(out.map((i) => i.id), ["a", "b", "d"]);
  });

  it("a self-only allowed set keeps just the caller's own items", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["self"]));
    assert.deepEqual(out.map((i) => i.id), ["a", "d"]);
  });

  it("drops every item whose agent is not permitted", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["nobody"]));
    assert.deepEqual(out, []);
  });

  it("an empty allowed set drops everything", () => {
    assert.deepEqual(filterQueueItemsForAgentScope(items, new Set<string>()), []);
  });

  it("an empty item list returns empty", () => {
    assert.deepEqual(filterQueueItemsForAgentScope([], new Set(["self"])), []);
  });

  it("is a pure filter — does not mutate the input array", () => {
    const snapshot = items.map((i) => ({ ...i }));
    filterQueueItemsForAgentScope(items, new Set(["self"]));
    assert.deepEqual(items, snapshot);
  });

  it("is order-preserving", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["self", "stranger"]));
    assert.deepEqual(out.map((i) => i.id), ["a", "c", "d"]);
  });
});
