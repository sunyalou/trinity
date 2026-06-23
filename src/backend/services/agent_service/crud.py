"""
Agent Service CRUD - Agent creation and deletion operations.

Contains the core logic for creating and deleting agents.
"""
import os
import re
import json
import docker
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import HTTPException, Request

from models import AgentConfig, AgentStatus, User, validate_agent_runtime, validate_agent_runtime_permission
from database import db
from services.docker_service import (
    docker_client,
    get_agent_by_name,
    get_next_available_port,
    get_agent_status_from_container,
)
from services.docker_utils import (
    volume_get, volume_create, containers_run
)
from services.template_service import (
    get_github_template,
    generate_credential_files,
)
from services.runtime_provider_templates import build_runtime_template
from services import git_service
from services.settings_service import get_anthropic_api_key, get_github_pat, get_agent_full_capabilities, get_agent_quota_for_role, get_agent_default_resources, get_agent_default_require_email, settings_service
from services.github_service import GitHubService, GitHubError
from services.github_template_ref import GitHubTemplateRef, parse_github_template_ref
from utils.helpers import sanitize_agent_name, utc_now_iso
from .helpers import validate_base_image
from .lifecycle import RESTRICTED_CAPABILITIES, FULL_CAPABILITIES
from .capabilities import AGENT_TMPFS_MOUNT, AGENT_DEFAULT_TMPDIR

logger = logging.getLogger(__name__)

# Allowed chars in a `local:`-prefixed template name. Strict enough to
# block path traversal (`..`, `/`, `\`, leading dots) so the templates
# directory join in `create_agent_internal` can't escape into arbitrary
# filesystem reads (CodeQL py/path-injection on #950 PR).
_LOCAL_TEMPLATE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

# Roots that a resolved local-template path must stay within (#950).
_LOCAL_TEMPLATE_ROOTS = (
    Path("/agent-configs/templates").resolve(),
    Path("/data/deployed-templates").resolve(),
)

_OPENCODE_PROVIDER_ENV_KEYS = (
    'GOOGLE_API_KEY',
    'GEMINI_API_KEY',
    'OPENAI_API_KEY',
)


def _inject_opencode_provider_envs(env_vars: dict) -> None:
    """Expose provider credentials OpenCode can consume directly.

    Claude subscription assignment is intentionally handled separately and only
    for Claude runtimes; this helper never removes ANTHROPIC_API_KEY.
    """
    for key in _OPENCODE_PROVIDER_ENV_KEYS:
        value = os.getenv(key, '')
        if value:
            env_vars[key] = value


def _inject_opencode_custom_provider_config(
    env_vars: dict,
    runtime_model: str | None,
    custom_provider_configs: dict | None,
) -> bool:
    """Inject OpenCode config for saved OpenAI-compatible custom providers.

    OpenCode reads custom provider definitions from OPENCODE_CONFIG_CONTENT.
    The raw API key is exposed only via a generated environment variable and is
    referenced from config with OpenCode's {env:...} indirection.
    """
    if not runtime_model or "/" not in runtime_model:
        return False

    provider, model = runtime_model.split("/", 1)
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        return False

    configs = custom_provider_configs if isinstance(custom_provider_configs, dict) else {}
    provider_config = configs.get(provider)
    if not isinstance(provider_config, dict):
        return False
    if provider_config.get("protocol") != "openai-compatible":
        return False

    sanitized_provider = re.sub(r"[^A-Za-z0-9]", "_", provider).upper().strip("_")
    if not sanitized_provider:
        return False

    base_url = str(provider_config.get("base_url") or "").strip()
    api_key = str(provider_config.get("api_key") or "")
    if not base_url or not api_key:
        return False

    api_key_env_name = f"TRINITY_CUSTOM_PROVIDER_{sanitized_provider}_API_KEY"
    env_vars[api_key_env_name] = api_key
    env_vars["OPENCODE_CONFIG_CONTENT"] = json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            provider: {
                "npm": "@ai-sdk/openai-compatible",
                "name": provider,
                "options": {
                    "baseURL": base_url,
                    "apiKey": f"{{env:{api_key_env_name}}}",
                },
                "models": {
                    model: {"name": model},
                },
            },
        },
        "model": runtime_model,
    })
    return True


def _apply_provider_runtime_template(
    env_vars: dict,
    runtime: str,
    provider_id: str | None,
    model_id: str | None,
) -> bool:
    """Inject runtime provider template env without exposing raw secrets in config."""
    if bool(provider_id) != bool(model_id):
        raise HTTPException(
            status_code=400,
            detail="Both runtime_provider_id and runtime_model_id are required when selecting a runtime provider model",
        )
    if not provider_id or not model_id:
        return False

    providers = settings_service.get_provider_configs()
    provider = providers.get(provider_id) if isinstance(providers, dict) else None
    if not provider:
        raise HTTPException(status_code=400, detail=f"Provider '{provider_id}' not found")

    try:
        template = build_runtime_template(runtime, provider, model_id)
        secrets = {
            f"provider:{provider_id}:api_key": provider.get("auth", {}).get("api_key", "")
        }
        env_vars.update(template.materialize_env(secrets))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    env_vars["AGENT_RUNTIME_MODEL"] = template.model_arg
    env_vars["TRINITY_RUNTIME_PROVIDER_ID"] = provider_id
    env_vars["TRINITY_RUNTIME_MODEL_ID"] = model_id
    return True


