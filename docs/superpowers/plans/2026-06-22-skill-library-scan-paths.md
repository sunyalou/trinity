# Skill Library Scan Paths Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand Trinity skills library discovery to support `.claude/skills`, `.agents/skills`, and `skills` repository layouts.

**Architecture:** Add a small discovery abstraction inside `SkillService` that enumerates supported skill roots in priority order and returns actual `SKILL.md` paths. Keep public API shapes unchanged while making `list_skills()`, `get_skill()`, sync counts, and status counts use the shared discovery path.

**Tech Stack:** Python 3, pathlib, pytest, existing Trinity backend service patterns.

---

## File Structure

- Modify: `src/backend/services/skill_service.py`
  - Add supported scan roots.
  - Add helper methods for finding skill files and relative paths.
  - Update `list_skills()`, `_parse_skill_info()`, and `get_skill()` to use actual paths.
- Modify: `tests/unit/test_skill_service_user_agent.py`
  - Add unit tests for supported scan layouts and duplicate priority.
  - Reuse the existing isolated import/stub setup for `SkillService`.

---

### Task 1: Add failing tests for scan layouts

**Files:**
- Modify: `tests/unit/test_skill_service_user_agent.py`

- [ ] **Step 1: Add test helpers and layout tests**

Append this code after `TestSkillServiceGitUserAgent` in `tests/unit/test_skill_service_user_agent.py`:

```python


def _write_skill(root: Path, relative_dir: str, name: str, description: str) -> Path:
    skill_dir = root / relative_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nBody for {name}.\n",
        encoding="utf-8",
    )
    return skill_file


class TestSkillServiceDiscoveryLayouts:

    def test_list_skills_finds_claude_agents_and_root_skills_layouts(self, tmp_path):
        svc = skill_service_mod.SkillService()
        svc.library_path = tmp_path / "skills-library"

        _write_skill(svc.library_path, ".claude/skills", "claude-skill", "Claude layout")
        _write_skill(svc.library_path, ".agents/skills", "agent-skill", "Agents layout")
        _write_skill(svc.library_path, "skills", "root-skill", "Root skills layout")

        skills = svc.list_skills()

        assert [skill["name"] for skill in skills] == [
            "agent-skill",
            "claude-skill",
            "root-skill",
        ]
        by_name = {skill["name"]: skill for skill in skills}
        assert by_name["claude-skill"]["path"] == ".claude/skills/claude-skill/SKILL.md"
        assert by_name["agent-skill"]["path"] == ".agents/skills/agent-skill/SKILL.md"
        assert by_name["root-skill"]["path"] == "skills/root-skill/SKILL.md"
        assert by_name["root-skill"]["description"] == "Root skills layout"

    def test_get_skill_reads_content_from_root_skills_layout(self, tmp_path):
        svc = skill_service_mod.SkillService()
        svc.library_path = tmp_path / "skills-library"

        _write_skill(svc.library_path, "skills", "algorithmic-art", "Create algorithmic art")

        skill = svc.get_skill("algorithmic-art")

        assert skill is not None
        assert skill["name"] == "algorithmic-art"
        assert skill["path"] == "skills/algorithmic-art/SKILL.md"
        assert skill["description"] == "Create algorithmic art"
        assert "Body for algorithmic-art." in skill["content"]

    def test_duplicate_skill_names_use_supported_root_priority(self, tmp_path):
        svc = skill_service_mod.SkillService()
        svc.library_path = tmp_path / "skills-library"

        _write_skill(svc.library_path, "skills", "duplicate", "Root layout")
        _write_skill(svc.library_path, ".agents/skills", "duplicate", "Agents layout")
        _write_skill(svc.library_path, ".claude/skills", "duplicate", "Claude layout")

        skills = svc.list_skills()
        skill = svc.get_skill("duplicate")

        assert len([item for item in skills if item["name"] == "duplicate"]) == 1
        assert skills[0]["path"] == ".claude/skills/duplicate/SKILL.md"
        assert skills[0]["description"] == "Claude layout"
        assert skill is not None
        assert skill["path"] == ".claude/skills/duplicate/SKILL.md"
        assert "description: Claude layout" in skill["content"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/unit/test_skill_service_user_agent.py::TestSkillServiceDiscoveryLayouts -v
```

Expected: failures showing `list_skills()` does not find `.agents/skills` or `skills`, and `get_skill()` returns `None` for `skills/algorithmic-art`.

