"""Unit tests for the eval harness: validator, scoring, and an end-to-end CLI run."""

from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import httpx
import pytest
from scripts.eval_harness.cli import main, run
from scripts.eval_harness.dataset import Fixture, load_dataset
from scripts.eval_harness.llm_client import LlmConfigurationError
from scripts.eval_harness.report import aggregate, evaluate_fixture
from scripts.eval_harness.runtime import fixture_context, gateway_response
from scripts.eval_harness.scoring import MatchCounts, match_findings
from scripts.eval_harness.validator import is_grounded, parse_diff, validate_shape

from app.llm.budget import BudgetLimits, RunBudget
from app.llm.gateway import Endpoint, LLMGateway, ProviderPool
from app.llm.openai_compatible import OpenAICompatibleClient
from app.review.prompt import PromptBuilder

_DIFF = (
    "diff --git a/app/example.py b/app/example.py\n"
    "new file mode 100644\n"
    "index 0000000..1111111\n"
    "--- /dev/null\n"
    "+++ b/app/example.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+line one\n"
    "+line two\n"
    "+line three\n"
)


_SCHEMA = json.loads(
    PromptBuilder.load(
        Path(__file__).parents[1] / ".agents/reviewer", "v3"
    ).output_schema_json
)
_FULL_PERCENT = 100.0
_SHA256_LENGTH = 64


def _counts(result: MatchCounts) -> tuple[int, int, int]:
    return result.true_positives, result.false_positives, result.false_negatives


def _finding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "path": "app/example.py",
        "side": "NEW",
        "start_line": 2,
        "end_line": 2,
        "category": "security",
        "severity": "high",
        "title": "t",
        "explanation": "e",
        "evidence": "ev",
        "recommendation": "r",
        "rule_ref": None,
        "proposed_diff_fix": None,
    }
    base.update(overrides)
    return base


class TestParseDiff:
    def test_records_added_lines_on_the_new_side(self) -> None:
        changed = parse_diff(_DIFF)

        assert changed["app/example.py"].new_lines == {1, 2, 3}
        assert changed["app/example.py"].old_lines == set()

    def test_unknown_path_has_no_entry(self) -> None:
        changed = parse_diff(_DIFF)

        assert "app/other.py" not in changed


class TestIsGrounded:
    def test_accepts_a_finding_fully_within_changed_lines(self) -> None:
        changed = parse_diff(_DIFF)

        assert is_grounded(_finding(start_line=1, end_line=2), changed) is True

    def test_rejects_a_finding_outside_the_diff(self) -> None:
        changed = parse_diff(_DIFF)

        assert is_grounded(_finding(start_line=10, end_line=10), changed) is False

    def test_rejects_a_finding_on_an_unknown_path(self) -> None:
        changed = parse_diff(_DIFF)

        assert is_grounded(_finding(path="app/other.py"), changed) is False

    def test_rejects_the_wrong_side(self) -> None:
        changed = parse_diff(_DIFF)

        finding = _finding(side="OLD", start_line=1, end_line=1)

        assert is_grounded(finding, changed) is False


class TestValidateShape:
    def test_valid_response_has_no_errors(self) -> None:
        response = {"summary": "ok", "findings": [_finding()], "limitations": []}

        assert validate_shape(response, _SCHEMA) == []

    def test_unknown_category_is_rejected(self) -> None:
        response = {
            "summary": "ok",
            "findings": [_finding(category="style")],
            "limitations": [],
        }

        assert validate_shape(response, _SCHEMA) != []

    def test_missing_required_field_is_rejected(self) -> None:
        response = {"summary": "ok", "findings": [], "limitations": []}
        del response["summary"]

        assert validate_shape(response, _SCHEMA) != []

    def test_unknown_top_level_field_is_rejected(self) -> None:
        response = {
            "summary": "ok",
            "findings": [],
            "limitations": [],
            "extra": "not allowed",
        }

        assert validate_shape(response, _SCHEMA) != []


