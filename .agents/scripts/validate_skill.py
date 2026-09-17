#!/usr/bin/env python3
"""
Project skill validator, adapted from OpenAI Codex skill-creator quick_validate.py.
Licensed under Apache-2.0; see validate_skill.LICENSE.txt.
Local changes: permit disable-model-invocation, read UTF-8, and update CLI usage.
"""

import re
import sys
from pathlib import Path

import yaml

MAX_SKILL_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
EXPECTED_ARGV_LEN = 2

ALLOWED_FRONTMATTER_PROPERTIES = frozenset(
    {
        "name",
        "description",
        "license",
        "allowed-tools",
        "metadata",
        "disable-model-invocation",
    }
)

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
NAME_RE = re.compile(r"^[a-z0-9-]+$")
FENCE_LINE_RE = re.compile(r"^[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?(`{3,}|~{3,})(.*)$")
TODO_LINE_RE = re.compile(r"[ ]{0,3}\[TODO:[^\n]*\][ \t]*")


def _load_skill_md(skill_path):
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return None, "SKILL.md not found"
    return skill_md.read_text(encoding="utf-8"), None


def _parse_frontmatter(content):
    if not content.startswith("---"):
        return None, None, "No YAML frontmatter found"

    match = FRONTMATTER_RE.match(content)
    if not match:
        return None, None, "Invalid frontmatter format"

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        return None, None, f"Invalid YAML in frontmatter: {e}"

    if not isinstance(frontmatter, dict):
        return None, None, "Frontmatter must be a YAML dictionary"

    return frontmatter, match, None


def _check_allowed_keys(frontmatter):
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_FRONTMATTER_PROPERTIES
    if not unexpected_keys:
        return None
    allowed = ", ".join(sorted(ALLOWED_FRONTMATTER_PROPERTIES))
    unexpected = ", ".join(sorted(unexpected_keys))
    return (
        f"Unexpected key(s) in SKILL.md frontmatter: {unexpected}. "
        f"Allowed properties are: {allowed}"
    )


def _check_name(frontmatter):
    if "name" not in frontmatter:
        return "Missing 'name' in frontmatter"

    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return f"Name must be a string, got {type(name).__name__}"

    name = name.strip()
    if not name:
        return None

    if not NAME_RE.match(name):
        msg = (
            f"Name '{name}' should be hyphen-case "
            "(lowercase letters, digits, and hyphens only)"
        )
    elif name.startswith("-") or name.endswith("-") or "--" in name:
        msg = (
            f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens"
        )
    elif len(name) > MAX_SKILL_NAME_LENGTH:
        msg = (
            f"Name is too long ({len(name)} characters). "
            f"Maximum is {MAX_SKILL_NAME_LENGTH} characters."
        )
    else:
        msg = None
    return msg


def _check_description(frontmatter):
    if "description" not in frontmatter:
        return "Missing 'description' in frontmatter"

    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return f"Description must be a string, got {type(description).__name__}"

    description = description.strip()
    if description.startswith("[TODO:"):
        return "Description contains an unfinished TODO placeholder"
    if not description:
        return None

    if "<" in description or ">" in description:
        msg = "Description cannot contain angle brackets (< or >)"
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        msg = (
            f"Description is too long ({len(description)} characters). "
            f"Maximum is {MAX_DESCRIPTION_LENGTH} characters."
        )
    else:
        msg = None
    return msg


def _update_fence_state(fence, fence_marker, fence_length):
    marker = fence.group(1)
    if fence_marker is None:
        return marker[0], len(marker)
    if (
        marker[0] == fence_marker
        and len(marker) >= fence_length
        and not fence.group(2).strip()
    ):
        return None, 0
    return fence_marker, fence_length


def _check_body_todos(content, match_end):
    body = content[match_end:]
    fence_marker = None
    fence_length = 0
    for line in body.splitlines():
        fence = FENCE_LINE_RE.match(line)
        if fence:
            fence_marker, fence_length = _update_fence_state(
                fence, fence_marker, fence_length
            )
            continue

        if fence_marker is None and TODO_LINE_RE.fullmatch(line):
            return "Skill instructions contain an unfinished TODO placeholder"
    return None


def validate_skill(skill_path):
    """Basic validation of a skill"""
    skill_path = Path(skill_path)

    content, error = _load_skill_md(skill_path)
    if error:
        return False, error

    frontmatter, match, error = _parse_frontmatter(content)
    if error:
        return False, error

    for check in (
        _check_allowed_keys(frontmatter),
        _check_name(frontmatter),
        _check_description(frontmatter),
        _check_body_todos(content, match.end()),
    ):
        if check:
            return False, check

    return True, "Skill is valid!"


if __name__ == "__main__":
    if len(sys.argv) != EXPECTED_ARGV_LEN:
        print("Usage: python validate_skill.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if valid else 1)
