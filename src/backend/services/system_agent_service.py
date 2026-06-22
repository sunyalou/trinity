"""
System Agent Service - Auto-deployment and management of the Trinity system agent.

The system agent is a privileged platform orchestrator that:
- Is automatically deployed on platform startup
- Cannot be deleted (only re-initialized)
- Has full access to all Trinity MCP tools
- Can communicate with any agent regardless of permissions
"""
import os
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Optional, Any

from database import db
from db.agents import SYSTEM_AGENT_NAME
from services.docker_service import (
    docker_client,
    get_agent_container,
    get_next_available_port,
)
from services.docker_utils import (
    container_reload,
    container_remove,
    container_rename,
    container_start,
    container_stop,
    containers_run,
)
from services import settings_service
from services.settings_service import get_anthropic_api_key
from services.runtime_provider_templates import build_runtime_template
from services.agent_service.lifecycle import FULL_CAPABILITIES, AGENT_TMPFS_MOUNT, AGENT_DEFAULT_TMPDIR
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# Constants
SYSTEM_AGENT_TEMPLATE = "local:trinity-system"
SYSTEM_AGENT_TYPE = "system-orchestrator"
SYSTEM_AGENT_OWNER = "admin"  # System agent is owned by admin


@dataclass
class SystemAgentRuntimeTarget:
    runtime: str = "claude-code"
    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    auto_recreate_on_drift: bool = False
    configured: bool = False
    error: Optional[str] = None


@dataclass
class SystemAgentRuntimeIdentity:
    runtime: str
    provider_id: Optional[str] = None
    model_id: Optional[str] = None


@dataclass
class SystemAgentLaunchPlan:
    env: dict[str, str]
    labels: dict[str, str]
    volumes: dict[str, dict[str, str]]
    resources: dict[str, Any]
    ssh_port: int


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_runtime(runtime: Optional[str]) -> str:
    normalized = (runtime or "").strip().lower()
    if not normalized:
        return "claude-code"
    if normalized == "gemini":
        return "gemini-cli"
    return normalized


def _container_env_map(container) -> dict[str, str]:
    env_map = {}
    for item in (container.attrs or {}).get("Config", {}).get("Env", []) or []:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        env_map[key] = value
    return env_map


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _system_agent_identity_from_container(container) -> SystemAgentRuntimeIdentity:
    labels = getattr(container, "labels", None) or {}
    env_map = _container_env_map(container)

    runtime = _normalize_runtime(
        labels.get("trinity.agent-runtime") or env_map.get("AGENT_RUNTIME")
    )
    provider_id = _blank_to_none(
        labels.get("trinity.runtime-provider-id")
        or env_map.get("TRINITY_RUNTIME_PROVIDER_ID")
    )
    model_id = _blank_to_none(
        labels.get("trinity.runtime-model-id")
        or env_map.get("TRINITY_RUNTIME_MODEL_ID")
    )

    runtime_model = _blank_to_none(env_map.get("AGENT_RUNTIME_MODEL"))
    if runtime == "opencode" and (provider_id is None or model_id is None) and runtime_model:
        parsed_provider, separator, parsed_model = runtime_model.partition("/")
        if separator and parsed_provider and parsed_model:
            provider_id = provider_id or parsed_provider
            model_id = model_id or parsed_model

    return SystemAgentRuntimeIdentity(
        runtime=runtime,
        provider_id=provider_id,
        model_id=model_id,
    )


def _system_agent_target_identity(target: SystemAgentRuntimeTarget) -> SystemAgentRuntimeIdentity:
    return SystemAgentRuntimeIdentity(
        runtime=_normalize_runtime(target.runtime),
        provider_id=_blank_to_none(target.provider_id),
        model_id=_blank_to_none(target.model_id),
    )


def _system_agent_is_drifted(container, target: SystemAgentRuntimeTarget) -> bool:
    if not target.configured:
        return False
    return _system_agent_identity_from_container(container) != _system_agent_target_identity(target)


