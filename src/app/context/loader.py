"""Root rules loader with immutable source provenance."""

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from app.application.github_ports import (
    RepositoryFile,
    RepositoryFileReader,
    RepositoryFileRejected,
)
from app.context.rules import MAX_FILE_BYTES, PARSER_VERSION, ParsedRules, parse_rules
from app.review.validation import rule_refs_from_text

MAX_CACHED_FILES = 128


@dataclass(frozen=True, slots=True)
class LoadedRules:
    rules: ParsedRules
    provenance: dict[str, Any]


class RepoConfigLoader:
    def __init__(
        self,
        reader: RepositoryFileReader,
        cache: OrderedDict[tuple[str, ...], RepositoryFile] | None = None,
    ) -> None:
        self._reader = reader
        self._cache = cache if cache is not None else OrderedDict()

    async def load(  # noqa: PLR0913 -- scoped immutable file identity and DB instructions
        self,
        *,
        tenant: str,
        owner: str,
        name: str,
        external_id: str,
        revision: str,
        instructions: tuple[str, ...],
    ) -> LoadedRules:
        key = (
            tenant,
            external_id,
            owner,
            name,
            revision,
            ".review/rules.md",
            PARSER_VERSION,
        )
        rejected: RepositoryFileRejected | None = None
        file: RepositoryFile | None
        if key in self._cache:
            file = self._cache[key]
            await self._reader.authorize_revision(owner, name, external_id, revision)
            if key in self._cache:
                self._cache.move_to_end(key)
        else:
            try:
                file = await self._reader.read_file(
                    owner,
                    name,
                    external_id,
                    revision,
                    ".review/rules.md",
                    max_bytes=MAX_FILE_BYTES,
                )
            except RepositoryFileRejected as failure:
                rejected, file = failure, None
            if file is not None:
                self._cache[key] = file
                if len(self._cache) > MAX_CACHED_FILES:
                    self._cache.popitem(last=False)
        rules = (
            parse_rules(file.text, instructions)
            if file is not None
            else ParsedRules(instructions, "absent")
        )
        if rejected is not None:
            rules = ParsedRules(instructions, "invalid", (f"{rejected.diagnostic}:0",))
        return LoadedRules(
            rules,
            {
                "parser_version": PARSER_VERSION,
                "path": ".review/rules.md",
                "tenant": tenant,
                "repository_external_id": external_id,
                "revision": revision,
                "blob_sha": file.blob_sha
                if file
                else rejected.blob_sha
                if rejected
                else None,
                "content_digest": hashlib.sha256(file.text.encode()).hexdigest()
                if file
                else None,
                "state": rules.state,
                "diagnostics": list(rules.diagnostics),
                "severities": dict(rules.severities),
                "allowed_rule_refs": sorted(
                    rule_refs_from_text("\n".join(instructions))
                    | rule_refs_from_text(
                        "\n".join(
                            rule.splitlines()[0]
                            for rule in rules.instructions[len(instructions) :]
                        )
                    )
                ),
            },
        )
