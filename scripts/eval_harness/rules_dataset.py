"""Load the independent public rule-specific corpus through existing fixtures."""

import difflib
import json
from collections import Counter
from pathlib import Path

from app.context.rules import parse_rules
from scripts.eval_harness.dataset import Fixture, dataset_file, load_dataset
from scripts.eval_harness.validator import is_grounded, parse_diff


def load_rules_dataset(root: Path) -> list[Fixture]:
    fixtures = load_dataset(root)
    expected_balance = {
        (category, violating): 2
        for category in ("security", "performance", "maintainability")
        for violating in (False, True)
    }
    if Counter(
        (item.category, bool(item.expected_findings)) for item in fixtures
    ) != expected_balance or len({item.id for item in fixtures}) != len(fixtures):
        raise ValueError("Corpus must contain twelve unique balanced examples")
    parsed = parse_rules(dataset_file(root, root / "rules.md").read_text())
    if parsed.state != "valid" or parsed.diagnostics:
        raise ValueError("Invalid rules profile")
    references = {
        instruction.split("]", 1)[0][1:] for instruction in parsed.instructions
    }
    sources = json.loads(dataset_file(root, root / "sources/manifest.json").read_text())
    for fixture in fixtures:
        if any(
            label.get("rule_ref") not in references
            for label in fixture.expected_findings
        ):
            raise ValueError("Unknown expected rule label")
        source = sources["fixtures"][fixture.id]
        old, new = (
            dataset_file(root, root / "sources" / source[side]["stored_path"])
            .read_text()
            .splitlines(keepends=True)
            for side in ("old", "new")
        )
        expected = "".join(
            difflib.unified_diff(
                old,
                new,
                fromfile="a/" + source["old"]["path"],
                tofile="b/" + source["new"]["path"],
            )
        )
        if "\n".join(fixture.diff_text.split("\n")[2:]) != expected:
            raise ValueError("Diff is inconsistent with source bytes")
        changed = parse_diff(fixture.diff_text)
        if any(
            not is_grounded(
                {**fixture.golden_response["findings"][index], **label}, changed
            )
            for index, label in enumerate(fixture.expected_findings)
        ):
            raise ValueError("Expected labels must be grounded in changed lines")
    return fixtures
