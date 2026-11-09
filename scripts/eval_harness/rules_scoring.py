"""Rule detection and attribution are independent; Gold scoring is unchanged."""

from collections import Counter
from typing import Any

from scripts.eval_harness.validator import is_grounded, parse_diff, validate_shape


def metrics(tp: int, fp: int, fn: int) -> dict[str, Any]:
    """Micro metrics; every zero denominator returns zero (including F1)."""
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
    }


def _pairs(
    predicted: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    *,
    exact: bool,
) -> list[tuple[int, int]]:
    edges = [
        [
            index
            for index, target in enumerate(expected)
            if all(
                candidate[key] == target[key] for key in ("path", "side", "category")
            )
            and candidate["start_line"] <= target["end_line"]
            and target["start_line"] <= candidate["end_line"]
            and (not exact or candidate.get("rule_ref") == target["rule_ref"])
        ]
        for candidate in predicted
    ]
    owners: dict[int, int] = {}

    def assign(candidate: int, seen: set[int]) -> bool:
        for target in edges[candidate]:
            if target in seen:
                continue
            seen.add(target)
            if target not in owners or assign(owners[target], seen):
                owners[target] = candidate
                return True
        return False

    for candidate in range(len(predicted)):
        assign(candidate, set())
    return [(candidate, target) for target, candidate in owners.items()]


def _attribution_pairs(  # noqa: PLR0912 -- bounded min-cost matching residual graph
    predicted: list[dict[str, Any]],
    expected: list[dict[str, Any]],
) -> list[tuple[int, int]]:
    # Successive shortest augmenting paths on a unit-capacity bipartite graph.
    # An exact-ref edge outweighs all possible ordinary edges combined.
    count = len(predicted) + len(expected) + 2
    source, sink = count - 2, count - 1
    graph: list[list[list[int]]] = [[] for _ in range(count)]

    def edge(start: int, end: int, cost: int) -> list[int]:
        forward = [end, len(graph[end]), 1, cost]
        backward = [start, len(graph[start]), 0, -cost]
        graph[start].append(forward)
        graph[end].append(backward)
        return forward

    for index in range(len(predicted)):
        edge(source, index, 0)
    for index in range(len(expected)):
        edge(len(predicted) + index, sink, 0)
    candidates = []
    for p, candidate in enumerate(predicted):
        for e, target in enumerate(expected):
            if (
                all(
                    candidate[key] == target[key]
                    for key in ("path", "side", "category")
                )
                and candidate["start_line"] <= target["end_line"]
                and target["start_line"] <= candidate["end_line"]
            ):
                weight = count if candidate.get("rule_ref") == target["rule_ref"] else 1
                candidates.append((p, e, edge(p, len(predicted) + e, -weight)))
    while True:
        distances = [float("inf")] * count
        distances[source] = 0
        previous: dict[int, tuple[int, int]] = {}
        for _ in range(count - 1):
            changed = False
            for start in range(count):
                for index, (end, _, capacity, cost) in enumerate(graph[start]):
                    if capacity and distances[start] + cost < distances[end]:
                        distances[end] = distances[start] + cost
                        previous[end] = (start, index)
                        changed = True
            if not changed:
                break
        if distances[sink] >= 0:
            break
        current = sink
        while current != source:
            start, index = previous[current]
            chosen = graph[start][index]
            chosen[2] -= 1
            graph[current][chosen[1]][2] += 1
            current = start
    return [(p, e) for p, e, forward in candidates if forward[2] == 0]


def score_rules(
    predicted: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> dict[str, Any]:
    """Maximum one-to-one matching separately for detection and exact rule refs.

    Missing/wrong references classify location matches remaining after exact rule
    attribution. Unjustified counts rule-bearing predictions left unmatched.
    Callers must validate shape and changed-line grounding before this seam.
    """
    detection = _pairs(predicted, expected, exact=False)
    allocation = _attribution_pairs(predicted, expected)
    attributed = [
        (p, e)
        for p, e in allocation
        if predicted[p].get("rule_ref") == expected[e]["rule_ref"]
    ]
    residual = [(p, e) for p, e in allocation if (p, e) not in attributed]
    missing = sum(not predicted[p].get("rule_ref") for p, _ in residual)
    wrong = len(residual) - missing
    rule_predictions = sum(bool(p.get("rule_ref")) for p in predicted)
    expected_refs = Counter(item["rule_ref"] for item in expected)
    predicted_refs = Counter(
        item["rule_ref"] for item in predicted if item.get("rule_ref")
    )
    attributed_refs = Counter(predicted[p]["rule_ref"] for p, _ in attributed)
    return {
        "by_rule": {
            reference: metrics(
                attributed_refs[reference],
                predicted_refs[reference] - attributed_refs[reference],
                expected_refs[reference] - attributed_refs[reference],
            )
            for reference in sorted(expected_refs.keys() | predicted_refs.keys())
        },
        "clean": {
            "case": not expected,
            "false_positive": not expected and bool(predicted),
        },
        "detection": metrics(
            len(detection),
            len(predicted) - len(detection),
            len(expected) - len(detection),
        ),
        "attribution": {
            **metrics(
                len(attributed),
                rule_predictions - len(attributed),
                len(expected) - len(attributed),
            ),
            "missing": missing,
            "wrong": wrong,
            "unjustified": rule_predictions - len(attributed) - wrong,
        },
    }


def evaluate_rules(
    diff: str,
    expected: list[dict[str, Any]],
    response: Any,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Validate provider/canonical shape before scoring any prediction."""

    errors = validate_shape(response, schema)
    predicted = [] if errors else response["findings"]
    changed = parse_diff(diff)
    grounded = [p for p in predicted if is_grounded(p, changed)]
    ungrounded = [p for p in predicted if not is_grounded(p, changed)]
    score = score_rules(grounded, expected)
    count = score["detection"]
    score["detection"] = metrics(
        count["tp"], count["fp"] + len(ungrounded), count["fn"]
    )
    count = score["attribution"]
    extra = sum(bool(p.get("rule_ref")) for p in ungrounded)
    score["attribution"] = {
        **count,
        **metrics(count["tp"], count["fp"] + extra, count["fn"]),
        "unjustified": count["unjustified"] + extra,
    }
    for reference, extra_fp in Counter(
        item["rule_ref"] for item in ungrounded if item.get("rule_ref")
    ).items():
        count = score["by_rule"].get(reference, metrics(0, 0, 0))
        score["by_rule"][reference] = metrics(
            count["tp"], count["fp"] + extra_fp, count["fn"]
        )
    score["clean"]["false_positive"] = not expected and bool(predicted)
    return {**score, "schema_errors": errors, "grounding_errors": len(ungrounded)}
