import assert from 'node:assert/strict'

import {
  createFilePreviewData,
  readTextFromPreviewData,
} from '../src/utils/filePreviewData.js'

const blob = new Blob(['hello from blob'], { type: 'text/plain' })
const preview = createFilePreviewData(blob, 'blob:preview-url')

assert.equal(preview.url, 'blob:preview-url')
assert.equal(preview.type, 'text/plain')
assert.equal(preview.size, blob.size)
assert.equal(preview.blob, blob)

let fetchCalled = false
globalThis.fetch = async () => {
  fetchCalled = true
  throw new Error('blob URL fetch should not be required for text preview')
}

assert.equal(await readTextFromPreviewData(preview), 'hello from blob')
assert.equal(fetchCalled, false, 'text preview should read directly from previewData.blob')

const fallbackPreview = { url: 'blob:fallback-url' }
globalThis.fetch = async (url) => {
  assert.equal(url, 'blob:fallback-url')
  return { text: async () => 'fallback text' }
}
assert.equal(await readTextFromPreviewData(fallbackPreview), 'fallback text')

console.log('file preview data tests passed')
