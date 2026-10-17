"""Historical review renderer v1. No project imports or current defaults.

Loaded from verified source bytes for execution; the diff facade uses this same
implementation for chunk sizing. Change behavior only under a new identity.
"""

import json
import posixpath
import re
from typing import Any, Literal, NamedTuple


class PromptTooLargeError(Exception):
    pass


class Message(NamedTuple):
    role: Literal["system", "user", "assistant"]
    content: str


class BuiltPrompt(NamedTuple):
    messages: list[Message]
    prompt_version: str
    estimated_tokens: int
    limitations: list[str]
    few_shot_ids: list[str]


# Data may not open or close our delimiter tags (prompt injection, SD §12).
_DELIMITER_TAG = re.compile(r"<(/?)(pr_metadata|diff)>", re.IGNORECASE)

# Per-message framing tokens added by chat templates.
_MESSAGE_OVERHEAD_TOKENS = 4


_TRIMMED_NOTE = "Метаданные PR сокращены из-за лимита токенов."
_TRIMMED_BODY_CHARS = 1000
_TRIMMED_COMMITS = 20


class Renderer:
    def __init__(
        self,
        prompt_version: str,
        system: str,
        few_shots: list[Any],
        counter: Any,
        repair: Any,
    ) -> None:
        self.prompt_version = prompt_version
        self._system = system
        self._few_shots = few_shots
        self._counter = counter
        self._repair = repair

    def overflow_input_limit(self, limit: int) -> int:
        return int(limit * self._repair.overflow_shrink)

    def repair_messages(self, raw_text: str, details: list[str]) -> list[Message]:
        errors = "\n".join(f"- {d}" for d in details[: self._repair.max_errors])
        return [
            Message(role="assistant", content=raw_text[: self._repair.answer_chars]),
            Message(
                role="user", content=self._repair.instruction.format(errors=errors)
            ),
        ]

    def build_repair(
        self,
        payload: Any,
        *,
        raw_text: str,
        details: list[str],
        max_input_tokens: int,
    ) -> BuiltPrompt:
        extra = self.repair_messages(raw_text, details)
        extra_tokens = sum(self._counter.count(message.content) for message in extra)
        prompt = self.build(payload, max_input_tokens=max_input_tokens - extra_tokens)
        return BuiltPrompt(
            messages=[*prompt.messages, *extra],
            prompt_version=prompt.prompt_version,
            estimated_tokens=prompt.estimated_tokens + extra_tokens,
            limitations=prompt.limitations,
            few_shot_ids=prompt.few_shot_ids,
        )

    def build(self, payload: Any, *, max_input_tokens: int) -> BuiltPrompt:
        """Assemble the prompt; metadata shrinks first, the diff never does."""
        files = [f.diff for f in payload.files]
        shots = self._select_few_shots({f.language for f in files if f.language})
        prefix = self._prefix(shots)
        for level, metadata in enumerate(_metadata_levels(payload.metadata)):
            messages = [
                *prefix,
                Message(role="user", content=_user_message(metadata, files)),
            ]
            estimated = self._estimate(messages)
            if estimated <= max_input_tokens:
                return BuiltPrompt(
                    messages=messages,
                    prompt_version=self.prompt_version,
                    estimated_tokens=estimated,
                    limitations=[_TRIMMED_NOTE] if level else [],
                    few_shot_ids=[shot.id for shot in shots],
                )
        msg = "chunk diff does not fit the input token limit"
        raise PromptTooLargeError(msg)

    def _select_few_shots(self, languages: set[str]) -> list[Any]:
        """One example with findings (chunk languages first, then file order)
        plus the example without findings."""
        with_findings = [s for s in self._few_shots if s.output.findings]
        empty = [s for s in self._few_shots if not s.output.findings]
        ranked = sorted(with_findings, key=lambda s: not languages & set(s.languages))
        return ranked[:1] + empty[:1]

    def _prefix(self, shots: list[Any]) -> list[Message]:
        """System rules, then few-shot examples as user/assistant turns."""
        messages = [Message(role="system", content=self._system)]
        for shot in shots:
            messages.append(
                Message(
                    role="user",
                    content=_user_message(_metadata_block(shot.title), shot.files),
                )
            )
            messages.append(
                Message(role="assistant", content=_serialize_output(shot.output))
            )
        return messages

    def _estimate(self, messages: list[Message]) -> int:
        return sum(
            self._counter.count(m.content) + _MESSAGE_OVERHEAD_TOKENS for m in messages
        )


def _metadata_block(title: str, output_language: str = "ru") -> str:
    return f"title: {title}\noutput_language: {output_language}"