def _resolve_system_agent_target() -> SystemAgentRuntimeTarget:
    runtime_env = os.getenv("SYSTEM_AGENT_RUNTIME")
    provider_id = (os.getenv("SYSTEM_AGENT_RUNTIME_PROVIDER_ID") or "").strip() or None
    model_id = (os.getenv("SYSTEM_AGENT_RUNTIME_MODEL_ID") or "").strip() or None
    auto_recreate_on_drift = _env_flag("SYSTEM_AGENT_AUTO_RECREATE_ON_RUNTIME_DRIFT")
    runtime = _normalize_runtime(runtime_env)
    runtime_configured = runtime_env is not None and runtime_env.strip() != ""
    configured = runtime_configured or provider_id is not None or model_id is not None

    target = SystemAgentRuntimeTarget(
        runtime=runtime,
        provider_id=provider_id,
        model_id=model_id,
        auto_recreate_on_drift=auto_recreate_on_drift,
        configured=configured,
    )

    if not runtime_configured and (provider_id is not None or model_id is not None):
        target.error = "SYSTEM_AGENT_RUNTIME is required when provider or model is configured"
        return target

    if runtime == "opencode" and (provider_id is None or model_id is None):
        target.error = (
            "SYSTEM_AGENT_RUNTIME_PROVIDER_ID and SYSTEM_AGENT_RUNTIME_MODEL_ID are required for opencode"
        )
        return target

    if runtime_configured and runtime not in {"claude-code", "opencode"}:
        target.error = f"Unsupported SYSTEM_AGENT_RUNTIME: {runtime_env}"

    return target


