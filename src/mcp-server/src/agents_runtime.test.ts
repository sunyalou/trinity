import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { buildCreateAgentConfig } from "./tools/agents.js";

describe("MCP agent runtime fields", () => {
  it("forwards runtime fields for create_agent", () => {
    const config = buildCreateAgentConfig({
      name: "worker",
      template: "github:sunyalou/agent",
      runtime: "opencode",
      runtime_model: "deepseek-openai/deepseek-v4-flash",
      runtime_provider_id: "deepseek-openai",
      runtime_model_id: "deepseek-v4-flash",
      runtime_permission: "standard",
    });

    assert.equal(config.runtime, "opencode");
    assert.equal(config.runtime_model, "deepseek-openai/deepseek-v4-flash");
    assert.equal(config.runtime_provider_id, "deepseek-openai");
    assert.equal(config.runtime_model_id, "deepseek-v4-flash");
    assert.equal(config.runtime_permission, "standard");
  });

  it("omits runtime fields when create_agent args omit them", () => {
    const config = buildCreateAgentConfig({ name: "default-worker" });

    assert.equal(Object.prototype.hasOwnProperty.call(config, "runtime"), false);
    assert.equal(Object.prototype.hasOwnProperty.call(config, "runtime_provider_id"), false);
  });
});
