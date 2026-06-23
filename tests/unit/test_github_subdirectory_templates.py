import base64

from services.github_template_ref import parse_github_template_ref
from services import template_service


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