class SystemAgentService:
    """Service for managing the Trinity system agent."""

    def is_deployed(self) -> bool:
        """Check if the system agent container exists."""
        container = get_agent_container(SYSTEM_AGENT_NAME)
        return container is not None

    async def is_running(self) -> bool:
        """Check if the system agent is running."""
        container = get_agent_container(SYSTEM_AGENT_NAME)
        if not container:
            return False
        await container_reload(container)
        return container.status == "running"

    def is_registered(self) -> bool:
        """Check if the system agent is registered in the database."""
        owner = db.get_agent_owner(SYSTEM_AGENT_NAME)
        return owner is not None

    def _build_launch_plan(
        self,
        target: SystemAgentRuntimeTarget,
        ssh_port: int,
        agent_mcp_key=None,
    ) -> SystemAgentLaunchPlan:
        """Build the system agent container launch configuration."""
        import yaml

        # Load template configuration
        templates_dir = Path("/agent-configs/templates")
        if not templates_dir.exists():
            templates_dir = Path("./config/agent-templates")

        template_name = SYSTEM_AGENT_TEMPLATE.replace("local:", "")
        template_path = templates_dir / template_name
        template_yaml = template_path / "template.yaml"

        if not template_yaml.exists():
            raise FileNotFoundError(f"System agent template not found: {template_yaml}")

        with open(template_yaml) as f:
            template_data = yaml.safe_load(f)

        # Get configuration from template
        agent_type = template_data.get("type", SYSTEM_AGENT_TYPE)
        resources = template_data.get("resources", {"cpu": "4", "memory": "8g"})

        # Build environment variables
        env_vars = {
            'AGENT_NAME': SYSTEM_AGENT_NAME,
            'AGENT_TYPE': agent_type,
            'ANTHROPIC_API_KEY': get_anthropic_api_key(),
            'ENABLE_SSH': 'true',
            'ENABLE_AGENT_UI': 'true',
            'AGENT_SERVER_PORT': '8000',
            'TEMPLATE_NAME': SYSTEM_AGENT_TEMPLATE,
            # #1098: redirect scratch off the 100 MB noexec /tmp tmpfs onto the
            # disk-backed home volume (dir created at start by startup.sh).
            'TMPDIR': AGENT_DEFAULT_TMPDIR,
        }

        # OpenTelemetry Configuration (enabled by default)
        if os.getenv('OTEL_ENABLED', '1') == '1':
            env_vars['CLAUDE_CODE_ENABLE_TELEMETRY'] = '1'
            env_vars['OTEL_METRICS_EXPORTER'] = os.getenv('OTEL_METRICS_EXPORTER', 'otlp')
            env_vars['OTEL_LOGS_EXPORTER'] = os.getenv('OTEL_LOGS_EXPORTER', 'otlp')
            env_vars['OTEL_EXPORTER_OTLP_PROTOCOL'] = os.getenv('OTEL_EXPORTER_OTLP_PROTOCOL', 'grpc')
            env_vars['OTEL_EXPORTER_OTLP_ENDPOINT'] = os.getenv('OTEL_COLLECTOR_ENDPOINT', 'http://trinity-otel-collector:4317')
            env_vars['OTEL_METRIC_EXPORT_INTERVAL'] = os.getenv('OTEL_METRIC_EXPORT_INTERVAL', '60000')

        # Inject Trinity MCP credentials
        trinity_mcp_url = os.getenv('TRINITY_MCP_URL', 'http://mcp-server:8080/mcp')
        if agent_mcp_key:
            env_vars['TRINITY_MCP_URL'] = trinity_mcp_url
            env_vars['TRINITY_MCP_API_KEY'] = agent_mcp_key.api_key

        if target.runtime == "opencode":
            providers = settings_service.get_provider_configs()
            provider = providers.get(target.provider_id) if isinstance(providers, dict) else None
            if not provider:
                raise ValueError(f"Provider '{target.provider_id}' not found")
            template = build_runtime_template(target.runtime, provider, target.model_id)
            secrets = {
                f"provider:{target.provider_id}:api_key": provider.get("auth", {}).get("api_key", "")
            }
            env_vars.update(template.materialize_env(secrets))
            env_vars["AGENT_RUNTIME_MODEL"] = template.model_arg
            env_vars["TRINITY_RUNTIME_PROVIDER_ID"] = target.provider_id
            env_vars["TRINITY_RUNTIME_MODEL_ID"] = target.model_id

        # Set up volumes
        # Note: Volume name contains "workspace" but it mounts to /home/developer (consistent with all agents)
        agent_volume_name = f"agent-{SYSTEM_AGENT_NAME}-workspace"
        volumes = {
            agent_volume_name: {'bind': '/home/developer', 'mode': 'rw'}
        }

        # Mount template directory
        # Check existence inside container (at /agent-configs/templates)
        # But mount using HOST path (for Docker to access from host filesystem)
        if template_path.exists():
            host_templates_base = os.getenv("HOST_TEMPLATES_PATH", "./config/agent-templates")
            host_template_path = Path(host_templates_base) / template_name
            volumes[str(host_template_path)] = {'bind': '/template', 'mode': 'ro'}
            logger.info(f"Mounting template from {host_template_path} to /template")

        # Container labels
        labels = {
            'trinity.platform': 'agent',
            'trinity.agent-name': SYSTEM_AGENT_NAME,
            'trinity.agent-type': agent_type,
            'trinity.ssh-port': str(ssh_port),  # Required for port tracking
            'trinity.cpu': str(resources.get('cpu', '4')),
            'trinity.memory': resources.get('memory', '8g'),
            'trinity.created': utc_now_iso(),
            'trinity.template': SYSTEM_AGENT_TEMPLATE,
            'trinity.is-system': 'true',  # Mark as system agent
            'trinity.agent-runtime': target.runtime,
        }
        if target.provider_id:
            labels['trinity.runtime-provider-id'] = target.provider_id
        if target.model_id:
            labels['trinity.runtime-model-id'] = target.model_id

        return SystemAgentLaunchPlan(
            env=env_vars,
            labels=labels,
            volumes=volumes,
            resources=resources,
            ssh_port=ssh_port,
        )

    async def ensure_deployed(self) -> dict:
        """
        Ensure the system agent is deployed and running.

        This is the main entry point called on platform startup.

        Returns:
            dict with deployment status and details
        """
        result = {
            "agent_name": SYSTEM_AGENT_NAME,
            "action": None,
            "status": None,
            "message": None
        }

        target = _resolve_system_agent_target()
        if target.error:
            result["action"] = "config_invalid"
            result["status"] = "error"
            result["message"] = f"Invalid system agent runtime configuration: {target.error}"
            logger.error(result["message"])
            return result

        # Check if already deployed and running
        if self.is_deployed():
            container = get_agent_container(SYSTEM_AGENT_NAME)
            await container_reload(container)

            # Ensure database record has is_system=True (fixes regression if record exists without flag)
            db.register_agent_owner(SYSTEM_AGENT_NAME, SYSTEM_AGENT_OWNER, is_system=True)

            if _system_agent_is_drifted(container, target):
                if not target.auto_recreate_on_drift:
                    result["action"] = "drift_detected"
                    result["status"] = "warning"
                    result["message"] = (
                        "System agent runtime drift detected; auto recreate is disabled, "
                        "existing system agent left unchanged"
                    )
                    logger.warning(result["message"])
                    return result

                ssh_port = None
                labels = getattr(container, "labels", None) or {}
                try:
                    ssh_port = int(labels.get("trinity.ssh-port"))
                except (TypeError, ValueError):
                    ssh_port = get_next_available_port()

                backup_renamed = False
                backup_stop_attempted = False
                create_attempted = False
                try:
                    # Validate the replacement launch plan before touching the running container.
                    self._build_launch_plan(target, ssh_port=ssh_port, agent_mcp_key=None)

                    backup_name = f"agent-{SYSTEM_AGENT_NAME}-backup-{uuid.uuid4().hex[:8]}"
                    await container_rename(container, backup_name)
                    backup_renamed = True
                    backup_stop_attempted = True
                    await container_stop(container)

                    create_attempted = True
                    creation_result = await self._create_system_agent(target, ssh_port=ssh_port)
                    await container_remove(container, force=True)
                    result["action"] = "recreated"
                    result["status"] = "running"
                    result["message"] = "System agent recreated after runtime drift"
                    result["details"] = creation_result
                    logger.info("System agent recreated after runtime drift")
                    return result
                except Exception as e:
                    rollback_message = "rollback not needed"
                    if backup_renamed:
                        try:
                            if create_attempted:
                                failed_replacement = get_agent_container(SYSTEM_AGENT_NAME)
                                if failed_replacement and failed_replacement is not container:
                                    await container_remove(failed_replacement, force=True)
                            await container_rename(container, f"agent-{SYSTEM_AGENT_NAME}")
                            if backup_stop_attempted:
                                await container_start(container)
                            rollback_message = "rollback restored existing system agent"
                        except Exception as rollback_error:
                            rollback_message = f"rollback failed: {rollback_error}"
                    result["action"] = "recreate_failed"
                    result["status"] = "error"
                    result["message"] = (
                        f"Failed to recreate drifted system agent: {e}; {rollback_message}"
                    )
                    logger.error(result["message"])
                    return result

            # If running, nothing to do
            if container.status == "running":
                result["action"] = "none"
                result["status"] = "running"
                result["message"] = "System agent already running"
                logger.info("System agent already running")
                return result

            # If stopped, start it
            try:
                await container_start(container)
                result["action"] = "started"
                result["status"] = "running"
                result["message"] = "System agent started"
                logger.info("System agent started")
                return result
            except Exception as e:
                result["action"] = "start_failed"
                result["status"] = "error"
                result["message"] = f"Failed to start system agent: {e}"
                logger.error(f"Failed to start system agent: {e}")
                return result

        # System agent doesn't exist - create it
        try:
            creation_result = await self._create_system_agent(target)
            result["action"] = "created"
            result["status"] = "running"
            result["message"] = "System agent created and started"
            result["details"] = creation_result
            logger.info("System agent created and started")
            return result
        except Exception as e:
            result["action"] = "create_failed"
            result["status"] = "error"
            result["message"] = f"Failed to create system agent: {e}"
            logger.error(f"Failed to create system agent: {e}")
            return result

    async def _create_system_agent(
        self,
        target: Optional[SystemAgentRuntimeTarget] = None,
        ssh_port: Optional[int] = None,
    ) -> dict:
        """
        Create the system agent container.

        Returns:
            dict with creation details
        """
        if target is None:
            target = _resolve_system_agent_target()
        if target.error:
            raise ValueError(target.error)

        # Ensure admin user exists for ownership
        admin_user = db.get_user_by_username(SYSTEM_AGENT_OWNER)
        if not admin_user:
            logger.error(f"Admin user '{SYSTEM_AGENT_OWNER}' not found. Cannot create system agent.")
            raise ValueError(f"Admin user '{SYSTEM_AGENT_OWNER}' not found")

        if ssh_port is None:
            ssh_port = get_next_available_port()

        launch_plan = self._build_launch_plan(
            target,
            ssh_port=ssh_port,
            agent_mcp_key=None,
        )

        # Create agent MCP API key with system scope
        agent_mcp_key = None
        try:
            agent_mcp_key = db.create_agent_mcp_api_key(
                agent_name=SYSTEM_AGENT_NAME,
                owner_username=SYSTEM_AGENT_OWNER,
                description="Auto-generated system agent MCP key"
            )
            if agent_mcp_key:
                # Update the key to have system scope
                self._set_system_scope(agent_mcp_key.id)
                logger.info(f"Created system-scoped MCP API key for system agent: {agent_mcp_key.key_prefix}...")
        except Exception as e:
            logger.warning(f"Failed to create MCP API key for system agent: {e}")

        if agent_mcp_key:
            launch_plan.env['TRINITY_MCP_URL'] = os.getenv('TRINITY_MCP_URL', 'http://mcp-server:8080/mcp')
            launch_plan.env['TRINITY_MCP_API_KEY'] = agent_mcp_key.api_key

        # Create the container with security settings
        # System agent uses FULL_CAPABILITIES for package installation, etc.
        # Security: Always apply baseline protections even for privileged containers
        container = await containers_run(
            'trinity-agent-base:latest',
            name=f"agent-{SYSTEM_AGENT_NAME}",
            detach=True,
            network='trinity-agent-network',
            ports={'22/tcp': launch_plan.ssh_port},
            volumes=launch_plan.volumes,
            environment=launch_plan.env,
            labels=launch_plan.labels,
            mem_limit=launch_plan.resources.get("memory", "8g"),
            # #1126: nano_cpus (Linux CFS quota), NOT cpu_count (Windows-only → NanoCpus=0).
            nano_cpus=int(launch_plan.resources.get("cpu", "4")) * 1_000_000_000,
            restart_policy={"Name": "unless-stopped"},  # Auto-restart on failure
            # Always apply AppArmor for additional sandboxing
            security_opt=['apparmor:docker-default'],
            # Always drop ALL capabilities first (defense in depth)
            cap_drop=['ALL'],
            # System agent gets full capabilities for operational tasks
            cap_add=FULL_CAPABILITIES,
            # Always apply noexec,nosuid to /tmp for security (#1098: scratch
            # redirected off this tiny tmpfs via the TMPDIR env var).
            tmpfs=AGENT_TMPFS_MOUNT,
        )

        # Register ownership with is_system=True
        db.register_agent_owner(SYSTEM_AGENT_NAME, SYSTEM_AGENT_OWNER, is_system=True)

        # Grant default permissions (system agent can talk to everyone)
        db.grant_default_permissions(SYSTEM_AGENT_NAME, SYSTEM_AGENT_OWNER)

        return {
            "container_id": container.short_id,
            "ssh_port": launch_plan.ssh_port,
            "mcp_key_created": agent_mcp_key is not None
        }

    def _set_system_scope(self, key_id: str):
        """Update MCP key to have system scope (bypasses permissions)."""
        from db.connection import get_db_connection

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE mcp_api_keys SET scope = ? WHERE id = ?",
                ("system", key_id)
            )
            conn.commit()


# Global service instance
system_agent_service = SystemAgentService()
