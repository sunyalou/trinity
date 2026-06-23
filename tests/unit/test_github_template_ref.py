import pytest

from services.github_template_ref import GitHubTemplateRef, parse_github_template_ref


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("owner/repo", GitHubTemplateRef(canonical="owner/repo", repo="owner/repo", template_path=None, branch=None)),
        ("github:owner/repo", GitHubTemplateRef(canonical="owner/repo", repo="owner/repo", template_path=None, branch=None)),
        ("owner/repo@dev", GitHubTemplateRef(canonical="owner/repo@dev", repo="owner/repo", template_path=None, branch="dev")),
        ("owner/repo//research-agent", GitHubTemplateRef(canonical="owner/repo//research-agent", repo="owner/repo", template_path="research-agent", branch=None)),
        ("github:owner/repo//research-agent@main", GitHubTemplateRef(canonical="owner/repo//research-agent@main", repo="owner/repo", template_path="research-agent", branch="main")),
        ("owner/repo//nested/template@release/v1", GitHubTemplateRef(canonical="owner/repo//nested/template@release/v1", repo="owner/repo", template_path="nested/template", branch="release/v1")),
    ],
)
def test_parse_valid_refs(raw, expected):
    assert parse_github_template_ref(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "owner",
        "owner/repo/extra",
        "owner/repo//",
        "owner/repo///x",
        "owner/repo//a//b",
        "owner/repo//../x",
        "owner/repo//a/../b",
        "owner/repo///absolute",
        "owner/repo//a@b@main",
        "owner/repo//a b",
        "owner/repo@",
        "owner/repo@bad branch",
        "owner/repo@bad;branch",
        "owner/repo@../branch",
        "owner/repo@branch.lock",
        "owner/repo@feature/foo.lock/bar",
        "owner/repo@foo//bar",
        "owner/repo@bad@{thing}",
        "owner/repo@bad\\branch",
    ],
)
def test_parse_rejects_invalid_refs(raw):
    with pytest.raises(ValueError):
        parse_github_template_ref(raw)


def test_prefixed_canonical_omits_github_prefix():
    ref = parse_github_template_ref("github:owner/repo//research-agent@main")
    assert ref.canonical == "owner/repo//research-agent@main"
    assert ref.template_id == "github:owner/repo//research-agent@main"