def _metadata_levels(metadata: Any) -> list[str]:
    """Any renderings from full to minimal (spec §5.4)."""
    body = metadata.body or ""
    commits = metadata.commit_messages
    return [
        _render_metadata(metadata, body, commits, with_discussions=True),
        _render_metadata(
            metadata,
            body[:_TRIMMED_BODY_CHARS],
            commits[:_TRIMMED_COMMITS],
            with_discussions=False,
        ),
        _render_metadata(metadata, "", [], with_discussions=False),
    ]


def _render_metadata(
    metadata: Any, body: str, commits: list[str], *, with_discussions: bool
) -> str:
    lines = [
        _metadata_block(metadata.title, metadata.output_language),
        f"branches: {metadata.base_ref} <- {metadata.head_ref}",
    ]
    if body:
        lines.append(f"body:\n{body}")
    if commits:
        lines.append("commits:\n" + "\n".join(f"- {m}" for m in commits))
    if with_discussions and metadata.discussions:
        lines.append(
            "discussions:\n"
            + "\n".join(f"- {d.author}: {d.body}" for d in metadata.discussions)
        )
    return "\n".join(lines)


def _neutralize(data: str) -> str:
    return _DELIMITER_TAG.sub(r"[\1\2]", data)


def _user_message(metadata: str, files: list[Any]) -> str:
    diff = _neutralize("\n\n".join(render_file_diff(f) for f in files))
    return (
        f"<pr_metadata>\n{_neutralize(metadata)}\n</pr_metadata>\n\n"
        f"<diff>\n{diff}\n</diff>"
    )


_MARKERS = {"added": "+", "deleted": "-", "context": " "}


def render_file_header(diff: Any) -> str:
    if diff.status == "renamed":
        return f"FILE {diff.old_path} -> {diff.new_path} (renamed)"
    return f"FILE {diff.new_path or diff.old_path} ({diff.status})"


def render_hunk(hunk: Any) -> str:
    header = (
        f"@@ -{hunk.old_start},{hunk.old_count} "
        f"+{hunk.new_start},{hunk.new_count} @@ {hunk.hunk_id}"
    )
    lines = [
        f"{line.old_line or '':>5} {line.new_line or '':>5} "
        f"{_MARKERS[line.kind]} {line.text}"
        for line in hunk.lines
    ]
    return "\n".join([header, *lines])


def render_file_diff(diff: Any) -> str:
    return "\n".join([render_file_header(diff), *map(render_hunk, diff.hunks)])


_FINDING_FIELDS = (
    "path",
    "side",
    "start_line",
    "end_line",
    "category",
    "severity",
    "title",
    "explanation",
    "evidence",
    "recommendation",
    "proposed_diff_fix",
)


def _serialize_output(output: Any) -> str:
    # Explicit field order, explicit nulls, compact UTF-8; no model_dump defaults.
    return json.dumps(
        {
            "summary": output.summary,
            "findings": [
                {field: getattr(finding, field) for field in _FINDING_FIELDS}
                for finding in output.findings
            ],
            "limitations": output.limitations,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def serialize_schema(output_bytes: bytes, result_bytes: bytes) -> str:
    """Resolve only the closed local document set; providers need no registry."""
    documents = {
        "schemas/llm-output/v1.json": json.loads(output_bytes),
        "schemas/review-result/v1.json": json.loads(result_bytes),
    }
    bundled = _expand_schema(
        documents["schemas/llm-output/v1.json"],
        "schemas/llm-output/v1.json",
        documents,
        frozenset(),
    )
    # These identify canonical documents, not provider transport resources.
    bundled.pop("$id", None)
    bundled.pop("$schema", None)
    return json.dumps(
        bundled,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _expand_schema(
    node: Any,
    document: str,
    documents: dict[str, Any],
    seen: frozenset[tuple[str, str]],
) -> Any:
    if isinstance(node, list):
        return [_expand_schema(item, document, documents, seen) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        path, _, pointer = node["$ref"].partition("#")
        target = (
            posixpath.normpath(posixpath.join(posixpath.dirname(document), path))
            if path
            else document
        )
        if target not in documents or (pointer and not pointer.startswith("/")):
            raise ValueError("unsupported schema reference")
        identity = (target, pointer)
        if identity in seen:
            raise ValueError("cyclic schema reference")
        value = documents[target]
        for part in pointer.split("/")[1:]:
            value = value[part.replace("~1", "/").replace("~0", "~")]
        expanded = _expand_schema(value, target, documents, seen | {identity})
        siblings = {key: value for key, value in node.items() if key != "$ref"}
        if siblings:
            return {
                "allOf": [expanded, _expand_schema(siblings, document, documents, seen)]
            }
        return expanded
    return {
        key: _expand_schema(value, document, documents, seen)
        for key, value in node.items()
    }
