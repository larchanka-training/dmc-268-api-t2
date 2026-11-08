"""Public rule corpus contract, independent of provider execution."""

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest
from scripts.eval_harness.rules_dataset import load_rules_dataset

from app.context.rules import parse_rules

CASE_COUNT = 12
RULE_COUNT = 3
ROOT = Path(__file__).parent / "fixtures/rules-benchmark"


def test_twelve_public_examples_have_balanced_hand_labels() -> None:
    fixtures = load_rules_dataset(ROOT)
    assert len(fixtures) == CASE_COUNT
    assert len({fixture.id for fixture in fixtures}) == CASE_COUNT
    counts = Counter(
        (fixture.category, bool(fixture.expected_findings)) for fixture in fixtures
    )
    assert counts == {
        (category, violating): 2
        for category in ("security", "performance", "maintainability")
        for violating in (False, True)
    }
    parsed = parse_rules((ROOT / "rules.md").read_text())
    assert parsed.state == "valid"
    assert not parsed.diagnostics
    assert len(parsed.instructions) == RULE_COUNT


def test_corpus_rejects_labels_outside_changed_lines(tmp_path: Path) -> None:

    shutil.copytree(ROOT, tmp_path / "corpus")
    corpus = tmp_path / "corpus"
    path = corpus / "fixtures/security-a/expected_findings.json"
    labels = json.loads(path.read_text())
    labels[0]["start_line"] = 99
    labels[0]["end_line"] = 99
    path.write_text(json.dumps(labels))
    with pytest.raises(ValueError, match="grounded"):
        load_rules_dataset(corpus)


def test_corpus_rejects_sources_inconsistent_with_diff(tmp_path: Path) -> None:

    shutil.copytree(ROOT, tmp_path / "corpus")
    corpus = tmp_path / "corpus"
    (corpus / "sources/security-a/new.py.txt").write_text("def changed():\n    pass\n")
    with pytest.raises(ValueError, match="source"):
        load_rules_dataset(corpus)


def test_corpus_rejects_unknown_rule_labels(tmp_path: Path) -> None:

    shutil.copytree(ROOT, tmp_path / "corpus")
    corpus = tmp_path / "corpus"
    path = corpus / "fixtures/security-a/expected_findings.json"
    labels = json.loads(path.read_text())
    labels[0]["rule_ref"] = "UNKNOWN-1"
    path.write_text(json.dumps(labels))
    with pytest.raises(ValueError, match="rule"):
        load_rules_dataset(corpus)


def test_corpus_rejects_invalid_rules_profile(tmp_path: Path) -> None:

    shutil.copytree(ROOT, tmp_path / "corpus")
    corpus = tmp_path / "corpus"
    path = corpus / "rules.md"
    path.write_text(path.read_text() + "\n# Unsupported\n")
    with pytest.raises(ValueError, match="profile"):
        load_rules_dataset(corpus)


def test_corpus_rejects_incomplete_balance(tmp_path: Path) -> None:
    shutil.copytree(ROOT, tmp_path / "corpus")
    corpus = tmp_path / "corpus"
    path = corpus / "manifest.json"
    path.write_text(json.dumps(json.loads(path.read_text())[:-1]))
    with pytest.raises(ValueError, match="balanced"):
        load_rules_dataset(corpus)
