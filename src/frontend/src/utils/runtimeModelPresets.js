export const CUSTOM_PROVIDER_VALUE = '__custom__'
export const OPENAI_COMPATIBLE_PROTOCOL = 'openai-compatible'

export const RUNTIME_MODEL_ROWS = [
  {
    runtime: 'claude-code',
    label: 'Claude Code',
    description: 'Default for Claude Code schedules and chats without an explicit model.',
  },
  {
    runtime: 'gemini-cli',
    label: 'Gemini CLI',
    description: 'Default for Gemini CLI agents when no model is selected.',
  },
  {
    runtime: 'opencode',
    label: 'OpenCode',
    description: 'Default provider/model pair used by OpenCode agents.',
  },
]

export const PROVIDER_OPTIONS = [
  { label: 'Anthropic', value: 'anthropic' },
  { label: 'OpenAI', value: 'openai' },
  { label: 'Google', value: 'google' },
  { label: 'Custom', value: CUSTOM_PROVIDER_VALUE },
]

export const MODEL_PRESETS = {
  anthropic: [
    'claude-sonnet-4-6',
    'claude-sonnet-4-5',
    'claude-opus-4-8',
    'claude-opus-4-7',
    'claude-opus-4-6',
  ],
  openai: [
    'gpt-5',
    'gpt-5-mini',
    'gpt-4.1',
  ],
  google: [
    'gemini-3-flash',
    'gemini-2.5-flash',
    'gemini-2.5-pro',
  ],
}

export const DEFAULT_RUNTIME_MODELS = {
  'claude-code': { provider: 'anthropic', model: 'claude-sonnet-4-6', providerMode: 'anthropic' },
  'gemini-cli': { provider: 'google', model: 'gemini-3-flash', providerMode: 'google' },
  opencode: { provider: 'anthropic', model: 'claude-sonnet-4-5', providerMode: 'anthropic' },
}

export const CONNECTION_STATUS_LABELS = {
  not_tested: 'Not tested',
  testing: 'Testing...',
  connected: 'Connected',
  authentication_failed: 'Authentication failed',
  model_not_found: 'Model not found or unavailable',
  provider_unreachable: 'Provider unreachable',
  timed_out: 'Timed out',
  unsupported_provider: 'Unsupported provider',
  unknown_error: 'Unknown error',
}

export function providerModelOptions(provider) {
  return MODEL_PRESETS[provider] || []
}

export function runtimeSupportsProtocol(runtime, protocol) {
  const normalizedRuntime = runtime === 'gemini' ? 'gemini-cli' : runtime
  if (normalizedRuntime === 'claude-code') return protocol === 'anthropic-messages'
  if (normalizedRuntime === 'opencode') return protocol === OPENAI_COMPATIBLE_PROTOCOL
  if (normalizedRuntime === 'gemini-cli') return protocol === 'google-gemini' || protocol === 'google-vertex'
  return false
}

export function buildRuntimeProviderModelOptions(runtime, providerConfigs = {}) {
  const options = []

  for (const [providerId, provider] of Object.entries(providerConfigs || {})) {
    if (!provider || !runtimeSupportsProtocol(runtime, provider.protocol)) continue
    const auth = provider.auth || {}
    const isVertexAdcProvider = provider.protocol === 'google-vertex' && auth.type === 'adc'
    if (auth.api_key_configured === false && !isVertexAdcProvider) continue

    const models = Array.isArray(provider.models) ? provider.models : []
    for (const model of models) {
      const modelId = String(model?.id || '').trim()
      if (!modelId) continue

      options.push({
        providerId,
        modelId,
        value: `${providerId}/${modelId}`,
        label: `${provider.name || providerId}: ${model.label || modelId}`,
        protocol: provider.protocol,
        claudeAlias: model.claude_alias || null,
      })
    }
  }

  return options
}

function titleCaseProvider(provider) {
  return String(provider || '')
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ') || String(provider || '')
}

