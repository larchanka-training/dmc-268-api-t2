"""Input guards: cheap pre-LLM file skips with an enumerated reason."""

import pytest

from app.vcs.diff import InvalidDiff, parse_diff
from app.vcs.input_guards import FileSkipReason, InputGuardPolicy


def _modified(path: str, *added: str) -> str:
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1 +1,{len(added) + 1} @@\n"
        " keep = 1\n"
        f"{body}"
    )


def _details(raw: str, policy: InputGuardPolicy | None = None) -> dict[str, str]:
    parsed = parse_diff(raw, guards=policy or InputGuardPolicy())
    return {item["path"]: item["detail"] for item in parsed.skipped}


def test_deleted_file_is_skipped_as_removed() -> None:
    raw = """diff --git a/old.py b/old.py
deleted file mode 100644
index 2222222..0000000
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-answer = 42
"""

    assert _details(raw) == {"old.py": FileSkipReason.REMOVED}


def test_pure_rename_without_hunks_is_skipped() -> None:
    raw = """diff --git a/old_name.py b/new_name.py
similarity index 100%
rename from old_name.py
rename to new_name.py
"""

    assert _details(raw) == {"new_name.py": FileSkipReason.PURE_RENAME}


def test_binary_patch_is_skipped_as_binary() -> None:
    raw = """diff --git a/logo.bin b/logo.bin
index 1111111..2222222 100644
Binary files a/logo.bin and b/logo.bin differ
"""

    assert _details(raw) == {"logo.bin": FileSkipReason.BINARY}


def test_patch_over_size_threshold_is_skipped_as_too_large() -> None:
    raw = _modified("src/big.py", "x = '" + "a" * 200 + "'")

    policy = InputGuardPolicy(max_patch_bytes=100)

    assert _details(raw, policy) == {"src/big.py": FileSkipReason.TOO_LARGE}


def test_wire_coverage_keeps_canonical_reason_without_detail() -> None:
    raw = """diff --git a/old.py b/old.py
deleted file mode 100644
index 2222222..0000000
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-answer = 42
"""

    parsed = parse_diff(raw, guards=InputGuardPolicy())

    assert parsed.coverage["skipped"] == [
        {
            "path": "old.py",
            "ranges": [{"start_line": 1, "end_line": 1}],
            "reason": "policy",
        }
    ]
    assert parsed.review_coverage["skipped"][0]["detail"] == "removed"


def test_without_guards_parser_keeps_legacy_behaviour() -> None:
    raw = """diff --git a/old.py b/old.py
deleted file mode 100644
index 2222222..0000000
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-answer = 42
"""

    parsed = parse_diff(raw)

    assert parsed.skipped == []
    assert parsed.files[0]["diff"]["status"] == "deleted"


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("package-lock.json", FileSkipReason.LOCKFILE),
        ("web/yarn.lock", FileSkipReason.LOCKFILE),
        ("pnpm-lock.yaml", FileSkipReason.LOCKFILE),
        ("poetry.lock", FileSkipReason.LOCKFILE),
        ("Cargo.lock", FileSkipReason.LOCKFILE),
        ("svc/go.sum", FileSkipReason.LOCKFILE),
        ("api/user.pb.go", FileSkipReason.GENERATED),
        ("proto/user_pb2.py", FileSkipReason.GENERATED),
        ("src/schema.generated.ts", FileSkipReason.GENERATED),
        ("ui/src/__generated__/types.ts", FileSkipReason.GENERATED),
        ("dist/app.js", FileSkipReason.BUNDLE),
        ("web/build/index.js", FileSkipReason.BUNDLE),
        ("static/app.min.js", FileSkipReason.BUNDLE),
        ("static/app.min.css", FileSkipReason.BUNDLE),
        ("static/app.js.map", FileSkipReason.BUNDLE),
        ("static/main.bundle.js", FileSkipReason.BUNDLE),
        ("vendor/lib/x.go", FileSkipReason.VENDORED),
        ("web/node_modules/x/index.js", FileSkipReason.VENDORED),
        ("assets/logo.png", FileSkipReason.BINARY),
        ("fonts/inter.woff2", FileSkipReason.BINARY),
        ("pkg/module.wasm", FileSkipReason.BINARY),
        ("lib/native.so", FileSkipReason.BINARY),
    ],
)
def test_path_patterns_skip_with_reason(path: str, reason: FileSkipReason) -> None:
    assert _details(_modified(path, "x")) == {path: reason}


