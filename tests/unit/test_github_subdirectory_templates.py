import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from models import AgentConfig, AgentStatus, User
from services.github_template_ref import parse_github_template_ref
from services import template_service
from services.agent_service import crud as agent_crud
from routers import settings as settings_router


class DummyAdmin:
    role = "admin"
    username = "admin"


class FakeResponse:
    def __init__(self, content: str, status_code: int = 200):
        self.status_code = status_code
        self._content = content

    def json(self):
        encoded = base64.b64encode(self._content.encode("utf-8")).decode("ascii")
        return {"content": encoded}


class FakeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse("display_name: Research Agent\ndescription: Test template\n")


def test_fetches_subdirectory_template_yaml(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(template_service.httpx, "Client", FakeClient)

    metadata = template_service._fetch_template_yaml_ref(
        parse_github_template_ref("owner/repo//research-agent"), "pat"
    )

    assert metadata["display_name"] == "Research Agent"
    assert FakeClient.calls[0][0] == (
        "https://api.github.com/repos/owner/repo/contents/research-agent/template.yaml"
    )


def test_fetch_url_encodes_each_path_segment(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(template_service.httpx, "Client", FakeClient)

    template_service._fetch_template_yaml_ref(
        parse_github_template_ref("owner/repo//templates/foo#bar"), "pat"
    )

    url = FakeClient.calls[0][0]
    assert "/contents/templates/foo%23bar/template.yaml" in url
    assert "foo#bar" not in url
    assert "templates%2Ffoo" not in url


def test_fetches_branch_ref(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(template_service.httpx, "Client", FakeClient)

    template_service._fetch_template_yaml_ref(
        parse_github_template_ref("owner/repo//research-agent@dev"), "pat"
    )

    assert FakeClient.calls[0][1]["params"] == {"ref": "dev"}


def test_cache_key_includes_path_and_branch(monkeypatch):
    template_service._metadata_cache.clear()
    fetched = []

    def fake_fetch(ref, pat):
        fetched.append(ref.canonical)
        return {"display_name": ref.canonical}

    monkeypatch.setattr(template_service, "_get_github_pat", lambda: "pat")
    monkeypatch.setattr(template_service, "_fetch_template_yaml_ref", fake_fetch)

    refs = [
        "owner/repo",
        "owner/repo//a",
        "owner/repo//b",
        "owner/repo//a@main",
        "owner/repo//a@dev",
    ]
    first = template_service._fetch_all_metadata(refs)
    second = template_service._fetch_all_metadata(refs)

    assert set(fetched) == set(refs)
    assert len(fetched) == len(refs)
    assert set(first) == set(refs)
    assert set(second) == set(refs)
    assert first["owner/repo//a"]["display_name"] == "owner/repo//a"
    assert first["owner/repo//a@dev"]["display_name"] == "owner/repo//a@dev"


def test_build_template_preserves_canonical_branch_id():
    template = template_service._build_template(
        parse_github_template_ref("owner/repo//research-agent@dev"),
        {"display_name": "Research Agent"},
    )

    assert template["id"] == "github:owner/repo//research-agent@dev"
    assert template["github_repo"] == "owner/repo//research-agent@dev"
    assert template["repo"] == "owner/repo"
    assert template["clone_repo"] == "owner/repo"
    assert template["template_path"] == "research-agent"
    assert template["source_branch"] == "dev"


@pytest.mark.parametrize(
    "raw, canonical",
    [
        ("owner/repo", "owner/repo"),
        ("owner/repo@dev", "owner/repo@dev"),
        ("owner/repo//research-agent", "owner/repo//research-agent"),
        ("owner/repo//research-agent@main", "owner/repo//research-agent@main"),
        ("github:owner/repo//research-agent@main", "owner/repo//research-agent@main"),
    ],
)
@pytest.mark.asyncio
async def test_settings_accepts_and_stores_canonical_github_template_refs(monkeypatch, raw, canonical):
    stored = []
    monkeypatch.setattr(settings_router.settings_service, "set_github_templates", stored.extend)

    body = settings_router.GitHubTemplatesUpdate(
        templates=[settings_router.GitHubTemplateEntry(github_repo=raw)]
    )

    result = await settings_router.update_github_templates(body, request=None, current_user=DummyAdmin())

    assert result == {"success": True, "count": 1}
    assert stored[0]["github_repo"] == canonical


@pytest.mark.parametrize(
    "raw",
    [
        "owner/repo//",
        "owner/repo//../x",
        "owner/repo@",
        "owner/repo@bad branch",
    ],
)
@pytest.mark.asyncio
async def test_settings_rejects_invalid_github_template_refs(monkeypatch, raw):
    called = False

    def fake_set_github_templates(templates):
        nonlocal called
        called = True

    monkeypatch.setattr(settings_router.settings_service, "set_github_templates", fake_set_github_templates)
    body = settings_router.GitHubTemplatesUpdate(
        templates=[settings_router.GitHubTemplateEntry(github_repo=raw)]
    )

    with pytest.raises(HTTPException) as exc:
        await settings_router.update_github_templates(body, request=None, current_user=DummyAdmin())

    assert exc.value.status_code == 400
    assert exc.value.detail == (
        f"Invalid repository format: '{raw}'. Expected 'owner/repo', "
        "'owner/repo@branch', 'owner/repo//path', or 'owner/repo//path@branch'."
    )
    assert called is False


class DummyConfig:
    name = "researcher"
    source_branch = None
    source_mode = False


def test_subdirectory_ref_sets_clone_repo_template_path_and_branch_env():
    resolved = agent_crud._resolve_github_template_config(
        "github:owner/repo//research-agent@main"
    )
    config = DummyConfig()
    config.source_branch = resolved.ref.branch
    env = {}

    agent_crud._apply_github_template_env(
        env_vars=env,
        config=config,
        github_repo_for_agent=resolved.github_repo_for_agent,
        github_pat_for_agent="pat",
        github_template_path=resolved.github_template_path,
        enable_git_sync_for_template=resolved.enable_git_sync_for_template,
        git_working_branch=None,
    )

    assert resolved.github_repo_for_agent == "owner/repo"
    assert resolved.github_template_path == "research-agent"
    assert env["GITHUB_REPO"] == "owner/repo"
    assert env["GITHUB_TEMPLATE_PATH"] == "research-agent"
    assert env["GIT_SOURCE_BRANCH"] == "main"


@pytest.mark.asyncio
async def test_subdirectory_ref_skips_git_sync_reservation(monkeypatch):
    called = False

    async def fake_reserve_and_generate_instance_id(**kwargs):
        nonlocal called
        called = True
        return "instance", "branch"

    monkeypatch.setattr(
        agent_crud.git_service,
        "reserve_and_generate_instance_id",
        fake_reserve_and_generate_instance_id,
    )

    instance_id, working_branch = await agent_crud._reserve_git_sync_for_template(
        enable_git_sync_for_template=False,
        agent_name="researcher",
        github_repo_for_agent="owner/repo",
        source_branch="main",
        source_mode=False,
    )

    assert (instance_id, working_branch) == (None, None)
    assert called is False


def test_subdirectory_ref_omits_git_sync_env_and_auto_sync_side_effect(monkeypatch):
    config = DummyConfig()
    config.source_branch = "main"
    env = {}
    auto_sync_calls = []

    monkeypatch.setattr(
        agent_crud.db,
        "set_git_auto_sync_enabled",
        lambda agent_name, enabled: auto_sync_calls.append((agent_name, enabled)),
    )

    agent_crud._apply_github_template_env(
        env_vars=env,
        config=config,
        github_repo_for_agent="owner/repo",
        github_pat_for_agent="pat",
        github_template_path="research-agent",
        enable_git_sync_for_template=False,
        git_working_branch="should-not-appear",
    )
    agent_crud._set_git_auto_sync_if_enabled(
        agent_name="researcher",
        github_repo_for_agent="owner/repo",
        enable_git_sync_for_template=False,
        source_mode=False,
    )

    assert env["GITHUB_REPO"] == "owner/repo"
    assert env["GITHUB_TEMPLATE_PATH"] == "research-agent"
    assert "GIT_SYNC_ENABLED" not in env
    assert "GIT_SYNC_AUTO" not in env
    assert "GIT_WORKING_BRANCH" not in env
    assert auto_sync_calls == []


@pytest.mark.asyncio
async def test_root_ref_keeps_git_sync_reservation_and_env(monkeypatch):
    called = []

    async def fake_reserve_and_generate_instance_id(**kwargs):
        called.append(kwargs)
        return "instance", "trinity/researcher"

    monkeypatch.setattr(
        agent_crud.git_service,
        "reserve_and_generate_instance_id",
        fake_reserve_and_generate_instance_id,
    )

    resolved = agent_crud._resolve_github_template_config("github:owner/repo")
    instance_id, working_branch = await agent_crud._reserve_git_sync_for_template(
        enable_git_sync_for_template=resolved.enable_git_sync_for_template,
        agent_name="researcher",
        github_repo_for_agent=resolved.github_repo_for_agent,
        source_branch=None,
        source_mode=False,
    )
    config = DummyConfig()
    env = {}
    agent_crud._apply_github_template_env(
        env_vars=env,
        config=config,
        github_repo_for_agent=resolved.github_repo_for_agent,
        github_pat_for_agent="pat",
        github_template_path=resolved.github_template_path,
        enable_git_sync_for_template=resolved.enable_git_sync_for_template,
        git_working_branch=working_branch,
    )

    assert (instance_id, working_branch) == ("instance", "trinity/researcher")
    assert called == [
        {
            "agent_name": "researcher",
            "github_repo": "owner/repo",
            "source_branch": "main",
            "source_mode": False,
        }
    ]
    assert env["GITHUB_REPO"] == "owner/repo"
    assert "GITHUB_TEMPLATE_PATH" not in env
    assert env["GIT_SYNC_ENABLED"] == "true"
    assert env["GIT_SYNC_AUTO"] == "true"
    assert env["GIT_WORKING_BRANCH"] == "trinity/researcher"


def _patch_create_agent_dependencies(monkeypatch, *, env_capture, reserve_calls, auto_sync_calls):
    monkeypatch.setattr(agent_crud, "docker_client", object())
    monkeypatch.setattr(agent_crud, "get_agent_by_name", lambda name: None)
    monkeypatch.setattr(agent_crud.db, "get_agent_owner", lambda name: None)
    monkeypatch.setattr(agent_crud.db, "is_agent_name_reserved", lambda name: False)
    monkeypatch.setattr(agent_crud.db, "get_agents_by_owner", lambda username: [])
    monkeypatch.setattr(agent_crud, "get_agent_quota_for_role", lambda role: 0)
    monkeypatch.setattr(agent_crud, "validate_base_image", lambda image: None)
    monkeypatch.setattr(agent_crud, "get_next_available_port", lambda: 2222)
    monkeypatch.setattr(agent_crud, "get_github_template", lambda template_id: None)
    monkeypatch.setattr(agent_crud, "get_github_pat", lambda: "pat")
    monkeypatch.setattr(agent_crud, "get_anthropic_api_key", lambda: "")
    monkeypatch.setattr(agent_crud, "get_agent_default_require_email", lambda: False)
    monkeypatch.setattr(agent_crud, "get_agent_full_capabilities", lambda: False)
    monkeypatch.setattr(agent_crud.db, "create_agent_mcp_api_key", lambda **kwargs: None)
    monkeypatch.setattr(agent_crud.db, "get_guardrails_config", lambda name: None)
    monkeypatch.setattr(agent_crud.db, "get_least_used_subscription", lambda: None)
    monkeypatch.setattr(agent_crud.db, "get_shared_folder_config", lambda name: None)
    monkeypatch.setattr(agent_crud.db, "get_file_sharing_enabled", lambda name: False)
    monkeypatch.setattr(agent_crud.db, "register_agent_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_crud.db, "grant_default_permissions", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        agent_crud.db,
        "set_git_auto_sync_enabled",
        lambda agent_name, enabled: auto_sync_calls.append((agent_name, enabled)),
    )

    class FakeGitHubService:
        def __init__(self, pat):
            self.pat = pat

        async def check_repo_exists(self, owner, repo):
            return SimpleNamespace(exists=True, private=False, default_branch="main")

    monkeypatch.setattr(agent_crud, "GitHubService", FakeGitHubService)

    async def fake_reserve_and_generate_instance_id(**kwargs):
        reserve_calls.append(kwargs)
        return "instance", "trinity/researcher"

    monkeypatch.setattr(
        agent_crud.git_service,
        "reserve_and_generate_instance_id",
        fake_reserve_and_generate_instance_id,
    )

    async def fake_materialize_persistent_state(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_crud.git_service, "materialize_persistent_state", fake_materialize_persistent_state)

    async def fake_volume_get(name):
        return object()

    async def fake_volume_create(**kwargs):
        return object()

    async def fake_containers_run(*args, **kwargs):
        env_capture.update(kwargs["environment"])
        return SimpleNamespace(id="container-id")

    monkeypatch.setattr(agent_crud, "volume_get", fake_volume_get)
    monkeypatch.setattr(agent_crud, "volume_create", fake_volume_create)
    monkeypatch.setattr(agent_crud, "containers_run", fake_containers_run)
    monkeypatch.setattr(
        agent_crud,
        "get_agent_status_from_container",
        lambda container: AgentStatus(
            name="researcher",
            type="business-assistant",
            status="running",
            port=2222,
            created=datetime.now(timezone.utc),
            resources={"cpu": "2", "memory": "4g"},
            container_id=container.id,
        ),
    )


@pytest.mark.parametrize(
    "template",
    [
        "github:owner",
        "github:owner/repo//",
        "github:owner/repo@bad branch",
    ],
)
@pytest.mark.asyncio
async def test_create_agent_rejects_invalid_github_template_refs(monkeypatch, template):
    env_capture = {}
    reserve_calls = []
    auto_sync_calls = []
    _patch_create_agent_dependencies(
        monkeypatch,
        env_capture=env_capture,
        reserve_calls=reserve_calls,
        auto_sync_calls=auto_sync_calls,
    )

    config = AgentConfig(name="researcher", template=template)
    user = User(id=1, username="admin", role="admin")

    with pytest.raises(HTTPException) as exc:
        await agent_crud.create_agent_internal(config, user, request=None)

    assert exc.value.status_code == 400
    assert reserve_calls == []
    assert env_capture == {}


@pytest.mark.asyncio
async def test_create_agent_subdirectory_ref_sets_env_and_skips_git_sync(monkeypatch):
    env_capture = {}
    reserve_calls = []
    auto_sync_calls = []
    _patch_create_agent_dependencies(
        monkeypatch,
        env_capture=env_capture,
        reserve_calls=reserve_calls,
        auto_sync_calls=auto_sync_calls,
    )

    config = AgentConfig(
        name="researcher",
        template="github:owner/repo//research-agent@main",
        source_mode=False,
    )
    user = User(id=1, username="admin", role="admin")

    await agent_crud.create_agent_internal(config, user, request=None)

    assert env_capture["GITHUB_REPO"] == "owner/repo"
    assert env_capture["GITHUB_TEMPLATE_PATH"] == "research-agent"
    assert env_capture["GIT_SOURCE_BRANCH"] == "main"
    assert reserve_calls == []
    assert "GIT_SYNC_ENABLED" not in env_capture
    assert "GIT_SYNC_AUTO" not in env_capture
    assert "GIT_WORKING_BRANCH" not in env_capture
    assert auto_sync_calls == []


@pytest.mark.asyncio
async def test_create_agent_subdirectory_ref_applies_template_runtime(monkeypatch):
    env_capture = {}
    reserve_calls = []
    auto_sync_calls = []
    _patch_create_agent_dependencies(
        monkeypatch,
        env_capture=env_capture,
        reserve_calls=reserve_calls,
        auto_sync_calls=auto_sync_calls,
    )
    fetched_refs = []

    def fake_fetch_template_yaml_ref(ref, pat):
        fetched_refs.append((ref.canonical, pat))
        return {
            "runtime": {
                "type": "opencode",
                "model": "deepseek-openai/deepseek-v4-flash",
                "permission": "standard",
            }
        }

    monkeypatch.setattr(agent_crud, "_fetch_template_yaml_ref", fake_fetch_template_yaml_ref)
    monkeypatch.setattr(agent_crud.settings_service, "get_provider_configs", lambda: {
        "deepseek-openai": {
            "id": "deepseek-openai",
            "name": "DeepSeek OpenAI",
            "protocol": "openai-compatible",
            "base_url": "https://api.deepseek.com/v1",
            "auth": {"api_key": "secret-key", "api_key_configured": True},
            "models": [{"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash"}],
        }
    })

    config = AgentConfig(
        name="researcher",
        template="github:owner/repo//research-agent@main",
        source_mode=False,
    )
    user = User(id=1, username="admin", role="admin")

    await agent_crud.create_agent_internal(config, user, request=None)

    assert fetched_refs == [("owner/repo//research-agent@main", "pat")]
    assert env_capture["AGENT_RUNTIME"] == "opencode"
    assert env_capture["AGENT_RUNTIME_MODEL"] == "deepseek-openai/deepseek-v4-flash"
    assert env_capture["TRINITY_RUNTIME_PROVIDER_ID"] == "deepseek-openai"
    assert env_capture["TRINITY_RUNTIME_MODEL_ID"] == "deepseek-v4-flash"
    assert env_capture["TRINITY_PROVIDER_DEEPSEEK_OPENAI_API_KEY"] == "secret-key"
    assert env_capture["OPENCODE_CONFIG_CONTENT"]


@pytest.mark.asyncio
async def test_create_agent_subdirectory_ref_defaults_source_branch_to_main(monkeypatch):
    env_capture = {}
    reserve_calls = []
    auto_sync_calls = []
    _patch_create_agent_dependencies(
        monkeypatch,
        env_capture=env_capture,
        reserve_calls=reserve_calls,
        auto_sync_calls=auto_sync_calls,
    )

    config = AgentConfig(
        name="researcher",
        template="github:owner/repo//research-agent",
        source_branch=None,
    )
    user = User(id=1, username="admin", role="admin")

    await agent_crud.create_agent_internal(config, user, request=None)

    assert env_capture["GITHUB_REPO"] == "owner/repo"
    assert env_capture["GITHUB_TEMPLATE_PATH"] == "research-agent"
    assert env_capture["GIT_SOURCE_BRANCH"] == "main"
    assert reserve_calls == []


@pytest.mark.asyncio
async def test_create_agent_root_ref_keeps_git_sync_reservation_and_env(monkeypatch):
    env_capture = {}
    reserve_calls = []
    auto_sync_calls = []
    _patch_create_agent_dependencies(
        monkeypatch,
        env_capture=env_capture,
        reserve_calls=reserve_calls,
        auto_sync_calls=auto_sync_calls,
    )

    config = AgentConfig(
        name="researcher",
        template="github:owner/repo",
        source_mode=False,
    )
    user = User(id=1, username="admin", role="admin")

    await agent_crud.create_agent_internal(config, user, request=None)

    assert reserve_calls == [
        {
            "agent_name": "researcher",
            "github_repo": "owner/repo",
            "source_branch": "main",
            "source_mode": False,
        }
    ]
    assert env_capture["GITHUB_REPO"] == "owner/repo"
    assert "GITHUB_TEMPLATE_PATH" not in env_capture
    assert env_capture["GIT_SYNC_ENABLED"] == "true"
    assert env_capture["GIT_SYNC_AUTO"] == "true"
    assert env_capture["GIT_WORKING_BRANCH"] == "trinity/researcher"
    assert auto_sync_calls == [("researcher", True)]