def _apply_runtime_template_config(config: AgentConfig, runtime_config) -> None:
    """Apply template runtime overrides with explicit validation.

    AgentConfig assignment validation is not enabled, so direct mutation must
    call the same validators used during model construction.
    """
    if isinstance(runtime_config, dict):
        runtime_type = runtime_config.get("type", config.runtime)
        runtime_permission = runtime_config.get("permission", config.runtime_permission)
        validate_agent_runtime(runtime_type)
        validate_agent_runtime_permission(runtime_permission)
        config.runtime = runtime_type
        config.runtime_model = runtime_config.get("model", config.runtime_model)
        config.runtime_permission = runtime_permission
    elif isinstance(runtime_config, str):
        validate_agent_runtime(runtime_config)
        config.runtime = runtime_config


def _safe_local_template_path(template_name: str, root: Path) -> Path:
    """Join `template_name` onto `root` and prove it didn't traverse out.

    Two-step defense:

    1. Regex allowlist on the name (rejects `..`, `/`, `\\`, leading
       dots etc.) — fail fast with HTTP 400 for obviously hostile input.
    2. Resolve the joined path and assert `is_relative_to(root)` — this
       is the pattern CodeQL recognises as a `py/path-injection`
       barrier, so the static analyser stops marking subsequent
       `.exists()` / `open()` calls on the returned path as tainted.

    Either failure raises `HTTPException(400)` with structured code
    `INVALID_LOCAL_TEMPLATE_NAME`.
    """
    if (
        not template_name
        or ".." in template_name
        or not _LOCAL_TEMPLATE_NAME_RE.match(template_name)
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"Invalid local template name {template_name!r}: must match "
                    f"[a-zA-Z0-9][a-zA-Z0-9_.-]* with no '..' segments."
                ),
                "code": "INVALID_LOCAL_TEMPLATE_NAME",
            },
        )
    candidate = (root / template_name).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"Resolved template path {candidate} escaped expected root {root}."
                ),
                "code": "INVALID_LOCAL_TEMPLATE_NAME",
            },
        )
    return candidate


def _get_default_resource(key: str) -> str:
    """Return system-default cpu or memory, falling back to hardcoded safe value."""
    defaults = get_agent_default_resources()
    return defaults.get(key, "2" if key == "cpu" else "4g")


@dataclass(frozen=True)
class GitHubTemplateConfig:
    ref: GitHubTemplateRef
    github_repo_for_agent: str
    github_template_path: str | None
    enable_git_sync_for_template: bool


def _resolve_github_template_config(config_template: str) -> GitHubTemplateConfig:
    """Resolve a github: template string into clone/env and git-sync config."""
    ref = parse_github_template_ref(config_template)
    return GitHubTemplateConfig(
        ref=ref,
        github_repo_for_agent=ref.repo,
        github_template_path=ref.template_path,
        enable_git_sync_for_template=ref.template_path is None,
    )


async def _reserve_git_sync_for_template(
    *,
    enable_git_sync_for_template: bool,
    agent_name: str,
    github_repo_for_agent: str,
    source_branch: str | None,
    source_mode: bool,
) -> tuple[str | None, str | None]:
    """Reserve a git-sync working branch only for templates that support sync."""
    if not enable_git_sync_for_template:
        return None, None
    return await git_service.reserve_and_generate_instance_id(
        agent_name=agent_name,
        github_repo=github_repo_for_agent,
        source_branch=source_branch or "main",
        source_mode=source_mode,
    )


def _apply_github_template_env(
    *,
    env_vars: dict,
    config: AgentConfig,
    github_repo_for_agent: str,
    github_pat_for_agent: str,
    github_template_path: str | None,
    enable_git_sync_for_template: bool,
    git_working_branch: str | None,
) -> None:
    """Apply GitHub template env vars, gating git-sync vars for subdir templates."""
    env_vars['GITHUB_REPO'] = github_repo_for_agent
    env_vars['GITHUB_PAT'] = github_pat_for_agent
    if github_template_path:
        env_vars['GITHUB_TEMPLATE_PATH'] = github_template_path
    if config.source_branch:
        env_vars['GIT_SOURCE_BRANCH'] = config.source_branch

    if not enable_git_sync_for_template:
        logger.info(
            f"GitHub subdirectory template env vars set for {config.name}: "
            f"repo={github_repo_for_agent}, template_path={github_template_path}, "
            f"branch={config.source_branch or 'default'}, sync=false"
        )
        return

    # Phase 7: Enable git sync for GitHub-native agents
    env_vars['GIT_SYNC_ENABLED'] = 'true'
    # Dev/self-host: propagate optional git base-URL override to agent container
    _git_base = os.getenv('TRINITY_GIT_BASE_URL')
    if _git_base:
        env_vars['TRINITY_GIT_BASE_URL'] = _git_base

    # #389 S1a: 15-min auto-sync heartbeat. Only legacy (working-branch)
    # agents get it — source-mode agents track main read-only, and
    # auto-pushing to main would clobber protected branches. Operators
    # can toggle per-agent via PUT /api/agents/{name}/git/auto-sync.
    if not config.source_mode:
        env_vars['GIT_SYNC_AUTO'] = 'true'

    # Source mode (default): Track source branch directly for pull-only sync
    # Legacy mode: Create a unique working branch for bidirectional sync
    if config.source_mode:
        env_vars['GIT_SOURCE_MODE'] = 'true'
        env_vars['GIT_SOURCE_BRANCH'] = config.source_branch or 'main'
        logger.info(
            f"GitHub template env vars set for {config.name}: "
            f"repo={github_repo_for_agent}, branch={config.source_branch or 'main'}, "
            f"source_mode=true, sync=true"
        )
    else:
        env_vars['GIT_WORKING_BRANCH'] = git_working_branch
        logger.info(
            f"GitHub template env vars set for {config.name}: "
            f"repo={github_repo_for_agent}, working_branch={git_working_branch}, "
            f"source_mode=false, sync=true"
        )


