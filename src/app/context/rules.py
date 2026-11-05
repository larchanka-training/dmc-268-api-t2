"""Selected S3 root Markdown profile; repository content is data, never policy."""

import re
from dataclasses import dataclass, field

from app.domain.frozen_configuration import MAX_REPOSITORY_INSTRUCTION_CHARS

PARSER_VERSION = "rules-md/1"
MAX_FILE_BYTES = 65536
_ENTRY = re.compile(r"## ([A-Z][A-Z0-9]{1,9}-[0-9]{1,4}): (\S[^\n]*)$")


@dataclass(frozen=True, slots=True)
class ParsedRules:
    instructions: tuple[str, ...]
    state: str
    diagnostics: tuple[str, ...] = ()
    severities: tuple[tuple[str, str], ...] = ()


@dataclass
class _Markdown:
    seen: set[str]
    instructions: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    severities: dict[str, str] = field(default_factory=dict)
    ignored: set[str] = field(default_factory=set)
    fence: str | None = None
    section: str = ""
    reference: str = ""
    title: str = ""
    body: list[str] = field(default_factory=list)
    invalid: str | None = None

    def finish(self) -> None:
        if self.reference:
            self.instructions.append(
                f"[{self.reference}] {self.section}: {self.title}\n"
                + "\n".join(self.body).strip()
            )

    def consume(self, number: int, line: str) -> None:
        if line.startswith(("```", "~~~")):
            self.fence = None if self.fence == line[:3] else line[:3]
            if self.reference:
                self.body.append(line)
        elif self.fence:
            if self.reference:
                self.body.append(line)
        elif line.startswith("# "):
            self.finish()
            self.reference, self.body = "", []
            self.section = line[2:]
            if self.section not in {"Security", "Performance", "Style", "Ignore"}:
                self.invalid = f"invalid_structure:{number}"
        elif match := _ENTRY.fullmatch(line):
            self.entry(number, match)
        elif self.section == "Ignore" and line.strip():
            if re.fullmatch(r"- [A-Z][A-Z0-9]{1,9}-[0-9]{1,4}", line):
                self.ignored.add(line[2:])
            else:
                self.diagnostics.append(f"invalid_ignore:{number}")
        elif self.reference:
            self.criterion_line(number, line)

    def entry(self, number: int, match: re.Match[str]) -> None:
        self.finish()
        self.reference, self.title = match.groups()
        if self.reference in self.seen:
            self.diagnostics.append(f"duplicate_ref:{number}")
            self.reference = ""
        else:
            self.seen.add(self.reference)
        self.body = []

    def criterion_line(self, number: int, line: str) -> None:
        if line.startswith("- severity: ") and line[12:] in {
            "critical",
            "high",
            "medium",
            "low",
        }:
            self.severities[self.reference] = line[12:]
        elif line.startswith("- severity:"):
            self.diagnostics.append(f"invalid_severity:{number}")
            self.severities.pop(self.reference, None)
            self.reference, self.body = "", []
        else:
            self.body.append(line)


def parse_rules(text: str, db_instructions: tuple[str, ...] = ()) -> ParsedRules:
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        return ParsedRules(db_instructions, "invalid", ("file_too_large:0",))
    if not text.strip():
        return ParsedRules(db_instructions, "empty")
    markdown = _Markdown(
        seen={
            m.group(1)
            for instruction in db_instructions
            if (m := re.match(r"^\[([^]]+)\]", instruction))
        }
    )
    for number, line in enumerate(text.splitlines(), 1):
        markdown.consume(number, line)
        if markdown.invalid:
            return ParsedRules(db_instructions, "invalid", (markdown.invalid,))
    if markdown.fence:
        return ParsedRules(db_instructions, "invalid", ("unclosed_fence:0",))
    markdown.finish()
    selected = [
        instruction
        for instruction in markdown.instructions
        if instruction.split("]", 1)[0][1:] not in markdown.ignored
    ]
    if sum(map(len, (*db_instructions, *selected))) > MAX_REPOSITORY_INSTRUCTION_CHARS:
        return ParsedRules(db_instructions, "invalid", ("combined_rule_limit:0",))
    return ParsedRules(
        (*db_instructions, *selected),
        "valid",
        tuple(markdown.diagnostics),
        tuple(
            (ref, severity)
            for ref, severity in markdown.severities.items()
            if ref not in markdown.ignored
        ),
    )
