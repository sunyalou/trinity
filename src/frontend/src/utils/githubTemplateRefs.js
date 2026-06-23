const REPO_PATTERN = /^[a-zA-Z0-9._-]+\/[a-zA-Z0-9._-]+$/
const BRANCH_PATTERN = /^[a-zA-Z0-9._/-]{1,128}$/

function validateBranch(branch) {
  if (!branch || !BRANCH_PATTERN.test(branch)) throw new Error('Invalid branch')
  if (
    branch.includes('..') ||
    branch.includes('//') ||
    branch.startsWith('/') ||
    branch.endsWith('/') ||
    branch.includes('@{') ||
    branch.includes('\\')
  ) {
    throw new Error('Invalid branch')
  }
  if (branch.split('/').some(part => part.endsWith('.lock'))) {
    throw new Error('Invalid branch')
  }
  return branch
}

function validatePath(path) {
  if (!path || path.startsWith('/') || path.includes('\\') || path.includes('@')) {
    throw new Error('Invalid template path')
  }
  const parts = path.split('/')
  if (parts.some(part => !part || part === '.' || part === '..' || /\s/.test(part))) {
    throw new Error('Invalid template path')
  }
  return parts.join('/')
}

export function parseGithubTemplateRef(input) {
  let value = String(input || '').trim()
  const urlMatch = value.match(/github\.com\/([^/\s#?.]+\/[^/\s#?.]+)(.*)?$/)
  if (urlMatch) {
    const repo = urlMatch[1].replace(/\.git$/, '')
    let suffix = urlMatch[2] || ''
    if (suffix === '.git') suffix = ''
    else if (suffix.startsWith('.git//') || suffix.startsWith('.git@')) suffix = suffix.slice('.git'.length)
    else if (suffix.startsWith('/tree/')) {
      const [branch, ...pathParts] = suffix.slice('/tree/'.length).split('/')
      if (!branch || pathParts.length === 0) throw new Error('Invalid GitHub tree URL')
      suffix = `//${pathParts.join('/')}@${branch}`
    }
    value = `${repo}${suffix}`
  }
  if (value.startsWith('github:')) value = value.slice('github:'.length)

  let branch = null
  if (value.includes('@')) {
    const idx = value.lastIndexOf('@')
    branch = validateBranch(value.slice(idx + 1))
    value = value.slice(0, idx)
  }

  let repo = value
  let templatePath = null
  if (value.includes('//')) {
    const parts = value.split('//')
    if (parts.length !== 2) throw new Error('Invalid template ref')
    repo = parts[0]
    templatePath = validatePath(parts[1])
  }

  if (!REPO_PATTERN.test(repo)) throw new Error('Invalid repository')

  let canonical = repo
  if (templatePath) canonical += `//${templatePath}`
  if (branch) canonical += `@${branch}`
  return { canonical, repo, templatePath, branch, templateId: `github:${canonical}` }
}
