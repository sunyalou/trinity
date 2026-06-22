import assert from 'node:assert/strict'

import {
  buildRuntimeProviderModelOptions,
  buildModelSelectorPresets,
} from '../src/utils/runtimeModelPresets.js'

const presets = buildModelSelectorPresets({
  runtimeDefaults: {
    opencode: { provider: 'deepseek', model: 'deepseek-chat' },
    'gemini-cli': { provider: 'google', model: 'gemini-2.5-pro' },
  },
  customProviders: [
    { provider: 'moonshot', api_key_configured: true },
    { provider: 'missing-key', api_key_configured: false },
  ],
})

assert(presets.some((preset) => preset.value === 'deepseek/deepseek-chat'))
assert(presets.some((preset) => preset.value === 'moonshot/'))
assert(presets.some((preset) => preset.value === 'google/gemini-2.5-pro'))
assert(!presets.some((preset) => preset.value === 'missing-key/'))

const providerConfigs = {
  'deepseek-anthropic': {
    id: 'deepseek-anthropic',
    name: 'DeepSeek Anthropic',
    protocol: 'anthropic-messages',
    models: [{ id: 'deepseek-v4-pro', label: 'DeepSeek V4 Pro', claude_alias: 'sonnet' }],
    auth: { api_key_configured: true },
  },
  'deepseek-openai': {
    id: 'deepseek-openai',
    name: 'DeepSeek OpenAI',
    protocol: 'openai-compatible',
    models: [{ id: 'deepseek-v4-pro', label: 'DeepSeek V4 Pro' }],
    auth: { api_key_configured: true },
  },
  google: {
    id: 'google',
    name: 'Google',
    protocol: 'google-gemini',
    models: [{ id: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro' }],
    auth: { api_key_configured: true },
  },
  vertex: {
    id: 'vertex',
    name: 'Google Vertex AI',
    protocol: 'google-vertex',
    models: [{ id: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash' }],
    auth: { type: 'adc', api_key_configured: false },
  },
  'vertex-service-account': {
    id: 'vertex-service-account',
    name: 'Google Vertex AI Service Account',
    protocol: 'google-vertex',
    models: [{ id: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro' }],
    auth: { type: 'service_account', api_key_configured: false },
  },
  'missing-key-openai': {
    id: 'missing-key-openai',
    name: 'Missing Key OpenAI',
    protocol: 'openai-compatible',
    models: [{ id: 'gpt-5', label: 'GPT-5' }],
    auth: { api_key_configured: false },
  },
}

const claudeOptions = buildRuntimeProviderModelOptions('claude-code', providerConfigs)
assert(claudeOptions.some((option) => option.providerId === 'deepseek-anthropic'), 'Claude should show anthropic-messages provider')
assert(!claudeOptions.some((option) => option.providerId === 'deepseek-openai'), 'Claude should hide openai-compatible provider')
assert.deepEqual(
  claudeOptions.find((option) => option.providerId === 'deepseek-anthropic'),
  {
    providerId: 'deepseek-anthropic',
    modelId: 'deepseek-v4-pro',
    value: 'deepseek-anthropic/deepseek-v4-pro',
    label: 'DeepSeek Anthropic: DeepSeek V4 Pro',
    protocol: 'anthropic-messages',
    claudeAlias: 'sonnet',
  },
  'Runtime provider/model option should include the expected shape'
)

const opencodeOptions = buildRuntimeProviderModelOptions('opencode', providerConfigs)
assert(opencodeOptions.some((option) => option.value === 'deepseek-openai/deepseek-v4-pro'), 'OpenCode should show OpenAI-compatible provider/model')
assert(!opencodeOptions.some((option) => option.providerId === 'deepseek-anthropic'), 'OpenCode v1 should hide anthropic-messages provider')
assert(!opencodeOptions.some((option) => option.providerId === 'missing-key-openai'), 'OpenCode should hide providers without configured API keys')

const geminiOptions = buildRuntimeProviderModelOptions('gemini', providerConfigs)
assert(geminiOptions.some((option) => option.value === 'google/gemini-2.5-pro'), 'Gemini should show Google Gemini model')
assert(geminiOptions.some((option) => option.value === 'vertex/gemini-2.5-flash'), 'Gemini should show Google Vertex ADC model')
assert(!geminiOptions.some((option) => option.providerId === 'vertex-service-account'), 'Gemini should hide Google Vertex service account provider until materialization exists')
assert(!geminiOptions.some((option) => option.providerId === 'deepseek-openai'), 'Gemini should hide OpenAI-compatible provider')

const geminiCliOptions = buildRuntimeProviderModelOptions('gemini-cli', providerConfigs)
assert(geminiCliOptions.some((option) => option.value === 'google/gemini-2.5-pro'), 'Gemini CLI should show Google Gemini model')
assert(geminiCliOptions.some((option) => option.value === 'vertex/gemini-2.5-flash'), 'Gemini CLI should show Google Vertex ADC model')
assert(!geminiCliOptions.some((option) => option.providerId === 'vertex-service-account'), 'Gemini CLI should hide Google Vertex service account provider until materialization exists')

console.log('runtime model preset tests passed')
