import assert from 'node:assert/strict'
import fs from 'node:fs'

const chatInput = fs.readFileSync('src/frontend/src/components/chat/ChatInput.vue', 'utf8')
const chatPanel = fs.readFileSync('src/frontend/src/components/ChatPanel.vue', 'utf8')
const sessionPanel = fs.readFileSync('src/frontend/src/components/SessionPanel.vue', 'utf8')

assert.match(chatInput, /clear:\s*\(\)\s*=>\s*clearDraft\(\)/, 'ChatInput should expose a clear() method')
assert.match(chatInput, /function\s+clearDraft\s*\(\)/, 'ChatInput should centralize draft clearing')
assert.match(chatInput, /const\s+isComposing\s*=\s*ref\(false\)/, 'ChatInput should track IME composition state')
assert.match(chatInput, /@compositionstart="handleCompositionStart"/, 'ChatInput should listen for IME composition start')
assert.match(chatInput, /@compositionend="handleCompositionEnd"/, 'ChatInput should listen for IME composition end')
assert.match(chatInput, /event\.isComposing\s*\|\|\s*isComposing\.value/, 'ChatInput Enter handling should ignore IME confirmation Enter')
assert.match(chatInput, /suppressNextEnterAfterComposition/, 'ChatInput should suppress the Enter immediately following compositionend')
assert.match(chatInput, /setTimeout\(\(\)\s*=>\s*\{\s*suppressNextEnterAfterComposition\.value\s*=\s*false/s, 'ChatInput should release post-composition Enter suppression on a timer')
assert.match(chatPanel, /chatInputRef\.value\?\.clear\?\.\(\)/, 'ChatPanel should explicitly clear ChatInput after submit is accepted')
assert.match(sessionPanel, /chatInputRef\.value\?\.clear\?\.\(\)/, 'SessionPanel should explicitly clear ChatInput after submit is accepted')

console.log('chat input clear tests passed')