@pytest.mark.parametrize(
    "path",
    [
        "migrations/versions/0001_init.py",
        "app/alembic/versions/x.py",
        "db/migrate/1.rb",
    ],
)
def test_migrations_are_reviewed_unless_excluded(path: str) -> None:
    raw = _modified(path, "op.drop_table('users')")

    assert _details(raw) == {}
    assert _details(raw, InputGuardPolicy(exclude_migrations=True)) == {
        path: FileSkipReason.MIGRATION
    }


def test_repository_exclude_glob_is_reported_as_ignored() -> None:
    raw = _modified("docs/spec.md", "text")

    assert parse_diff(raw, ignore_globs=["docs/**"], guards=InputGuardPolicy()).skipped[
        0
    ]["detail"] == (FileSkipReason.IGNORED)


def test_repository_include_glob_overrides_default_patterns() -> None:
    raw = _modified("build/settings.py", "DEBUG = False")

    parsed = parse_diff(raw, guards=InputGuardPolicy(include_globs=("build/*.py",)))

    assert parsed.skipped == []


def test_skipped_files_do_not_count_toward_admission_limits() -> None:
    lock = _modified("package-lock.json", *(f'"dep{n}": "1.0.0",' for n in range(2500)))
    raw = lock + _modified("src/app.py", "value = 2")

    parsed = parse_diff(raw, guards=InputGuardPolicy())

    assert [f["diff"]["new_path"] for f in parsed.files] == ["src/app.py"]
    assert _details(raw) == {"package-lock.json": FileSkipReason.LOCKFILE}


def test_null_byte_in_added_text_is_skipped_as_binary() -> None:
    assert _details(_modified("data/blob.txt", "abc\x00def")) == {
        "data/blob.txt": FileSkipReason.BINARY
    }


@pytest.mark.parametrize(
    "marker",
    [
        "# @generated by tool",
        "// Code generated by protoc-gen-go. DO NOT EDIT.",
        "/* autogenerated file */",
        "# This file is auto-generated",
    ],
)
def test_generation_marker_in_header_is_skipped(marker: str) -> None:
    raw = """diff --git a/src/client.py b/src/client.py
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/src/client.py
@@ -0,0 +1,2 @@
"""
    raw += f"+{marker}\n+value = 1\n"

    assert _details(raw) == {"src/client.py": FileSkipReason.GENERATED}


def test_reviewed_migration_ignores_framework_generation_header() -> None:
    raw = """diff --git a/app/migrations/0002_user.py b/app/migrations/0002_user.py
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/app/migrations/0002_user.py
@@ -0,0 +1,2 @@
+# Generated by Django 5.1 on 2026-11-04
+from django.db import migrations
"""

    assert _details(raw) == {}


def test_generation_phrase_inside_code_is_not_a_marker() -> None:
    raw = _modified("src/msg.py", 'NOTE = "do not edit this field"')

    assert _details(raw) == {}


def test_long_line_without_whitespace_is_skipped_as_minified() -> None:
    raw = _modified("static/app.css", "a{b:c}" * 300)

    assert _details(raw) == {"static/app.css": FileSkipReason.MINIFIED}


def test_high_average_line_length_is_skipped_as_minified() -> None:
    line = "x=1;" * 60  # 240 chars: below the single-line threshold
    policy = InputGuardPolicy(minified_avg_line_length=200)

    assert _details(_modified("static/app.js", line, line), policy) == {
        "static/app.js": FileSkipReason.MINIFIED
    }