export function buildModelSelectorPresets({ runtimeDefaults = {}, customProviders = [] } = {}) {
  const presets = []
  const seen = new Set()

  const addPreset = (value, label, note) => {
    const normalized = String(value || '').trim()
    if (!normalized || seen.has(normalized)) return
    seen.add(normalized)
    presets.push({ value: normalized, label: label || normalized, note })
  }

  for (const [provider, models] of Object.entries(MODEL_PRESETS)) {
    const labelPrefix = titleCaseProvider(provider)
    for (const model of models) {
      addPreset(`${provider}/${model}`, `${labelPrefix}: ${model}`, 'Built-in provider')
    }
  }

  for (const entry of Object.values(runtimeDefaults || {})) {
    const provider = String(entry?.provider || '').trim()
    const model = String(entry?.model || '').trim()
    if (provider && model) {
      addPreset(`${provider}/${model}`, `${titleCaseProvider(provider)}: ${model}`, 'Runtime default')
    }
  }

  for (const entry of customProviders || []) {
    const provider = String(entry?.provider || '').trim()
    if (!provider || !entry?.api_key_configured) continue
    const models = Array.isArray(entry.models) ? entry.models : []
    if (models.length === 0) {
      addPreset(`${provider}/`, `${provider}/`, 'Saved custom provider')
      continue
    }
    for (const model of models) {
      const modelName = String(model || '').trim()
      if (!modelName) continue
      addPreset(`${provider}/${modelName}`, `${provider}: ${modelName}`, 'Saved custom provider')
    }
  }

  return presets
}

export function resolveRuntimeModel(entry) {
  const provider = String(entry?.provider || '').trim()
  const model = String(entry?.model || '').trim()
  if (!provider && !model) return 'Not configured'
  if (!provider) return model
  if (!model) return provider
  return `${provider}/${model}`
}

export function cloneRuntimeDefaults(value) {
  const source = value && typeof value === 'object' ? value : {}
  const clone = {}

  for (const row of RUNTIME_MODEL_ROWS) {
    const fallback = DEFAULT_RUNTIME_MODELS[row.runtime]
    const entry = source[row.runtime] && typeof source[row.runtime] === 'object'
      ? source[row.runtime]
      : fallback
    const provider = String(entry.provider || fallback.provider || '').trim()
    const model = String(entry.model || fallback.model || '').trim()
    const providerMode = MODEL_PRESETS[provider] ? provider : CUSTOM_PROVIDER_VALUE

    clone[row.runtime] = {
      provider,
      model,
      providerMode,
    }
  }

  return clone
}

export function blankCustomProviderConfig() {
  return {
    protocol: OPENAI_COMPATIBLE_PROTOCOL,
    base_url: '',
    api_key: '',
    api_key_configured: false,
    api_key_masked: null,
  }
}

export function cloneCustomProviderConfigs(value) {
  const source = value && typeof value === 'object' ? value : {}
  const clone = {}

  for (const [provider, config] of Object.entries(source)) {
    const providerName = String(provider || '').trim()
    if (!providerName || !config || typeof config !== 'object') continue

    clone[providerName] = {
      protocol: String(config.protocol || OPENAI_COMPATIBLE_PROTOCOL).trim() || OPENAI_COMPATIBLE_PROTOCOL,
      base_url: String(config.base_url || '').trim(),
      api_key: '',
      api_key_configured: Boolean(config.api_key_configured),
      api_key_masked: config.api_key_masked ?? null,
    }
  }

  return clone
}

export function payloadRuntimeDefaults(value) {
  const source = value && typeof value === 'object' ? value : {}
  const payload = {}

  for (const row of RUNTIME_MODEL_ROWS) {
    payload[row.runtime] = {
      provider: String(source[row.runtime]?.provider || '').trim(),
      model: String(source[row.runtime]?.model || '').trim(),
    }
  }

  return payload
}

export function payloadCustomProviderConfigs(runtimeDefaults, customProviderConfigs) {
  const defaultsSource = runtimeDefaults && typeof runtimeDefaults === 'object' ? runtimeDefaults : {}
  const configsSource = customProviderConfigs && typeof customProviderConfigs === 'object' ? customProviderConfigs : {}
  const payload = {}

  for (const row of RUNTIME_MODEL_ROWS) {
    const entry = defaultsSource[row.runtime]
    if (entry?.providerMode !== CUSTOM_PROVIDER_VALUE) continue

    const provider = String(entry.provider || '').trim()
    if (!provider) continue

    const config = configsSource[provider] && typeof configsSource[provider] === 'object'
      ? configsSource[provider]
      : blankCustomProviderConfig()

    payload[provider] = {
      protocol: String(config.protocol || OPENAI_COMPATIBLE_PROTOCOL).trim() || OPENAI_COMPATIBLE_PROTOCOL,
      base_url: String(config.base_url || '').trim(),
      api_key: String(config.api_key || ''),
    }
  }

  return payload
}
