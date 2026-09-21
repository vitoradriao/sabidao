"""Parser Markdown mínimo, consciente de fences, usado pela ingestão e pelo linter."""

from __future__ import annotations

import re
from dataclasses import dataclass


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_SECTION_KEY_RE = re.compile(r"[ \t]+\{#([a-z][a-z0-9]*(?:-[a-z0-9]+)*)\}[ \t]*$")
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_LIST_RE = re.compile(r"^[ \t]{0,3}(?:[-+*]|\d+[.)])[ \t]+")
_TABLE_SEPARATOR_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$"
)


@dataclass(frozen=True)
class MarkdownHeading:
    level: int
    title: str
    line: int
    start: int
    section_key: str | None


@dataclass(frozen=True)
class MarkdownBlock:
    kind: str
    line: int


@dataclass(frozen=True)
class ParsedMarkdown:
    headings: tuple[MarkdownHeading, ...]
    blocks: tuple[MarkdownBlock, ...]
    unclosed_fence_line: int | None


def _closing_fence(line: str, marker: str, length: int) -> bool:
    return bool(re.match(rf"^[ \t]{{0,3}}{re.escape(marker)}{{{length},}}[ \t]*$", line))


def parse_markdown(text: str) -> ParsedMarkdown:
    """Reconhece headings ATX H1-H6 e blocos relevantes sem interpretar fences."""

    headings: list[MarkdownHeading] = []
    blocks: list[MarkdownBlock] = []
    fence: tuple[str, int, int] | None = None
    offset = 0
    lines = text.splitlines(keepends=True)

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        if fence is not None:
            marker, length, _opening_line = fence
            if _closing_fence(line, marker, length):
                fence = None
            offset += len(raw_line)
            continue

        fence_match = _FENCE_OPEN_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            fence = (marker[0], len(marker), line_number)
            blocks.append(MarkdownBlock("fence", line_number))
            offset += len(raw_line)
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            raw_title = heading_match.group(2).strip()
            raw_title = re.sub(r"[ \t]+#+[ \t]*$", "", raw_title).strip()
            key_match = _SECTION_KEY_RE.search(raw_title)
            section_key = key_match.group(1) if key_match else None
            title = raw_title[: key_match.start()].rstrip() if key_match else raw_title
            headings.append(
                MarkdownHeading(
                    level=len(heading_match.group(1)),
                    title=title,
                    line=line_number,
                    start=offset,
                    section_key=section_key,
                )
            )
            blocks.append(MarkdownBlock("heading", line_number))
        elif _LIST_RE.match(line):
            blocks.append(MarkdownBlock("list", line_number))
        elif _TABLE_SEPARATOR_RE.match(line):
            blocks.append(MarkdownBlock("table", line_number))

        offset += len(raw_line)

    if not lines and text:
        # splitlines(keepends=True) só retorna vazio para texto vazio, mas mantém o
        # cálculo explícito para documentar o contrato do scanner.
        offset = len(text)

    return ParsedMarkdown(
        headings=tuple(headings),
        blocks=tuple(blocks),
        unclosed_fence_line=fence[2] if fence is not None else None,
    )


def split_markdown_sections(text: str) -> list[tuple[MarkdownHeading | None, str]]:
    """Divide o texto por headings reais, preservando tabelas, listas e fences."""

    headings = parse_markdown(text).headings
    if not headings:
        return [(None, text)] if text.strip() else []

    sections: list[tuple[MarkdownHeading | None, str]] = []
    preamble = text[: headings[0].start]
    if preamble.strip():
        sections.append((None, preamble.strip()))

    for index, heading in enumerate(headings):
        end = headings[index + 1].start if index + 1 < len(headings) else len(text)
        content = text[heading.start:end].strip()
        if content:
            sections.append((heading, content))
    return sections