class TestMatchFindings:
    def test_exact_match_is_a_true_positive(self) -> None:
        predicted = [_finding(start_line=2, end_line=2)]
        expected = [_finding(start_line=2, end_line=2)]

        counts = match_findings(predicted, expected)

        assert _counts(counts) == (
            1,
            0,
            0,
        )

    def test_overlapping_range_still_counts_as_a_match(self) -> None:
        predicted = [_finding(start_line=1, end_line=3)]
        expected = [_finding(start_line=2, end_line=2)]

        counts = match_findings(predicted, expected)

        assert counts.true_positives == 1

    def test_different_category_is_not_a_match(self) -> None:
        predicted = [_finding(category="correctness")]
        expected = [_finding(category="security")]

        counts = match_findings(predicted, expected)

        assert _counts(counts) == (
            0,
            1,
            1,
        )

    def test_extra_predicted_finding_is_a_false_positive(self) -> None:
        predicted = [_finding(), _finding(start_line=1, end_line=1)]
        expected = [_finding()]

        counts = match_findings(predicted, expected)

        assert _counts(counts) == (
            1,
            1,
            0,
        )

    def test_missing_prediction_is_a_false_negative(self) -> None:
        counts = match_findings([], [_finding()])

        assert _counts(counts) == (
            0,
            0,
            1,
        )

    def test_duplicate_predictions_consume_at_most_one_expected_finding(self) -> None:
        predicted = [_finding(), _finding()]
        expected = [_finding()]

        counts = match_findings(predicted, expected)

        assert _counts(counts) == (
            1,
            1,
            0,
        )


