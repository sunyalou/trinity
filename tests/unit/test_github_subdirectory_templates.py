import base64

import pytest
from fastapi import HTTPException

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
