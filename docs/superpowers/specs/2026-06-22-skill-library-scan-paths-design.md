# Skill Library Scan Paths Design

## Goal

Fix Trinity's skills library discovery so repositories such as `anthropics/skills` are recognized without breaking existing `.claude/skills` libraries.

## Current Problem

`SkillService.list_skills()` and `SkillService.get_skill()` only look under:

```text
.claude/skills/<name>/SKILL.md
```

The configured library can be a valid skills repository while using a different common layout, for example:

```text
skills/<name>/SKILL.md
```

That makes sync appear successful while `skill_count` remains `0`.

## Supported Layouts

Discovery will support these controlled paths, in priority order:

1. `.claude/skills/*/SKILL.md`
2. `.agents/skills/*/SKILL.md`
3. `skills/*/SKILL.md`

The scanner will not recursively scan the entire repository. This avoids accidental matches and keeps the library contract predictable.

## Behavior

`list_skills()` will scan all supported roots, parse each `SKILL.md`, and return the existing public shape:

```json
{
  "name": "skill-directory-name",
  "description": "frontmatter or fallback description",
  "path": "actual/relative/path/to/SKILL.md"
}
```

`get_skill(skill_name)` will use the same supported paths and return the existing shape plus `content`.

`sync_library()` and `get_library_status()` keep their API shape. Their `skill_count` values automatically reflect the expanded discovery logic through `list_skills()`.

## Conflict Handling

If the same skill name appears in multiple supported roots, the first root in priority order wins:

1. `.claude/skills`
2. `.agents/skills`
3. `skills`

This preserves backward compatibility for existing Trinity-specific libraries.

## Tests

Add focused unit coverage for `SkillService` discovery:

- Finds skills in `.claude/skills/<name>/SKILL.md`.
- Finds skills in `.agents/skills/<name>/SKILL.md`.
- Finds skills in `skills/<name>/SKILL.md`.
- Returns actual relative `path` values.
- `get_skill()` returns `content` from non-`.claude` layouts.
- Duplicate names follow the documented priority order.
