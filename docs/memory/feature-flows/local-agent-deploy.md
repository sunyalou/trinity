# Feature: Local Agent Deployment via MCP

> **Updated**: 2026-04-03 - Remove credential injection from deploy flow. Archive is self-contained (.env included).

## Overview

Deploy Trinity-compatible local Claude Code agents to a remote Trinity platform with a single MCP command. The **local agent** (Claude Code on your machine) packages the directory into a tar.gz archive and sends it to the remote Trinity backend for deployment.

**Key Architecture Point**: The MCP server runs remotely and cannot access your local filesystem. Therefore, the **calling agent** must package the archive locally before invoking the MCP tool.

## User Story

As a developer working with a Trinity-compatible local agent, I want to deploy it to a remote Trinity instance with one command so I can run it on the platform without manual file transfer.

## Entry Points

- **MCP Tool**: `deploy_local_agent` via Trinity MCP server
- **API**: `POST /api/agents/deploy-local`

---

## Architecture

```
+-------------------------------------+                     +-----------------------------+
|  Your Local Machine                 |                     |  Remote Trinity Server      |
|                                     |                     |                             |
|  Claude Code (local agent)          |     HTTP POST       |  MCP Server                 |
|  1. tar -czf archive.tar.gz ...     |  ---------------->  |  deploy_local_agent tool    |
|  2. base64 archive.tar.gz           |   archive           |         |                   |
|  3. Call deploy_local_agent         |                     |         v                   |
|                                     |                     |  Backend API                |
|                                     |                     |  /api/agents/deploy-local   |
|  /home/you/my-agent/                |                     |         |                   |
|  |-- template.yaml                  |                     |         v                   |
|  |-- CLAUDE.md                      |                     |  Extract, validate, deploy  |
|  +-- .env                           |                     |  Agent container created    |
+-------------------------------------+                     +-----------------------------+
```

---

## MCP Tool Layer

### Tool: `deploy_local_agent`

**Location**: `src/mcp-server/src/tools/agents.ts:426-525`

**Parameters**:
```typescript
{
  archive: string,                    // Base64-encoded tar.gz archive (REQUIRED)
  name?: string                       // Override agent name (optional)
}
```

The archive should include all files needed by the agent — `.env`, `.mcp.json`, `CLAUDE.md`, etc. No separate credential injection step.

**Validation** (lines 464-476):
- Checks archive is provided and non-empty
- Validates base64 format with regex: `/^[A-Za-z0-9+/=]+$/`

**Description**: The tool receives a pre-packaged archive from the calling agent and forwards it to the backend. It does NOT access the local filesystem - that's the calling agent's responsibility.

---

## Calling Agent Workflow

The local Claude Code agent must perform these steps before calling `deploy_local_agent`:

### Step 1: Create tar.gz Archive

```bash
# Package the agent directory, including .env and all credential files
tar -czf /tmp/agent-deploy.tar.gz \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  -C /path/to/parent agent-directory-name
```

### Step 2: Base64 Encode

```bash
# macOS
base64 -i /tmp/agent-deploy.tar.gz > /tmp/agent-deploy.b64

# Linux
base64 /tmp/agent-deploy.tar.gz > /tmp/agent-deploy.b64
```

### Step 3: Call MCP Tool

The agent then calls `deploy_local_agent` with:
- `archive`: Contents of the base64 file
- `name`: Optional name override

---

## Backend Layer

### Architecture (Service Layer)