class TestCliEndToEnd:
    def test_offline_run_against_a_tiny_dataset_reports_perfect_scores(
        self, tmp_path: Path
    ) -> None:
        dataset_dir = tmp_path / "dataset"
        fixture_dir = dataset_dir / "fixtures" / "sec-001-demo"
        fixture_dir.mkdir(parents=True)

        manifest = [{"id": "sec-001-demo", "category": "security", "title": "Demo"}]
        (dataset_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (fixture_dir / "pr.diff").write_text(_DIFF, encoding="utf-8")
        meta = {
            "title": "Demo",
            "description": "demo",
            "language": "python",
            "category": "security",
        }
        (fixture_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        expected = [
            {
                "path": "app/example.py",
                "side": "NEW",
                "start_line": 2,
                "end_line": 2,
                "category": "security",
                "severity": "high",
            }
        ]
        (fixture_dir / "expected_findings.json").write_text(
            json.dumps(expected), encoding="utf-8"
        )
        golden = {
            "summary": "one issue",
            "findings": [_finding()],
            "limitations": [],
        }
        (fixture_dir / "golden_response.json").write_text(
            json.dumps(golden), encoding="utf-8"
        )

        report_out = tmp_path / "report.json"
        report = run(
            [
                "--mode",
                "offline",
                "--dataset-dir",
                str(dataset_dir),
                "--report-out",
                str(report_out),
            ]
        )

        assert report["aggregate"]["fixtures_total"] == 1
        assert report["aggregate"]["shape_valid_percent"] == _FULL_PERCENT
        assert report["aggregate"]["precision"] == 1.0
        assert report["aggregate"]["recall"] == 1.0
        assert json.loads(report_out.read_text(encoding="utf-8")) == report

    def test_live_mode_without_env_vars_raises_configuration_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EVAL_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EVAL_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EVAL_LLM_MODEL", raising=False)

        dataset_dir = tmp_path / "dataset"
        (dataset_dir / "fixtures").mkdir(parents=True)
        (dataset_dir / "manifest.json").write_text("[]", encoding="utf-8")

        with pytest.raises(LlmConfigurationError):
            run(["--mode", "live", "--dataset-dir", str(dataset_dir)])


def test_matching_does_not_credit_an_old_side_finding_on_the_new_side() -> None:
    counts = match_findings([_finding(side="OLD")], [_finding(side="NEW")])
    assert _counts(counts) == (0, 1, 1)


def test_clean_corpus_grounding_is_a_percentage() -> None:

    assert aggregate([])["grounding_valid_percent"] == _FULL_PERCENT


def test_ungrounded_prediction_cannot_be_a_true_positive() -> None:

    fixture = Fixture(
        id="demo",
        category="security",
        title="Demo",
        description="",
        language="python",
        diff_text=_DIFF,
        expected_findings=[_finding()],
        golden_response={},
    )
    report = evaluate_fixture(
        fixture,
        {
            "summary": "ok",
            "findings": [_finding(start_line=2, end_line=20)],
            "limitations": [],
        },
        _SCHEMA,
    )
    assert (report.true_positives, report.false_positives, report.false_negatives) == (
        0,
        1,
        1,
    )


def test_cli_exit_status_rejects_invalid_schema(tmp_path: Path) -> None:

    dataset = tmp_path / "dataset"
    shutil.copytree(Path(__file__).parents[1] / "test-prs-dataset", dataset)
    first = json.loads((dataset / "manifest.json").read_text())[0]["id"]
    (dataset / "fixtures" / first / "golden_response.json").write_text("{}")
    assert (
        main(
            [
                "--dataset-dir",
                str(dataset),
                "--report-out",
                str(tmp_path / "report.json"),
            ]
        )
        == 1
    )


def test_cli_validates_the_canonical_schema_required_nullable_fields(
    tmp_path: Path,
) -> None:

    dataset = tmp_path / "dataset"
    shutil.copytree(Path(__file__).parents[1] / "test-prs-dataset", dataset)
    response = dataset / "fixtures" / "sec-001-sql-injection" / "golden_response.json"
    data = json.loads(response.read_text())
    del data["findings"][0]["proposed_diff_fix"]
    response.write_text(json.dumps(data))
    report = run(
        ["--dataset-dir", str(dataset), "--report-out", str(tmp_path / "report.json")]
    )
    assert report["fixtures"][0]["shape_valid"] is False


def test_deleted_file_findings_are_grounded_on_the_old_path() -> None:
    diff = (
        "diff --git a/app/example.py b/app/example.py\n"
        "deleted file mode 100644\n--- a/app/example.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-line one\n-line two\n"
    )
    assert is_grounded(_finding(side="OLD"), parse_diff(diff))


def test_report_pins_replay_provenance_without_claiming_model_quality(
    tmp_path: Path,
) -> None:
    args = ["--report-out", str(tmp_path / "report.json")]
    report = run(args)
    second = run(args)
    provenance = report["provenance"]
    assert provenance == second["provenance"]
    assert provenance["mode"] == "offline"
    assert provenance["quality_measurement"] is False
    assert provenance["model"] is None
    assert len(provenance["dataset_sha256"]) == _SHA256_LENGTH
    assert provenance["prompt_version"] == "v3"
    assert len(provenance["prompt_digest"]) == _SHA256_LENGTH
    assert len(provenance["schema_sha256"]) == _SHA256_LENGTH
    assert len(provenance["config_sha256"]) == _SHA256_LENGTH
    assert report["usage"] == {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": "0",
    }


def test_deterministic_mode_exercises_verified_prompt_and_gateway(
    tmp_path: Path,
) -> None:
    report = run(
        [
            "--mode",
            "deterministic",
            "--filter",
            "clean",
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )
    assert report["aggregate"]["shape_valid_percent"] == _FULL_PERCENT
    assert report["usage"]["requests"] == report["aggregate"]["fixtures_total"]
    assert report["provenance"]["quality_measurement"] is False
    assert report["provenance"]["provider"] == "mock"


def test_live_mode_requires_explicit_manifest_and_spend_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    for key, value in {
        "EVAL_LLM_BASE_URL": "https://unapproved.invalid",
        "EVAL_LLM_API_KEY": "fixture-key",
        "EVAL_LLM_MODEL": "unapproved",
    }.items():
        monkeypatch.setenv(key, value)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("An unapproved live HTTP call was attempted")

    monkeypatch.setattr(httpx, "post", forbidden)
    with pytest.raises(LlmConfigurationError):
        run(
            [
                "--mode",
                "live",
                "--filter",
                "clean",
                "--report-out",
                str(tmp_path / "report.json"),
            ]
        )


def test_authorized_live_path_uses_runtime_gateway_with_a_local_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    root = Path(__file__).parents[1]
    manifest = json.loads((root / "deploy/analysis-runtime.staging.json").read_text())
    manifest["profiles"]["eurorouter/review-v1"]["base_url"] = "https://llm.invalid/v1"
    manifest["profiles"]["eurorouter/review-v1"]["allowed_models"] = ["fixture-model"]
    manifest["admission"]["primary"]["model_ref"] = "fixture-model"
    manifest["admission"]["prompt_version"] = "v3"
    manifest["admission"]["prompt_digest"] = PromptBuilder.load(
        root / ".agents/reviewer", "v3"
    ).prompt_digest
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(manifest))
    monkeypatch.setenv("LLM__EUROROUTER__API_KEYS", "fixture-key")
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert body["model"] == "fixture-model"
        assert body["response_format"]["json_schema"]["schema"]["properties"][
            "findings"
        ]["items"]["required"][-2:] == ["rule_ref", "proposed_diff_fix"]
        assert (
            body["messages"][0]["content"]
            == (root / ".agents/reviewer/v3/system.md").read_text()
        )
        return httpx.Response(
            200,
            json={
                "model": "fixture-model",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"summary": "ok", "findings": [], "limitations": []}
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )

    report = run(
        [
            "--mode",
            "live",
            "--allow-live",
            "--runtime-manifest",
            str(config),
            "--max-cost-usd",
            "100",
            "--input-usd-per-million",
            "1",
            "--output-usd-per-million",
            "1",
            "--filter",
            "clean",
            "--report-out",
            str(tmp_path / "report.json"),
        ],
        transport=httpx.MockTransport(provider),
    )
    assert len(requests) == report["aggregate"]["fixtures_total"]
    assert report["provenance"]["model"] == "fixture-model"
    assert report["fixtures"][0]["model"] == "fixture-model"
    assert report["fixtures"][0]["provider"] == "eurorouter"
    assert report["provenance"]["mode"] == "live"
    assert report["usage"]["requests"] == len(requests)
    assert (
        report["usage"]["cost_usd"] == "0.000027"
    )  # Nine clean controls, 1+2 tokens each.


def test_reversed_finding_range_cannot_pass_grounding() -> None:
    assert not is_grounded(_finding(start_line=3, end_line=2), parse_diff(_DIFF))


def test_dataset_rejects_fixture_paths_outside_its_directory(tmp_path: Path) -> None:

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "manifest.json").write_text(
        json.dumps([{"id": "../../outside", "category": "clean", "title": "bad"}])
    )
    with pytest.raises(ValueError, match="fixture id"):
        load_dataset(dataset)


def test_schema_diagnostics_do_not_echo_provider_output() -> None:
    errors = validate_shape(
        {
            "summary": {"text": "fixture-sensitive-output"},
            "findings": [],
            "limitations": [],
        },
        _SCHEMA,
    )
    assert errors
    assert "fixture-sensitive-output" not in json.dumps(errors)


def test_cli_exit_status_rejects_ungrounded_replay(tmp_path: Path) -> None:

    dataset = tmp_path / "dataset"
    shutil.copytree(Path(__file__).parents[1] / "test-prs-dataset", dataset)
    response = dataset / "fixtures" / "sec-001-sql-injection" / "golden_response.json"
    data = json.loads(response.read_text())
    data["findings"][0]["end_line"] = 200
    response.write_text(json.dumps(data))
    assert (
        main(
            [
                "--dataset-dir",
                str(dataset),
                "--report-out",
                str(tmp_path / "report.json"),
            ]
        )
        == 1
    )


def test_cli_catches_replay_missing_an_expected_bug(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    shutil.copytree(Path(__file__).parents[1] / "test-prs-dataset", dataset)
    response = dataset / "fixtures" / "sec-001-sql-injection" / "golden_response.json"
    response.write_text(
        json.dumps({"summary": "miss", "findings": [], "limitations": []})
    )
    assert (
        main(
            [
                "--dataset-dir",
                str(dataset),
                "--report-out",
                str(tmp_path / "report.json"),
            ]
        )
        == 1
    )


async def test_gateway_benchmark_accounts_for_schema_in_per_call_input_limit() -> None:

    fixture = load_dataset(
        Path(__file__).parents[1] / "test-prs-dataset", category_filter="clean"
    )[0]
    builder = PromptBuilder.load(Path(__file__).parents[1] / ".agents/reviewer", "v3")
    message_tokens = builder.build(
        fixture_context(fixture), max_input_tokens=10000
    ).estimated_tokens
    calls: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "fixture",
                "choices": [
                    {"message": {"content": json.dumps(fixture.golden_response)}}
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        client = OpenAICompatibleClient(
            name="mock",
            http=http,
            base_url="https://fixture.invalid",
            model="fixture",
            api_key="fixture",
        )
        gateway = LLMGateway([ProviderPool("mock", [Endpoint(client)], timeout_s=120)])
        outcome = await gateway_response(
            fixture,
            builder,
            gateway,
            RunBudget(BudgetLimits(1, 80000, 16000, 600)),
            max_input_tokens=message_tokens,
        )
    assert calls == []
    assert outcome.error_code == "prompt_too_large"


def test_eval_accepts_a_changed_anchor_with_trailing_context_in_the_same_hunk() -> None:
    diff = (
        "diff --git a/app/example.py b/app/example.py\n"
        "--- a/app/example.py\n+++ b/app/example.py\n"
        "@@ -1,3 +1,3 @@\n-old\n+new\n context\n tail\n"
    )
    fixture = Fixture(
        id="changed-context",
        category="security",
        title="Demo",
        description="",
        language="python",
        diff_text=diff,
        expected_findings=[_finding(start_line=1, end_line=2)],
        golden_response={},
    )
    report = evaluate_fixture(
        fixture,
        {
            "summary": "ok",
            "findings": [_finding(start_line=1, end_line=2)],
            "limitations": [],
        },
        _SCHEMA,
    )
    assert (report.true_positives, report.false_positives, report.false_negatives) == (
        1,
        0,
        0,
    )


def test_eval_rejects_a_range_spanning_two_adjacent_hunks() -> None:
    diff = (
        "diff --git a/app/example.py b/app/example.py\n"
        "--- a/app/example.py\n+++ b/app/example.py\n"
        "@@ -1 +1 @@\n-old\n+new\n@@ -2 +2 @@\n-other\n+changed\n"
    )
    fixture = Fixture(
        id="two-hunks",
        category="security",
        title="Demo",
        description="",
        language="python",
        diff_text=diff,
        expected_findings=[_finding(start_line=1, end_line=1)],
        golden_response={},
    )
    report = evaluate_fixture(
        fixture,
        {
            "summary": "ok",
            "findings": [_finding(start_line=1, end_line=2)],
            "limitations": [],
        },
        _SCHEMA,
    )
    assert (report.true_positives, report.false_positives, report.false_negatives) == (
        0,
        1,
        1,
    )


def test_cli_rejects_an_empty_selected_dataset(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No fixtures"):
        run(
            [
                "--filter",
                "category-does-not-exist",
                "--report-out",
                str(tmp_path / "report.json"),
            ]
        )


def test_dataset_rejects_symlinked_source_outside_approved_corpus(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    shutil.copytree(Path(__file__).parents[1] / "test-prs-dataset", dataset)
    meta = dataset / "fixtures" / "sec-001-sql-injection" / "meta.json"
    outside = tmp_path / "outside.json"
    outside.write_text(meta.read_text())
    meta.unlink()
    meta.symlink_to(outside)
    with pytest.raises(ValueError, match="outside dataset"):
        load_dataset(dataset)


def test_corpus_includes_a_real_syntax_error_with_grounded_bug_label() -> None:
    fixtures = load_dataset(Path(__file__).parents[1] / "test-prs-dataset")
    syntax = [
        fixture for fixture in fixtures if fixture.id == "syntax-001-missing-colon"
    ]
    assert len(syntax) == 1
    fixture = syntax[0]
    code = "\n".join(
        line[1:]
        for line in fixture.diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    with pytest.raises(SyntaxError):
        ast.parse(code)
    assert fixture.expected_findings[0]["category"] == "correctness"
    assert is_grounded(
        fixture.golden_response["findings"][0], parse_diff(fixture.diff_text)
    )
    report = evaluate_fixture(fixture, fixture.golden_response, _SCHEMA)
    assert (report.true_positives, report.false_positives, report.false_negatives) == (
        1,
        0,
        0,
    )


def test_style_only_examples_are_clean_false_positive_controls() -> None:
    fixtures = load_dataset(Path(__file__).parents[1] / "test-prs-dataset")
    controls = [fixture for fixture in fixtures if fixture.id.startswith("syn-")]
    assert {fixture.id for fixture in controls} == {
        "syn-001-dead-code-branch",
        "syn-002-redundant-nested-conditionals",
        "syn-003-unnecessary-wrapper-function",
        "syn-004-overengineered-trivial-abstraction",
    }
    for fixture in controls:
        assert fixture.category == "clean"
        assert fixture.expected_findings == []
        assert fixture.golden_response["findings"] == []