---

### Task 2: Implement supported scan roots

**Files:**
- Modify: `src/backend/services/skill_service.py:43-287`
- Test: `tests/unit/test_skill_service_user_agent.py`

- [ ] **Step 1: Add scan root constants and helper methods**

In `src/backend/services/skill_service.py`, after `_GIT_HTTP_UA_ARGS`, add:

```python
_SUPPORTED_SKILL_ROOTS = (
    Path(".claude") / "skills",
    Path(".agents") / "skills",
    Path("skills"),
)
```

Inside `class SkillService`, before `list_skills()`, add:

```python
    def _relative_skill_path(self, skill_file: Path) -> str:
        """Return a POSIX relative path from the library root."""
        try:
            return skill_file.relative_to(self.library_path).as_posix()
        except ValueError:
            return skill_file.as_posix()

    def _iter_skill_files(self):
        """
        Yield (skill_name, skill_file) pairs from supported roots in priority order.

        Duplicate names are ignored after the first hit so legacy
        .claude/skills entries override newer compatible layouts.
        """
        seen = set()
        for relative_root in _SUPPORTED_SKILL_ROOTS:
            skills_dir = self.library_path / relative_root
            if not skills_dir.exists():
                logger.debug(f"Skills directory not found: {skills_dir}")
                continue

            for skill_path in skills_dir.iterdir():
                if not skill_path.is_dir():
                    continue

                skill_name = skill_path.name
                if skill_name in seen:
                    continue

                skill_file = skill_path / "SKILL.md"
                if not skill_file.exists():
                    continue

                seen.add(skill_name)
                yield skill_name, skill_file

    def _find_skill_file(self, skill_name: str) -> Optional[Path]:
        """Find a skill by name using the supported root priority order."""
        for found_name, skill_file in self._iter_skill_files():
            if found_name == skill_name:
                return skill_file
        return None
```

- [ ] **Step 2: Update `list_skills()`**

Replace the existing `list_skills()` body with:

```python
        skills = []

        for skill_name, skill_file in self._iter_skill_files():
            skill_info = self._parse_skill_info(skill_name, skill_file)
            skills.append(skill_info)

        return sorted(skills, key=lambda s: s["name"])
```

Keep the existing docstring, but update its scan sentence to:

```python
        Scans supported skill roots for */SKILL.md files.
```

- [ ] **Step 3: Update `_parse_skill_info()` path field**

Replace the `info` initialization in `_parse_skill_info()` with:

```python
        info = {
            "name": skill_name,
            "description": None,
            "path": self._relative_skill_path(skill_file)
        }
```

- [ ] **Step 4: Update `get_skill()`**

Replace the hardcoded first line of `get_skill()`:

```python
        skill_file = self.library_path / ".claude" / "skills" / skill_name / "SKILL.md"
```

with:

```python
        skill_file = self._find_skill_file(skill_name)
```

Then replace:

```python
        if not skill_file.exists():
            return None
```

with:

```python
        if not skill_file:
            return None
```

- [ ] **Step 5: Run discovery tests**

Run:

```bash
pytest tests/unit/test_skill_service_user_agent.py::TestSkillServiceDiscoveryLayouts -v
```

Expected: all tests pass.

---

### Task 3: Run targeted regression tests

**Files:**
- Test only.

- [ ] **Step 1: Run full skill service unit test file**

Run:

```bash
pytest tests/unit/test_skill_service_user_agent.py -v
```

Expected: all tests pass, including existing git User-Agent tests.

- [ ] **Step 2: Run skills smoke tests**

Run:

```bash
pytest tests/test_skills.py -v
```

Expected: tests pass or skip according to local environment configuration; no failures caused by skill discovery shape changes.

- [ ] **Step 3: Inspect diff**

Run:

```bash
git diff -- src/backend/services/skill_service.py tests/unit/test_skill_service_user_agent.py docs/superpowers/specs/2026-06-22-skill-library-scan-paths-design.md docs/superpowers/plans/2026-06-22-skill-library-scan-paths.md
```

Expected: diff only contains the scan path change, tests, and docs for this work.

---

## Self-Review Notes

- Spec coverage: supported roots, real relative path output, duplicate priority, and `get_skill()` content lookup are all covered by Tasks 1-2.
- API stability: no route or response shape changes are planned.
- Placeholder scan: no implementation placeholders remain; test code and replacement snippets are explicit.
