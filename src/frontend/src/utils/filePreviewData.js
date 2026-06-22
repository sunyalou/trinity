export function createFilePreviewData(blob, url = URL.createObjectURL(blob)) {
  return {
    url,
    blob,
    type: blob.type,
    size: blob.size
  }
}

export async function readTextFromPreviewData(previewData) {
  if (previewData?.blob) {
    return previewData.blob.text()
  }

  const response = await fetch(previewData.url)
  return response.text()
}
