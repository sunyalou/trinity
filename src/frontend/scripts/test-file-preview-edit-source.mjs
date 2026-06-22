import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const files = [
  '../src/components/FilesPanel.vue',
  '../src/views/FileManager.vue',
]

for (const file of files) {
  const source = readFileSync(new URL(file, import.meta.url), 'utf8')
  assert.match(source, /readTextFromPreviewData/, `${file} should use shared preview-data text reader`)
  assert.doesNotMatch(
    source,
    /fetch\(previewData\.value\.url\)/,
    `${file} should not fetch preview blob URLs`,
  )
  assert.doesNotMatch(
    source,
    /fetch\(data\.url\)/,
    `${file} should not fetch newly-created blob URLs`,
  )
}

console.log('file preview edit source tests passed')