def test_long_prose_line_in_source_is_not_minified() -> None:
    raw = _modified("src/text.py", "MESSAGE = '" + "word " * 120 + "'")

    assert _details(raw) == {}


def test_include_glob_bypasses_content_heuristics_but_not_binary() -> None:
    policy = InputGuardPolicy(include_globs=("static/**",))

    assert _details(_modified("static/app.css", "a{b:c}" * 300), policy) == {}
    assert _details(_modified("static/x.txt", "a\x00b"), policy) == {
        "static/x.txt": FileSkipReason.BINARY
    }


GITATTRIBUTES = """\
# Linguist overrides
*.gen.ts linguist-generated
/third_party/** linguist-vendored=true
schema/*.sql linguist-generated
schema/keep.sql -linguist-generated
web/api.ts linguist-generated=false
"""


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("src/client.gen.ts", FileSkipReason.GENERATED),
        ("third_party/lib/x.c", FileSkipReason.VENDORED),
        ("schema/tables.sql", FileSkipReason.GENERATED),
        ("schema/keep.sql", None),
        ("web/api.ts", None),
        ("nested/third_party/x.c", None),
    ],
)
def test_gitattributes_linguist_flags_skip_paths(
    path: str, reason: FileSkipReason | None
) -> None:
    policy = InputGuardPolicy().with_gitattributes(GITATTRIBUTES)

    assert _details(_modified(path, "x"), policy) == ({path: reason} if reason else {})


def test_include_glob_overrides_linguist_flags() -> None:
    policy = InputGuardPolicy(include_globs=("src/*.ts",)).with_gitattributes(
        GITATTRIBUTES
    )

    assert _details(_modified("src/client.gen.ts", "x"), policy) == {}


def test_repository_settings_override_deployment_defaults() -> None:
    deployment = InputGuardPolicy(max_patch_bytes=500)

    policy = deployment.for_repository(
        {
            "globs": ["docs/**"],
            "include_globs": ["dist/keep.js"],
            "exclude_migrations": True,
            "max_patch_bytes": 2048,
        }
    )

    assert policy.include_globs == ("dist/keep.js",)
    assert policy.exclude_migrations is True
    assert policy.max_patch_bytes == 2048  # noqa: PLR2004 -- override above
    assert policy.lockfile_globs == deployment.lockfile_globs


@pytest.mark.parametrize("ignores", [{}, {"globs": []}])
def test_historical_settings_keep_deployment_defaults(
    ignores: dict[str, object],
) -> None:
    deployment = InputGuardPolicy(max_patch_bytes=500)

    assert deployment.for_repository(ignores) == deployment


def test_unknown_repository_override_is_rejected() -> None:
    with pytest.raises(ValueError, match="lockfile_globs"):
        InputGuardPolicy().for_repository({"globs": [], "lockfile_globs": []})


@pytest.mark.parametrize("path", ["src/app.py", "README.md"])
def test_ordinary_source_passes_all_guards(path: str) -> None:
    parsed = parse_diff(_modified(path, "value = 2"), guards=InputGuardPolicy())

    assert parsed.skipped == []
    assert len(parsed.files) == 1


def test_raw_patch_count_stays_bounded_even_when_files_are_skipped() -> None:
    raw = "".join(_modified(f"pkg{n}/package-lock.json", "x") for n in range(6))

    with pytest.raises(InvalidDiff, match="raw file limit"):
        parse_diff(raw, guards=InputGuardPolicy(max_raw_files=5))


def test_gitattributes_rules_beyond_the_cap_are_ignored() -> None:
    attributes = "".join(f"gen{n}/** linguist-generated\n" for n in range(5))

    policy = InputGuardPolicy(max_linguist_rules=3).with_gitattributes(attributes)

    assert len(policy.linguist_rules) == 3  # noqa: PLR2004
    assert _details(_modified("gen4/x.py", "x"), policy) == {}
