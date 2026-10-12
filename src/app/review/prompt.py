"""Versioned prompt loading and assembly (SD §7, §12; spec §7).

PR text, commit messages, discussions and code go to the model as data inside
explicit delimiters. The raw prompt is never logged (SD §13).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.llm.client import ChatMessage
from app.llm.tokens import ConservativeTokenCounter, TokenCounter
from app.review.diff_render import render_file_diff
from app.review.schemas.context import ContextPayload, FileDiff, Metadata
from app.review.schemas.result import LLMReviewOutput

# Data may not open or close our delimiter tags (prompt injection, SD §12).
_DELIMITER_TAG = re.compile(r"<(/?)(pr_metadata|diff)>", re.IGNORECASE)

# Per-message framing tokens added by chat templates.
_MESSAGE_OVERHEAD_TOKENS = 4


_TRIMMED_NOTE = "Метаданные PR сокращены из-за лимита токенов."
_TRIMMED_BODY_CHARS = 1000
_TRIMMED_COMMITS = 20


class PromptConfigError(Exception):
    """Prompt artifacts are missing or do not match the manifest."""


class PromptTooLargeError(Exception):
    """The diff does not fit even with minimal metadata: re-chunk it."""


class FewShot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    languages: list[str]
    title: str
    files: list[FileDiff]
    output: LLMReviewOutput


@dataclass(frozen=True)
class BuiltPrompt:
    messages: list[ChatMessage]
    prompt_version: str
    estimated_tokens: int
    limitations: list[str]
    few_shot_ids: list[str]


class PromptBuilder:
    def __init__(
        self,
        *,
        prompt_version: str,
        system: str,
        few_shots: list[FewShot],
        counter: TokenCounter | None = None,
    ) -> None:
        self.prompt_version = prompt_version
        self._system = system
        self._few_shots = few_shots
        self._counter = counter or ConservativeTokenCounter()

    @classmethod
    def load(
        cls, root: Path, prompt_version: str, counter: TokenCounter | None = None
    ) -> PromptBuilder:
        """Load ``prompt_version`` from ``root`` after checking SHA-256 digests."""
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            entry = manifest["versions"][prompt_version]
            for relative, expected in entry["sha256"].items():
                digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
                if digest != expected:
                    msg = f"prompt file {relative} does not match its SHA-256"
                    raise PromptConfigError(msg)
            system = (root / entry["system"]).read_text(encoding="utf-8")
            raw_shots = json.loads((root / entry["few_shots"]).read_text("utf-8"))
        except (OSError, KeyError, ValueError) as exc:
            msg = f"cannot load prompt version {prompt_version!r}: {type(exc).__name__}"
            raise PromptConfigError(msg) from exc
        few_shots = [FewShot.model_validate(shot) for shot in raw_shots]
        return cls(
            prompt_version=prompt_version,
            system=system,
            few_shots=few_shots,
            counter=counter,
        )

    def build(self, payload: ContextPayload, *, max_input_tokens: int) -> BuiltPrompt:
        """Assemble the prompt; metadata shrinks first, the diff never does."""
        files = [f.diff for f in payload.files]
        shots = self._select_few_shots({f.language for f in files if f.language})
        prefix = self._prefix(shots)
        for level, metadata in enumerate(_metadata_levels(payload.metadata)):
            messages = [
                *prefix,
                ChatMessage(role="user", content=_user_message(metadata, files)),
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

    def _select_few_shots(self, languages: set[str]) -> list[FewShot]:
        """One example with findings (chunk languages first, then file order)
        plus the example without findings."""
        with_findings = [s for s in self._few_shots if s.output.findings]
        empty = [s for s in self._few_shots if not s.output.findings]
        ranked = sorted(with_findings, key=lambda s: not languages & set(s.languages))
        return ranked[:1] + empty[:1]

    def _prefix(self, shots: list[FewShot]) -> list[ChatMessage]:
        """System rules, then few-shot examples as user/assistant turns."""
        messages = [ChatMessage(role="system", content=self._system)]
        for shot in shots:
            messages.append(
                ChatMessage(
                    role="user",
                    content=_user_message(_metadata_block(shot.title), shot.files),
                )
            )
            messages.append(
                ChatMessage(role="assistant", content=shot.output.model_dump_json())
            )
        return messages

    def _estimate(self, messages: list[ChatMessage]) -> int:
        return sum(
            self._counter.count(m.content) + _MESSAGE_OVERHEAD_TOKENS for m in messages
        )


def _metadata_block(title: str, output_language: str = "ru") -> str:
    return f"title: {title}\noutput_language: {output_language}"


def _metadata_levels(metadata: Metadata) -> list[str]:
    """Metadata renderings from full to minimal (spec §5.4)."""
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
    metadata: Metadata, body: str, commits: list[str], *, with_discussions: bool
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


def _user_message(metadata: str, files: list[FileDiff]) -> str:
    diff = _neutralize("\n\n".join(render_file_diff(f) for f in files))
    return (
        f"<pr_metadata>\n{_neutralize(metadata)}\n</pr_metadata>\n\n"
        f"<diff>\n{diff}\n</diff>"
    )