The local agent deployment uses a **thin router + service layer** architecture:

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py` | Endpoint definition |
| Service | `src/backend/services/agent_service/deploy.py` | Deployment business logic |

### Endpoint: POST /api/agents/deploy-local

**Router**: `src/backend/routers/agents.py:212-225`

```python
@router.post("/deploy-local")
async def deploy_local_agent(
    body: DeployLocalRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Deploy a Trinity-compatible local agent."""
    return await deploy_local_agent_logic(
        body=body,
        current_user=current_user,
        request=request,
        create_agent_fn=create_agent_internal
    )
```

**Request Model** (`src/backend/models.py`):
```python
class DeployLocalRequest(BaseModel):
    """Request to deploy a local agent."""
    archive: str  # Base64-encoded tar.gz
    name: Optional[str] = None  # Override name from template.yaml
```

**Response Model** (`src/backend/models.py`):
```python
class DeployLocalResponse(BaseModel):
    """Response from local agent deployment."""
    status: str  # "success" or "error"
    agent: Optional[AgentStatus] = None
    versioning: Optional[VersioningInfo] = None
    error: Optional[str] = None
    code: Optional[str] = None  # Error code for machine-readable errors
```

### Deployment Flow (`deploy.py`)

1. **Decode & Validate Archive**
   - Decode base64 archive
   - Check size limit (50MB max)

2. **Extract Archive**
   - Extract to temp directory using `_safe_extract_tar()`
   - Security: Full path validation via `_validate_tar_member()`

3. **Find Root Directory**
   - Handle nested extraction (single directory case)

4. **Trinity-Compatible Validation**
   - `is_trinity_compatible()` in `services/template_service.py`
   - Requires template.yaml with `name` and `resources` fields

5. **Determine Agent Name**
   - Use body.name override or template.yaml name
   - Sanitize with `sanitize_agent_name()`

6. **Version Handling**
   - `get_next_version_name()` finds next available version
   - Pattern: `my-agent` -> `my-agent-2` -> `my-agent-3`
   - Stops previous version if running

7. **Template Copy**
   - Try `/agent-configs/templates` first (with write test)
   - Fall back to `./config/agent-templates/{version_name}/`

8. **Agent Creation**
    - Extract runtime config from template
    - Call `create_agent_fn()` (injected `create_agent_internal`) with local template
    - Agent container starts with all files from the archive (including `.env`)

9. **Return Response**
    - Return DeployLocalResponse with agent status and versioning info

### Safe Tar Extraction (`deploy.py:39-181`)

The extraction uses comprehensive security validation:

**Path Validation** (`_is_path_within()`, lines 43-60):
- Uses `Path.resolve()` to normalize paths
- Checks target stays within base directory

**Member Validation** (`_validate_tar_member()`, lines 63-135):
- Rejects absolute paths
- Rejects path traversal (`..` in paths)
- Validates destination stays within base_dir
- Rejects device files (chr, blk) and FIFOs
- Validates symlink targets stay within base_dir
- Validates hardlink targets stay within base_dir

**Safe Extraction** (`_safe_extract_tar()`, lines 138-180):
- Checks file count (1000 max)
- Validates all members before extraction
- Only extracts validated members

---

## Template Validation

**Location**: `src/backend/services/template_service.py:309-358`

```python
def is_trinity_compatible(path: Path) -> Tuple[bool, Optional[str], Optional[dict]]:
    """
    Check if a directory contains a Trinity-compatible agent.

    A Trinity-compatible agent must have:
    1. template.yaml file
    2. name field in template.yaml
    3. resources field in template.yaml
    """
```

**Validation Checks**:
1. `template.yaml` exists
2. File is valid YAML
3. File is not empty
4. `name` field present
5. `resources` field present and is a dictionary
6. Warning (non-blocking) if `CLAUDE.md` missing

---

## Error Codes

| Code | HTTP | Description |
|------|------|-------------|
| `NOT_TRINITY_COMPATIBLE` | 400 | Missing or invalid template.yaml |
| `ARCHIVE_TOO_LARGE` | 400 | Exceeds 50MB limit |
| `INVALID_ARCHIVE` | 400 | Not valid tar.gz, bad base64, or path traversal |
| `TOO_MANY_FILES` | 400 | Exceeds 1000 file limit |
| `MISSING_NAME` | 400 | No name specified and template.yaml has no name |

---

## Size Limits

| Limit | Value | Constant Location |
|-------|-------|-------------------|
| Archive size | 50 MB | `deploy.py` MAX_ARCHIVE_SIZE |
| File count | 1000 | `deploy.py` MAX_FILES |

---

## Security Considerations

1. **Path Traversal Prevention**: Archive paths validated via `_validate_tar_member()`:
   - No `..` in paths
   - No absolute paths
   - Symlinks/hardlinks validated to stay within extraction dir
   - Device files and FIFOs rejected

2. **Temp Cleanup**: Temp directory always cleaned up in finally block (lines 430-436)

3. **Self-Contained Archives**: Credentials (`.env`) are included in the archive — no separate injection step

4. **Auth Required**: Uses standard JWT authentication via `get_current_user`

5. **Write Permission Check**: Templates directory write-tested before use

---

## Testing

### Prerequisites
- Trinity backend running (local or remote)
- MCP server running and accessible
- Valid MCP API key configured in Claude Code
- Local agent directory with valid template.yaml

### Test Steps

#### 1. Create Test Agent Directory
```bash
mkdir -p /tmp/test-deploy-agent
cat > /tmp/test-deploy-agent/template.yaml << 'EOF'
name: test-deploy
display_name: Test Deploy Agent
description: Testing local agent deployment
resources:
  cpu: "2"
  memory: "4g"
EOF

echo "# Test Deploy Agent" > /tmp/test-deploy-agent/CLAUDE.md
echo "TEST_API_KEY=test-value-123" > /tmp/test-deploy-agent/.env
```

#### 2. Package and Deploy via Claude Code

In Claude Code with Trinity MCP configured, ask:

```
Package and deploy my local agent at /tmp/test-deploy-agent to Trinity.
```

**Expected**: Claude Code will:
1. Run `tar` command to create archive (including .env)
2. Run `base64` to encode it
3. Call the MCP tool with the archive

**Verify**:
- Agent "test-deploy" created in Trinity
- Agent has .env file from archive

#### 3. Deploy Again (Versioning Test)
```
Deploy my local agent at /tmp/test-deploy-agent to Trinity again
```

**Expected**:
- New agent "test-deploy-2" created
- Previous "test-deploy" stopped

#### 4. Test Invalid Archive
```
Call deploy_local_agent with archive="not-valid-base64!"
```

**Expected**: Error "Invalid archive format"

#### 5. Test Missing Template
```bash
rm /tmp/test-deploy-agent/template.yaml
# Then try to deploy
```

**Expected**: Error "NOT_TRINITY_COMPATIBLE"

### Edge Cases
- [ ] Archive larger than 50MB -> ARCHIVE_TOO_LARGE
- [ ] More than 1000 files -> TOO_MANY_FILES
- [ ] Path traversal in archive -> INVALID_ARCHIVE

### Cleanup
```bash
rm -rf /tmp/test-deploy-agent
rm -f /tmp/agent-deploy.tar.gz /tmp/agent-deploy.b64
```

---

## Example: Full Deployment Script

For reference, here's a complete bash script a local agent might execute:

```bash
#!/bin/bash
# deploy-to-trinity.sh - Package and prepare for MCP deployment

AGENT_DIR="$1"
if [ -z "$AGENT_DIR" ]; then
  echo "Usage: deploy-to-trinity.sh /path/to/agent"
  exit 1
fi

# Validate template.yaml exists
if [ ! -f "$AGENT_DIR/template.yaml" ]; then
  echo "Error: Not Trinity-compatible - missing template.yaml"
  exit 1
fi

# Create archive (includes .env and all credential files)
ARCHIVE="/tmp/trinity-deploy-$$.tar.gz"
tar -czf "$ARCHIVE" \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  -C "$(dirname "$AGENT_DIR")" "$(basename "$AGENT_DIR")"

# Base64 encode
ARCHIVE_B64=$(base64 -i "$ARCHIVE" 2>/dev/null || base64 "$ARCHIVE")

echo "Archive size: $(wc -c < "$ARCHIVE") bytes"
echo "Ready for deploy_local_agent MCP call"

# Cleanup
rm -f "$ARCHIVE"
```

---

## Related Documentation

- [TRINITY_COMPATIBLE_AGENT_GUIDE.md](../../TRINITY_COMPATIBLE_AGENT_GUIDE.md) - Required template.yaml structure
- [credential-injection.md](credential-injection.md) - Credential management
- [agent-lifecycle.md](agent-lifecycle.md) - Agent creation flow

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-04-03 | **#251**: Removed `credentials` parameter entirely. Archive is self-contained — `.env` and credential files included in tar.gz. Removed credential injection block that caused hangs. |
| 2026-02-05 | **CRED-002**: Removed `credential_manager` parameter from deploy flow. |
| 2026-01-23 | Verified all line numbers. Updated deploy.py references (now 437 lines). Added safe tar extraction details. Updated router line numbers (212-225). Added template validation location. |
| 2025-12-30 | Verified line numbers |
| 2025-12-27 | Service layer refactoring: Deploy logic moved to `services/agent_service/deploy.py` |
| 2025-12-24 | Changed from local path to archive-based deployment |
| 2025-12-21 | Initial implementation |

**Status**: Working