def _set_git_auto_sync_if_enabled(
    *,
    agent_name: str,
    github_repo_for_agent: str | None,
    enable_git_sync_for_template: bool,
    source_mode: bool,
) -> None:
    """Enable default auto-sync only when git sync is active for the template."""
    if github_repo_for_agent and enable_git_sync_for_template and not source_mode:
        db.set_git_auto_sync_enabled(agent_name, True)


def get_platform_version() -> str:
    """Get the current Trinity platform version from VERSION file."""
    version_paths = [
        Path("/app/VERSION"),  # In container
        Path(__file__).parent.parent.parent.parent.parent / "VERSION",  # Development
    ]
    for version_path in version_paths:
        if version_path.exists():
            return version_path.read_text().strip()
    return "unknown"


async def create_agent_internal(
    config: AgentConfig,
    current_user: User,
    request: Request,
    skip_name_sanitization: bool = False,
    ws_manager=None
) -> AgentStatus:
    """
    Internal function to create an agent.

    Used by both the API endpoint and system deployment.

    CRED-002: Credentials are no longer auto-injected during creation.
    They are added after creation via inject_credentials endpoint or
    imported from .credentials.enc on startup.

    Args:
        config: Agent configuration
        current_user: Authenticated user
        request: FastAPI request object
        skip_name_sanitization: If True, don't sanitize the name (used when name is pre-validated)
        ws_manager: Optional WebSocket manager for broadcasts

    Returns:
        AgentStatus of the created agent

    Raises:
        HTTPException: On validation or creation errors
    """
    original_name = config.name
    if not skip_name_sanitization:
        config.name = sanitize_agent_name(config.name)

    if not config.name:
        raise HTTPException(status_code=400, detail="Invalid agent name - must contain at least one alphanumeric character")

    # #834: the name-reservation check must also catch soft-deleted agents.
    # `get_agent_owner` filters them out (user-facing 404 transparency), so
    # we use the unfiltered `is_agent_name_reserved` here. Without this the
    # create flow walks past the existence guard, the container ends up
    # created, and the agent_ownership INSERT hits a UNIQUE constraint
    # IntegrityError leaving the system half-built.
    if (
        get_agent_by_name(config.name)
        or db.get_agent_owner(config.name)
        or db.is_agent_name_reserved(config.name)
    ):
        raise HTTPException(status_code=409, detail="Agent already exists")

    # Agent quota enforcement: per-role limits (QUOTA-001)
    max_agents = get_agent_quota_for_role(current_user.role)
    if max_agents > 0:
        owned = db.get_agents_by_owner(current_user.username)
        # System agents don't count toward user quota
        non_system = [a for a in owned if not (db.get_agent_owner(a) or {}).get("is_system")]
        if len(non_system) >= max_agents:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": f"Agent quota exceeded. You have {len(non_system)}/{max_agents} agents. "
                             f"Delete an agent to create a new one.",
                    "code": "QUOTA_EXCEEDED",
                    "current": len(non_system),
                    "limit": max_agents
                }
            )

    # SEC-172: Validate base image against allowlist before any Docker operations
    validate_base_image(config.base_image)

    template_data = {}
    github_template_path = None
    github_repo_for_agent = None
    github_pat_for_agent = None
    enable_git_sync_for_template = False
    git_instance_id = None
    git_working_branch = None
    # Phase 9.11: Track shared folder config from template
    template_shared_folders = None

    # Load template configuration
    if config.template:
        # #843: reject template strings that don't start with a known
        # scheme. Pre-fix, an unprefixed name (e.g. "dd-compliance"
        # instead of "local:dd-compliance") fell through every
        # branch of the dispatch and silently produced a blank agent
        # — same return code as success, no log warning, the operator
        # only noticed when the agent had no template.yaml. Reject
        # explicitly so the failure is loud.
        if not (
            config.template.startswith("github:")
            or config.template.startswith("local:")
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Template '{config.template}' must start with "
                    f"'local:' (for templates under config/agent-templates/) "
                    f"or 'github:' (for GitHub-hosted templates). "
                    f"Example: 'local:{config.template}' or 'github:owner/repo'."
                ),
            )
        if config.template.startswith("github:"):
            try:
                resolved_template = _resolve_github_template_config(config.template)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            config.source_branch = resolved_template.ref.branch or config.source_branch
            github_template_path = resolved_template.github_template_path
            enable_git_sync_for_template = resolved_template.enable_git_sync_for_template
            template_lookup = resolved_template.ref.template_id
            gh_template = get_github_template(template_lookup)

            if gh_template:
                # Pre-defined GitHub template from config.py
                github_repo = gh_template.get("repo") or gh_template.get("clone_repo") or resolved_template.github_repo_for_agent

                # Get system GitHub PAT from settings (SQLite) or env var
                github_pat = get_github_pat()
                if not github_pat:
                    raise HTTPException(
                        status_code=500,
                        detail="GitHub PAT not configured. Set GITHUB_PAT in .env or add via Settings."
                    )

                github_repo_for_agent = github_repo
                github_template_path = gh_template.get("template_path", github_template_path)
                if gh_template.get("source_branch"):
                    config.source_branch = gh_template.get("source_branch")
                enable_git_sync_for_template = github_template_path is None
                github_pat_for_agent = github_pat
                config.resources = gh_template.get("resources", config.resources)
                config.mcp_servers = gh_template.get("mcp_servers", config.mcp_servers)
            else:
                # Dynamic GitHub template - use any github:owner/repo[//path][@branch] format
                # Requires system GitHub PAT to be configured

                # Get system GitHub PAT from settings (SQLite) or env var
                github_pat = get_github_pat()
                if not github_pat:
                    raise HTTPException(
                        status_code=500,
                        detail="GitHub PAT not configured. Set GITHUB_PAT in .env or add via Settings."
                    )

                github_repo_for_agent = resolved_template.github_repo_for_agent
                github_template_path = resolved_template.github_template_path
                enable_git_sync_for_template = resolved_template.enable_git_sync_for_template
                github_pat_for_agent = github_pat
                logger.info(f"Using dynamic GitHub template: {resolved_template.ref.canonical} (branch: {config.source_branch})")

            # Validate PAT has access to the repository before creating container
            # This prevents silent clone failures in startup.sh (#218)
            try:
                gh_service = GitHubService(github_pat_for_agent)
                repo_parts = github_repo_for_agent.split("/", 1)
                if len(repo_parts) == 2:
                    repo_info = await gh_service.check_repo_exists(repo_parts[0], repo_parts[1])
                    if not repo_info.exists:
                        raise HTTPException(
                            status_code=400,
                            detail=f"GitHub repository '{github_repo_for_agent}' not found or PAT does not have access. "
                                   f"Verify the repository exists and the configured GitHub PAT has read access."
                        )
                    logger.info(f"Validated GitHub repo access: {github_repo_for_agent} (private={repo_info.private})")

                    # If source_branch specified, validate branch exists
                    if config.source_branch and config.source_branch != repo_info.default_branch:
                        try:
                            branch_resp = await gh_service._request(
                                "GET", f"/repos/{github_repo_for_agent}/branches/{config.source_branch}"
                            )
                            if branch_resp.status_code == 404:
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"Branch '{config.source_branch}' not found in repository '{github_repo_for_agent}'. "
                                           f"Available default branch: '{repo_info.default_branch}'."
                                )
                        except HTTPException:
                            raise
                        except Exception as e:
                            logger.warning(f"Could not validate branch '{config.source_branch}': {e}")
            except HTTPException:
                raise
            except GitHubError as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to validate GitHub repository access: {e}"
                )
            except Exception as e:
                # Log but don't block creation for transient network errors
                logger.warning(f"GitHub repo validation failed (non-blocking): {e}")

            # Generate git sync instance ID and branch for Phase 7.
            # S7 Layer 0 (#382): reserve the working branch atomically —
            # probes the remote with `git ls-remote` and inserts the DB
            # row under the partial UNIQUE index so no two agents can end
            # up bound to the same (repo, branch). The row is written
            # here, before the container is created, so it must be rolled
            # back if anything in the rest of the flow fails (see the
            # `try: ... except: db.delete_git_config(...)` block below).
            git_instance_id, git_working_branch = await _reserve_git_sync_for_template(
                enable_git_sync_for_template=enable_git_sync_for_template,
                agent_name=config.name,
                github_repo_for_agent=github_repo_for_agent,
                source_branch=config.source_branch,
                source_mode=config.source_mode,
            )
        elif config.template.startswith("local:"):
            # Local template - strip "local:" prefix. Look in curated catalog
            # first (/agent-configs/templates), then in deploy-local writable
            # store (/data/deployed-templates) per #950. Each candidate path
            # is validated + resolved to prove it stays under the root before
            # any filesystem access (regex barrier + is_relative_to barrier).
            raw_name = config.template[6:]
            template_path = _safe_local_template_path(
                raw_name, _LOCAL_TEMPLATE_ROOTS[0]
            )
            if not (template_path / "template.yaml").exists():
                template_path = _safe_local_template_path(
                    raw_name, _LOCAL_TEMPLATE_ROOTS[1]
                )

            template_yaml = template_path / "template.yaml"

            if template_yaml.exists():
                try:
                    with open(template_yaml) as f:
                        template_data = yaml.safe_load(f)
                        config.type = template_data.get("type", config.type)
                        config.resources = template_data.get("resources", config.resources)
                        config.tools = template_data.get("tools", config.tools)
                        creds = template_data.get("credentials", {})
                        mcp_servers = list(creds.get("mcp_servers", {}).keys())
                        if mcp_servers:
                            config.mcp_servers = mcp_servers
                        # Multi-runtime support - extract runtime config from template
                        runtime_config = template_data.get("runtime", {})
                        try:
                            _apply_runtime_template_config(config, runtime_config)
                        except ValueError as e:
                            raise HTTPException(status_code=400, detail=str(e)) from e
                        # Phase 9.11: Extract shared folder config from template
                        shared_folders_config = template_data.get("shared_folders", {})
                        if shared_folders_config:
                            template_shared_folders = {
                                "expose": shared_folders_config.get("expose", False),
                                "consume": shared_folders_config.get("consume", False)
                            }
                except HTTPException:
                    raise
                except Exception as e:
                    logger.warning(f"Error loading template config: {e}")

    if config.port is None:
        config.port = get_next_available_port()

    runtime = (config.runtime or 'claude-code').lower()

    # CRED-002: Credentials are now injected directly into agents after creation
    # via the inject_credentials endpoint, not auto-injected during creation.
    # The agent starts without credentials and they are added via Quick Inject
    # or imported from .credentials.enc files.

    generated_files = {}
    if template_data:
        # Generate empty credential files structure from template
        generated_files = generate_credential_files(
            template_data, {}, config.name,
            template_base_path=github_template_path
        )

    cred_files_dir = Path(f"/tmp/agent-{config.name}-creds")
    cred_files_dir.mkdir(exist_ok=True)

    # Write template-generated files (.env, .mcp.json, etc.)
    for filepath, content in generated_files.items():
        file_path = cred_files_dir / filepath
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w") as f:
            f.write(content)

    agent_config = {
        "agent": {
            "type": config.type,
            "base_image": config.base_image,
            "resources": config.resources,
            "tools": config.tools,
            "mcp_servers": config.mcp_servers,
            "custom_instructions": config.custom_instructions,
            "credentials": {}  # CRED-002: Credentials injected after creation
        }
    }

    config_path = Path(f"/tmp/agent-{config.name}.yaml")
    with open(config_path, "w") as f:
        yaml.dump(agent_config, f)

    credentials_path = Path(f"/tmp/agent-{config.name}-credentials.json")
    with open(credentials_path, "w") as f:
        json.dump({}, f)  # CRED-002: Empty credentials, injected after creation

    template_volume = None
    cred_files_volume = None
    if config.template:
        if config.template.startswith("github:"):
            pass  # Agent clones at startup
        elif config.template.startswith("local:"):
            # Local template - strip "local:" prefix for path resolution.
            # Curated templates (under /agent-configs/templates) bind their
            # host path to /template; the agent's startup.sh copies it to
            # /home/developer on first boot. Deploy-local templates (under
            # /data/deployed-templates) do NOT bind here — deploy.py has
            # already pre-populated the agent's workspace volume directly
            # via put_archive (#950). The bind-mount transport relied on
            # backend's /data and the agent's host bind resolving to the
            # same host path, which was true in prod compose (host bind)
            # but not in dev compose (named volume).
            raw_name = config.template[6:]
            curated_path = _safe_local_template_path(
                raw_name, _LOCAL_TEMPLATE_ROOTS[0]
            )
            if curated_path.exists():
                host_templates_base = os.getenv("HOST_TEMPLATES_PATH", "./config/agent-templates")
                # raw_name already validated by _safe_local_template_path; the
                # join here is on a value that survived the regex + resolve
                # barriers above, so the bind source can't traverse out.
                host_template_path = Path(host_templates_base) / curated_path.name
                template_volume = {str(host_template_path): {'bind': '/template', 'mode': 'ro'}}

        if generated_files:
            cred_files_volume = {str(cred_files_dir): {'bind': '/generated-creds', 'mode': 'ro'}}

    # Phase: Agent-to-Agent Collaboration
    # Generate agent-scoped MCP API key for Trinity MCP access
    agent_mcp_key = None
    trinity_mcp_url = os.getenv('TRINITY_MCP_URL', 'http://mcp-server:8080/mcp')
    try:
        agent_mcp_key = db.create_agent_mcp_api_key(
            agent_name=config.name,
            owner_username=current_user.username,
            description=f"Auto-generated Trinity MCP key for agent {config.name}"
        )
        if agent_mcp_key:
            logger.info(f"Created MCP API key for agent {config.name}: {agent_mcp_key.key_prefix}...")
    except Exception as e:
        logger.warning(f"Failed to create MCP API key for agent {config.name}: {e}")

    env_vars = {
        'AGENT_NAME': config.name,
        'AGENT_TYPE': config.type,
        'CREDENTIALS_FILE': '/config/credentials.json',
        'ANTHROPIC_API_KEY': get_anthropic_api_key(),
        'ENABLE_SSH': 'true',
        'ENABLE_AGENT_UI': 'true',
        'AGENT_SERVER_PORT': '8000',
        'TEMPLATE_NAME': config.template if config.template else '',
        # Multi-runtime support
        'AGENT_RUNTIME': runtime,
        'AGENT_RUNTIME_MODEL': config.runtime_model or '',
        # #1098: redirect scratch (pip/npm/build, ML wheels) off the 100 MB
        # noexec /tmp tmpfs onto the disk-backed, exec-capable home volume.
        # The dir is created at container start by startup.sh.
        'TMPDIR': AGENT_DEFAULT_TMPDIR,
    }

    if config.runtime_model:
        env_vars['AGENT_RUNTIME_MODEL'] = config.runtime_model

    provider_template_applied = _apply_provider_runtime_template(
        env_vars,
        runtime,
        config.runtime_provider_id,
        config.runtime_model_id,
    )
    if provider_template_applied:
        config.runtime_model = env_vars.get('AGENT_RUNTIME_MODEL') or config.runtime_model

    if runtime == 'opencode':
        env_vars['OPENCODE_PERMISSION_PROFILE'] = config.runtime_permission or 'restricted'
        env_vars['OPENCODE_DISABLE_AUTOUPDATE'] = '1'
        env_vars['OPENCODE_DISABLE_MODELS_FETCH'] = '1'
        if not provider_template_applied:
            _inject_opencode_provider_envs(env_vars)
            try:
                custom_provider_configs = settings_service.get_custom_provider_configs()
                if _inject_opencode_custom_provider_config(env_vars, config.runtime_model, custom_provider_configs):
                    provider = config.runtime_model.split('/', 1)[0]
                    logger.info(f"Injected OpenCode custom provider config for provider '{provider}'")
            except Exception as e:
                logger.warning(f"Failed to inject OpenCode custom provider config: {e}")

    # GUARD-001: per-agent guardrails overrides (empty by default; baseline
    # is always applied inside the container).
    _guardrails = db.get_guardrails_config(config.name)
    if _guardrails:
        import json as _json
        env_vars['AGENT_GUARDRAILS'] = _json.dumps(_guardrails)

    # Auto-assign subscription (round-robin) — #74
    auto_assigned_subscription_id = None
    if runtime in {'claude-code', 'claude'}:
        try:
            least_used = db.get_least_used_subscription()
            if least_used:
                token = db.get_subscription_token(least_used.id)
                if token:
                    env_vars['CLAUDE_CODE_OAUTH_TOKEN'] = token
                    env_vars.pop('ANTHROPIC_API_KEY', None)
                    auto_assigned_subscription_id = least_used.id
                    logger.info(f"Auto-assigned subscription '{least_used.name}' to agent {config.name}")
                else:
                    logger.warning(f"Failed to decrypt subscription '{least_used.name}' token, using platform API key")
        except Exception as e:
            logger.warning(f"Subscription auto-assign failed for {config.name}: {e}")

    # Add Google API key if using Gemini runtime
    # Gemini CLI expects GEMINI_API_KEY environment variable
    if runtime == 'gemini-cli' or runtime == 'gemini':
        google_api_key = os.getenv('GOOGLE_API_KEY', '')
        if google_api_key:
            env_vars['GEMINI_API_KEY'] = google_api_key  # Gemini CLI expects this name
        else:
            logger.warning("Gemini runtime selected but GOOGLE_API_KEY not configured")

    # OpenTelemetry Configuration (enabled by default)
    # Claude Code has built-in OTel support - these vars enable metrics export
    if os.getenv('OTEL_ENABLED', '1') == '1':
        env_vars['CLAUDE_CODE_ENABLE_TELEMETRY'] = '1'
        env_vars['OTEL_METRICS_EXPORTER'] = os.getenv('OTEL_METRICS_EXPORTER', 'otlp')
        env_vars['OTEL_LOGS_EXPORTER'] = os.getenv('OTEL_LOGS_EXPORTER', 'otlp')
        env_vars['OTEL_EXPORTER_OTLP_PROTOCOL'] = os.getenv('OTEL_EXPORTER_OTLP_PROTOCOL', 'grpc')
        env_vars['OTEL_EXPORTER_OTLP_ENDPOINT'] = os.getenv('OTEL_COLLECTOR_ENDPOINT', 'http://trinity-otel-collector:4317')
        env_vars['OTEL_METRIC_EXPORT_INTERVAL'] = os.getenv('OTEL_METRIC_EXPORT_INTERVAL', '60000')

    # Phase: Agent-to-Agent Collaboration - Inject Trinity MCP credentials
    if agent_mcp_key:
        env_vars['TRINITY_MCP_URL'] = trinity_mcp_url
        env_vars['TRINITY_MCP_API_KEY'] = agent_mcp_key.api_key
        # RELIABILITY-004 / #307: backend base URL for the liveness heartbeat
        # loop. The agent authenticates the beat with the MCP key injected
        # above (Option B — no master internal secret in agents); the agent
        # heartbeat is gated on both this URL and the MCP key being present.
        env_vars['TRINITY_BACKEND_URL'] = os.getenv('TRINITY_BACKEND_URL', 'http://backend:8000')

    if github_repo_for_agent and github_pat_for_agent:
        _apply_github_template_env(
            env_vars=env_vars,
            config=config,
            github_repo_for_agent=github_repo_for_agent,
            github_pat_for_agent=github_pat_for_agent,
            github_template_path=github_template_path,
            enable_git_sync_for_template=enable_git_sync_for_template,
            git_working_branch=git_working_branch,
        )

    # CRED-002: Legacy credential injection loop removed.
    # Credentials are now injected after agent creation via:
    # - inject_credentials endpoint (Quick Inject)
    # - .credentials.enc import on agent startup

    if docker_client:
        try:
            # Create per-agent persistent volume for /home/developer (Pillar III: Persistent Memory)
            # This ensures files created by the agent survive container restarts
            agent_volume_name = f"agent-{config.name}-workspace"
            try:
                await volume_get(agent_volume_name)
            except docker.errors.NotFound:
                await volume_create(
                    name=agent_volume_name,
                    labels={
                        'trinity.platform': 'agent-workspace',
                        'trinity.agent-name': config.name
                    }
                )

            volumes = {
                str(config_path): {'bind': '/config/agent-config.yaml', 'mode': 'ro'},
                str(credentials_path): {'bind': '/config/credentials.json', 'mode': 'ro'},
                'encrypted-data': {'bind': '/data', 'mode': 'rw'},
                agent_volume_name: {'bind': '/home/developer', 'mode': 'rw'}  # Persistent workspace
            }

            if template_volume:
                volumes.update(template_volume)
            if cred_files_volume:
                volumes.update(cred_files_volume)

            # Phase 9.11: Agent Shared Folders - mount shared volumes based on config
            # First, write template-defined shared folder config to DB (if defined)
            if template_shared_folders:
                try:
                    db.upsert_shared_folder_config(
                        agent_name=config.name,
                        expose_enabled=template_shared_folders.get("expose", False),
                        consume_enabled=template_shared_folders.get("consume", False)
                    )
                    logger.info(f"Applied template shared folder config for {config.name}: expose={template_shared_folders.get('expose')}, consume={template_shared_folders.get('consume')}")
                except Exception as e:
                    logger.warning(f"Failed to apply template shared folder config for {config.name}: {e}")

            shared_folder_config = db.get_shared_folder_config(config.name)
            if shared_folder_config:
                # If agent exposes a shared folder, create and mount the shared volume
                if shared_folder_config.expose_enabled:
                    shared_volume_name = db.get_shared_volume_name(config.name)
                    volume_created = False
                    try:
                        await volume_get(shared_volume_name)
                    except docker.errors.NotFound:
                        await volume_create(
                            name=shared_volume_name,
                            labels={
                                'trinity.platform': 'agent-shared',
                                'trinity.agent-name': config.name
                            }
                        )
                        volume_created = True

                    # Fix ownership of new volumes (Docker creates them as root)
                    if volume_created:
                        try:
                            await containers_run(
                                'alpine',
                                command='chown 1000:1000 /shared',
                                volumes={shared_volume_name: {'bind': '/shared', 'mode': 'rw'}},
                                remove=True
                            )
                        except Exception as e:
                            logger.warning(f"Could not fix shared volume ownership: {e}")

                    volumes[shared_volume_name] = {'bind': '/home/developer/shared-out', 'mode': 'rw'}

                # If agent consumes shared folders, mount available shared volumes
                if shared_folder_config.consume_enabled:
                    available_folders = db.get_available_shared_folders(config.name)
                    for source_agent in available_folders:
                        source_volume = db.get_shared_volume_name(source_agent)
                        mount_path = db.get_shared_mount_path(source_agent)
                        # Only mount if the source volume exists
                        try:
                            await volume_get(source_volume)
                            volumes[source_volume] = {'bind': mount_path, 'mode': 'rw'}
                        except docker.errors.NotFound:
                            # Source agent hasn't started yet or doesn't have shared volume
                            pass

            # FILES-001 Step 2: if file sharing is enabled, create and mount the
            # per-agent public volume (symmetric to the shared-folders expose flow).
            if db.get_file_sharing_enabled(config.name):
                public_volume_name = db.get_public_volume_name(config.name)
                public_volume_created = False
                try:
                    await volume_get(public_volume_name)
                except docker.errors.NotFound:
                    await volume_create(
                        name=public_volume_name,
                        labels={
                            'trinity.platform': 'agent-public',
                            'trinity.agent-name': config.name,
                        },
                    )
                    public_volume_created = True

                if public_volume_created:
                    try:
                        await containers_run(
                            'alpine',
                            command='chown 1000:1000 /public',
                            volumes={public_volume_name: {'bind': '/public', 'mode': 'rw'}},
                            remove=True,
                        )
                    except Exception as e:
                        logger.warning(f"Could not fix public volume ownership: {e}")

                volumes[public_volume_name] = {'bind': db.get_public_mount_path(), 'mode': 'rw'}

            # Get system-wide full_capabilities setting (not per-agent)
            full_capabilities = get_agent_full_capabilities()

            # Create container with security settings
            # Security principle: ALWAYS apply baseline security, even in full_capabilities mode
            # - Always drop ALL caps, then add back only what's needed
            # - Always apply AppArmor profile
            # - Always apply noexec,nosuid to /tmp
            container = await containers_run(
                config.base_image,
                detach=True,
                name=f"agent-{config.name}",
                ports={'22/tcp': config.port},
                volumes=volumes,
                environment=env_vars,
                labels={
                    'trinity.platform': 'agent',
                    'trinity.agent-name': config.name,
                    'trinity.agent-type': config.type,
                    'trinity.ssh-port': str(config.port),
                    'trinity.cpu': config.resources['cpu'],
                    'trinity.memory': config.resources['memory'],
                    'trinity.created': utc_now_iso(),
                    'trinity.template': config.template or '',
                    'trinity.agent-runtime': runtime,
                    'trinity.runtime': runtime,
                    'trinity.full-capabilities': str(full_capabilities).lower(),
                    'trinity.base-image-version': get_platform_version()
                },
                # Always apply AppArmor for additional sandboxing
                security_opt=['apparmor:docker-default'],
                # Always drop ALL capabilities first (defense in depth)
                cap_drop=['ALL'],
                # Add back only the capabilities needed for the mode
                cap_add=FULL_CAPABILITIES if full_capabilities else RESTRICTED_CAPABILITIES,
                read_only=False,
                # Always apply noexec,nosuid to /tmp for security (#1098: scratch
                # is redirected off this tiny tmpfs via the TMPDIR env var).
                tmpfs=AGENT_TMPFS_MOUNT,
                network='trinity-agent-network',
                mem_limit=config.resources.get('memory') or _get_default_resource('memory'),
                # #1126: nano_cpus (Linux CFS quota), NOT cpu_count — the latter
                # is Windows-only in docker-py and left NanoCpus=0, so newly
                # created agents never got a CPU limit on Linux.
                nano_cpus=int(config.resources.get('cpu') or _get_default_resource('cpu')) * 1_000_000_000,
            )

            agent_status = get_agent_status_from_container(container)

            if ws_manager:
                await ws_manager.broadcast(json.dumps({
                    "event": "agent_created",
                    "data": {
                        "name": agent_status.name,
                        "type": agent_status.type,
                        "status": agent_status.status,
                        "port": agent_status.port,
                        "created": agent_status.created.isoformat(),
                        "resources": agent_status.resources,
                        "container_id": agent_status.container_id
                    }
                }))

            # #1129: seed require_email from the fleet-wide default
            # (secure-by-default ON) at creation; owners can override per agent.
            db.register_agent_owner(
                config.name,
                current_user.username,
                require_email=get_agent_default_require_email(),
            )

            # Persist auto-assigned subscription (#74)
            if auto_assigned_subscription_id:
                try:
                    db.assign_subscription_to_agent(config.name, auto_assigned_subscription_id)
                except Exception as e:
                    logger.warning(f"Failed to persist subscription assignment for {config.name}: {e}")

            # AVATAR-003: Seed avatar prompt from template
            _avatar_prompt = template_data.get("avatar_prompt") if template_data else None
            if _avatar_prompt:
                try:
                    db.set_default_avatar(config.name, _avatar_prompt, datetime.now(timezone.utc).isoformat())
                    logger.info(f"[AVATAR-003] Seeded avatar prompt from template for {config.name}")
                except Exception as e:
                    logger.warning(f"[AVATAR-003] Failed to seed avatar prompt for {config.name}: {e}")

            # Phase 9.10: Grant default permissions (Option B - same-owner agents)
            try:
                permissions_count = db.grant_default_permissions(config.name, current_user.username)
                if permissions_count > 0:
                    logger.info(f"Granted {permissions_count} default permissions for agent {config.name}")
            except Exception as e:
                logger.warning(f"Failed to grant default permissions for {config.name}: {e}")

            # Phase 7: git config was already reserved and persisted via
            # `reserve_and_generate_instance_id` earlier in this function
            # (S7 Layer 0). No second db.create_git_config call here — that
            # would either be a no-op (agent_name UNIQUE) or, worse, mask
            # a Layer 2 conflict.

            # S4 (#383): Materialize persistent-state allowlist into the agent.
            # Runtime sync/reset paths read `.trinity/persistent-state.yaml`;
            # template.yaml is only read at creation (10-min cache), so this
            # is the source of truth going forward. Non-fatal on failure —
            # reset operations fall back to the default list at read time.
            persistent_state = (
                (template_data or {}).get(
                    "persistent_state", git_service.DEFAULT_PERSISTENT_STATE
                )
            )
            try:
                await git_service.materialize_persistent_state(
                    config.name, persistent_state
                )
            except Exception as e:
                logger.warning(
                    f"[S4] Failed to materialize persistent-state.yaml for "
                    f"{config.name}: {e}"
                )

            # #389 S1a: opt non-source-mode GitHub-template agents into the
            # auto-sync heartbeat by default. Source-mode agents stay opt-in
            # (auto-pushing to main would clobber protected branches).
            if github_repo_for_agent and enable_git_sync_for_template and not config.source_mode:
                try:
                    _set_git_auto_sync_if_enabled(
                        agent_name=config.name,
                        github_repo_for_agent=github_repo_for_agent,
                        enable_git_sync_for_template=enable_git_sync_for_template,
                        source_mode=config.source_mode,
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to enable auto-sync for {config.name}: {e}"
                    )

            return agent_status
        except Exception as e:
            # S7 Layer 0 (#382): if anything after the reservation fails,
            # roll back the agent_git_config row so the working branch is
            # released and a retry can claim it fresh.
            if github_repo_for_agent and enable_git_sync_for_template and git_instance_id:
                try:
                    db.delete_git_config(config.name)
                except Exception as cleanup_exc:
                    logger.warning(
                        "Failed to roll back agent_git_config for %s after "
                        "creation failure: %s",
                        config.name,
                        cleanup_exc,
                    )
            logger.error(f"Failed to create agent {config.name}: {e}")
            raise HTTPException(status_code=500, detail="Failed to create agent. Please try again.")
    else:
        raise HTTPException(
            status_code=503,
            detail="Docker not available - cannot create agents in demo mode"
        )
