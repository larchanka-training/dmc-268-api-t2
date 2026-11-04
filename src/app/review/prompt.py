"""Versioned prompt loading and assembly (SD §7, §12; spec §7).

PR text, commit messages, discussions and code go to the model as data inside
explicit delimiters. The raw prompt is never logged (SD §13).
"""

import ast
import builtins
import copy
import hashlib
import importlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field

from app.domain.frozen_configuration import RepositoryInstructions
from app.llm.client import ChatMessage
from app.llm.tokens import ConservativeTokenCounter, TokenCounter
from app.review.schemas.context import ContextPayload, FileDiff
from app.review.schemas.result import LLMReviewOutput, LLMReviewOutputV1


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
    output: LLMReviewOutputV1


class FewShotV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    languages: list[str]
    title: str
    files: list[FileDiff]
    output: LLMReviewOutput


class RepairInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    instruction: str
    answer_chars: int = Field(gt=0)
    max_errors: int = Field(gt=0)
    overflow_shrink: float = Field(gt=0, lt=1)


@dataclass(frozen=True)
class BuiltPrompt:
    messages: list[ChatMessage]
    prompt_version: str
    estimated_tokens: int
    limitations: list[str]
    few_shot_ids: list[str]
    included_rules: str | None = None


class PromptBuilder:
    def __init__(
        self,
        *,
        prompt_version: str,
        system: str,
        few_shots: list[FewShot | FewShotV2],
        counter: TokenCounter | None = None,
    ) -> None:
        self._prompt_version = prompt_version
        self._system = system
        self._few_shots = few_shots
        self._counter = counter or ConservativeTokenCounter()
        self._repair: RepairInputs | None = None
        self._verified = False
        self._repository_instructions: tuple[str, ...] = ()
        self._prompt_digest: str | None = None
        self._output_schema_json: str | None = None
        v1 = importlib.import_module("app.review.renderers.v1")
        self._renderer_module = v1
        self._renderer = v1.Renderer(
            prompt_version, system, few_shots, self._counter, None
        )

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    @property
    def provider_schema_path(self) -> str:
        self.ensure_verified()
        return _PROMPT_IDENTITIES[self._prompt_version][1]

    @property
    def result_schema_version(self) -> Literal[1, 2]:
        self.ensure_verified()
        return _PROMPT_IDENTITIES[self._prompt_version][2]

    @property
    def repository_instructions(self) -> tuple[str, ...]:
        return self._repository_instructions

    @property
    def prompt_digest(self) -> str | None:
        return self._prompt_digest

    @property
    def output_schema_json(self) -> str:
        self.ensure_verified()
        assert self._output_schema_json is not None
        return self._output_schema_json

    def ensure_verified(self) -> None:
        if not self._verified:
            raise PromptConfigError("execution requires a verified prompt bundle")

    def with_repository_instructions(
        self, instructions: RepositoryInstructions
    ) -> PromptBuilder:
        """Bind in-memory rules to the same verified v3 closure without mutation."""
        self.ensure_verified()
        if self._prompt_version != "v3":
            raise PromptConfigError("only v3 supports rules rebinding")
        builder = copy.copy(self)
        builder._repository_instructions = instructions.instructions
        builder._renderer = self._renderer_module.Renderer(
            self._prompt_version,
            self._system,
            self._few_shots,
            self._counter,
            self._repair,
            instructions.instructions,
        )
        return builder

    def overflow_input_limit(self, limit: int) -> int:
        self.ensure_verified()
        return cast(int, self._renderer.overflow_input_limit(limit))

    def repair_messages(self, raw_text: str, details: list[str]) -> list[ChatMessage]:
        self.ensure_verified()
        return [
            ChatMessage(role=m.role, content=m.content)
            for m in self._renderer.repair_messages(raw_text, details)
        ]

    @classmethod
    def load(  # noqa: PLR0913 -- verified roots, saved identity and typed user data
        cls,
        root: Path,
        prompt_version: str,
        counter: TokenCounter | None = None,
        *,
        expected_digest: str | None = None,
        source_root: Path | None = None,
        repository_instructions: RepositoryInstructions | None = None,
    ) -> PromptBuilder:
        """Load ``prompt_version`` from ``root`` after checking SHA-256 digests."""
        try:
            manifest = json.loads(
                (root / "manifest.json").read_text("utf-8"),
                object_pairs_hook=_unique_keys,
            )
            entry = manifest["versions"][prompt_version]
            artifacts = _verify_artifacts(
                root, prompt_version, entry, source_root or _SOURCE_ROOT
            )
            if (
                entry["digest_format"] != "review-prompt-bundle/v1"
                or entry["renderer_version"] not in _RENDERERS
            ):
                raise PromptConfigError("unsupported bundle descriptor")
            descriptor = {
                "digest_format": entry["digest_format"],
                "prompt_version": prompt_version,
                "renderer_version": entry["renderer_version"],
                "artifacts": sorted(
                    entry["artifacts"], key=lambda a: (a["role"], a["path"])
                ),
            }
            digest = hashlib.sha256(
                json.dumps(
                    descriptor,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            if expected_digest is not None and digest != expected_digest:
                raise PromptConfigError("prompt digest mismatch")
            system = artifacts["system"].decode("utf-8")
            raw_shots = json.loads(artifacts["few_shots"])
            repair = RepairInputs.model_validate_json(artifacts["repair"])
            module = _load_renderer(artifacts["renderer"], entry["renderer_version"])
            schema_json = module.serialize_schema(
                artifacts["llm_output_schema"], artifacts["review_result_schema"]
            )
            schema = json.loads(schema_json)
            Draft202012Validator.check_schema(schema)
            for shot in raw_shots:
                Draft202012Validator(schema).validate(shot["output"])
            shot_model = (
                FewShotV2
                if _PROMPT_IDENTITIES[prompt_version][1] == "schemas/llm-output/v2.json"
                else FewShot
            )
            few_shots = [shot_model.model_validate(shot) for shot in raw_shots]
        except (
            OSError,
            KeyError,
            ValueError,
            TypeError,
            SyntaxError,
            SchemaError,
            JSONSchemaValidationError,
        ) as exc:
            msg = f"cannot load prompt version {prompt_version!r}: {type(exc).__name__}"
            raise PromptConfigError(msg) from exc
        # Do not run the convenience constructor: it uses the current imported
        # renderer for test-only assembly. Execution uses only the checked bytes.
        builder = cls.__new__(cls)
        builder._system = system
        builder._few_shots = few_shots
        builder._repair = repair
        builder._prompt_version = prompt_version
        builder._counter = counter or ConservativeTokenCounter()
        builder._verified = True
        builder._prompt_digest = digest
        builder._renderer_module = module
        instructions = repository_instructions or RepositoryInstructions(
            instructions=()
        )
        builder._repository_instructions = instructions.instructions
        if entry["renderer_version"] == "review-renderer-v1":
            if instructions.instructions:
                raise PromptConfigError("v1 cannot render repository instructions")
            builder._renderer = module.Renderer(
                prompt_version, system, few_shots, builder._counter, repair
            )
        else:
            builder._renderer = module.Renderer(
                prompt_version,
                system,
                few_shots,
                builder._counter,
                repair,
                instructions.instructions,
            )
        builder._output_schema_json = schema_json
        return builder

    def build(self, payload: ContextPayload, *, max_input_tokens: int) -> BuiltPrompt:
        try:
            rendered = self._renderer.build(payload, max_input_tokens=max_input_tokens)
        except self._renderer_module.PromptTooLargeError as exc:
            raise PromptTooLargeError(str(exc)) from None
        return _to_prompt(rendered)

    def build_repair(
        self,
        payload: ContextPayload,
        *,
        raw_text: str,
        details: list[str],
        max_input_tokens: int,
    ) -> BuiltPrompt:
        self.ensure_verified()
        try:
            rendered = self._renderer.build_repair(
                payload,
                raw_text=raw_text,
                details=details,
                max_input_tokens=max_input_tokens,
            )
        except self._renderer_module.PromptTooLargeError as exc:
            raise PromptTooLargeError(str(exc)) from None
        return _to_prompt(rendered)


def _to_prompt(rendered: Any) -> BuiltPrompt:
    return BuiltPrompt(
        messages=[
            ChatMessage(role=m.role, content=m.content) for m in rendered.messages
        ],
        prompt_version=rendered.prompt_version,
        estimated_tokens=rendered.estimated_tokens,
        limitations=rendered.limitations,
        few_shot_ids=rendered.few_shot_ids,
        included_rules=getattr(rendered, "included_rules", None),
    )


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PromptConfigError("duplicate manifest key")
        result[key] = value
    return result


def _verify_artifacts(
    root: Path, version: str, entry: dict[str, Any], source_root: Path
) -> dict[str, bytes]:
    # Paths are server-owned; manifest entries cannot select imports or files.
    if not re.fullmatch(r"v[1-9][0-9]*", version):
        raise PromptConfigError("unsupported prompt version")
    if (
        version not in _PROMPT_IDENTITIES
        or entry["renderer_version"] != _PROMPT_IDENTITIES[version][0]
    ):
        raise PromptConfigError("unsupported prompt/renderer identity")
    required = {
        "system": f"{version}/system.md",
        "few_shots": f"{version}/few_shots.json",
        "repair": f"{version}/repair.json",
        "renderer": _RENDERERS[entry["renderer_version"]][1],
        "llm_output_schema": _PROMPT_IDENTITIES[version][1],
        "review_result_schema": (
            f"schemas/review-result/v{_PROMPT_IDENTITIES[version][2]}.json"
        ),
    }
    artifacts = entry["artifacts"]
    if not isinstance(artifacts, list):
        raise PromptConfigError("artifacts must be a list")
    verified: dict[str, bytes] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {
            "role",
            "path",
            "sha256",
        }:
            raise PromptConfigError("invalid artifact entry")
        role, relative, expected = (
            artifact["role"],
            artifact["path"],
            artifact["sha256"],
        )
        if role not in required or role in verified or relative != required[role]:
            raise PromptConfigError("unsupported or duplicate artifact")
        artifact_root = (
            source_root
            if role in {"renderer", "llm_output_schema", "review_result_schema"}
            else root
        )
        path = (artifact_root / relative).resolve()
        if not path.is_relative_to(artifact_root.resolve()):
            raise PromptConfigError("artifact escapes bundle root")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise PromptConfigError(
                f"prompt file {relative} does not match its SHA-256"
            )
        verified[role] = data
    if set(verified) != set(required):
        raise PromptConfigError("missing required artifacts")
    return verified


_SOURCE_ROOT = Path(__file__).resolve().parents[3]
_RENDERERS = {
    "review-renderer-v1": ("app.review.renderers.v1", "src/app/review/renderers/v1.py"),
    "review-renderer-v2": ("app.review.renderers.v2", "src/app/review/renderers/v2.py"),
    "review-renderer-v3": ("app.review.renderers.v3", "src/app/review/renderers/v3.py"),
}


_PROMPT_IDENTITIES: dict[str, tuple[str, str, Literal[1, 2]]] = {
    "v1": ("review-renderer-v1", "schemas/llm-output/v1.json", 1),
    "v2": ("review-renderer-v2", "schemas/llm-output/v1.json", 1),
    "v3": ("review-renderer-v3", "schemas/llm-output/v2.json", 2),
}


def _load_renderer(source: bytes, renderer_version: str) -> ModuleType:
    # The attested closure is deliberately project-import-free. Fail closed if a
    # future change tries to delegate rendering to an unverified project module.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name not in {"json", "re", "posixpath"} for alias in node.names
            ):
                raise PromptConfigError("unverified renderer dependency")
        elif (
            isinstance(node, ast.ImportFrom)
            and (node.module != "typing" or node.level != 0)
        ) or (
            isinstance(node, ast.Name)
            and node.id
            in {
                "__import__",
                "eval",
                "exec",
                "compile",
            }
        ):
            raise PromptConfigError("unverified renderer dependency")
    name, path = _RENDERERS[renderer_version]
    module = ModuleType(name)
    # Defense against indirect import aliases as well as ordinary imports. This
    # is a trusted-source closure guard, not a sandbox for hostile Python code.
    module.__dict__["__builtins__"] = {
        **vars(builtins),
        "__import__": _renderer_import,
    }
    exec(compile(tree, path, "exec"), module.__dict__)
    return module


def _renderer_import(name: str, *args: Any, **kwargs: Any) -> ModuleType:
    if name not in {"json", "re", "posixpath", "typing"}:
        raise PromptConfigError("unverified renderer dependency")
    return importlib.import_module(name)


DELIMITER_TAGS = (
    "repository_instructions",
    "pr_metadata",
    "diff",
    "invalid_answer",
    "repo_rules",
    "findings",
)
_DELIMITER_TAG = re.compile(
    rf"<\s*(/?)\s*({'|'.join(DELIMITER_TAGS)})\b[^>]*>", re.IGNORECASE
)


def neutralize(data: str) -> str:
    """Keep untrusted data from opening or closing review/verifier delimiters."""
    return _DELIMITER_TAG.sub(r"[\1\2]", data)


def delimiter_block_list() -> str:
    """Describe every protected block in verifier trust-boundary instructions."""
    *head, last = DELIMITER_TAGS
    return ", ".join(f"<{tag}>" for tag in head) + f" и <{last}>"


def estimate_messages(messages: Sequence[ChatMessage], counter: TokenCounter) -> int:
    """Content estimate including chat-template framing for every message."""
    return sum(counter.count(message.content) + 4 for message in messages)
