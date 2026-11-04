"""Bounded Markdown blocks with immutable character spans for evidence."""
import re


_HEADING = re.compile(r" {0,3}(?P<marks>#{1,6})[ \t]+(?P<title>\S.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r" {0,3}(?P<marker>`{3,}|~{3,})")
_PIPE = re.compile(r"(?<!\\)\|")
_ALIGN = re.compile(r":?-{3,}:?")
_MAX_TEXT = 12000


def _table_cells(line):
    pipes = list(_PIPE.finditer(line))
    if not pipes:
        return []
    edges = [0, *(match.end() for match in pipes), len(line) + 1]
    spans = [(start, stop - 1) for start, stop in zip(edges, edges[1:])]
    if not line[:pipes[0].start()].strip():
        spans = spans[1:]
    if not line[pipes[-1].end():].strip():
        spans = spans[:-1]
    return spans


def markdown_locators(text):
    if len(text) > _MAX_TEXT:
        return []
    lines, cursor = [], 0
    for physical in text.splitlines(keepends=True):
        line = physical.rstrip("\r\n")
        lines.append((cursor, line))
        cursor += len(physical)

    def span(start, end, kind, **metadata):
        return {"id": f"md:{start}:{end}", "kind": kind, "text": text[start:end],
                "start": start, "end": end, **metadata}

    result = []
    pending = []

    def flush():
        if pending:
            start, _ = pending[0]
            end, last = pending[-1]
            result.append(span(start, end + len(last), "paragraph"))
            pending.clear()

    index, tables = 0, 0
    while index < len(lines):
        start, line = lines[index]
        if not line.strip():
            flush()
        elif fence := _FENCE.match(line):
            flush()
            pending.append(lines[index])
            marker = fence["marker"]
            closing = re.compile(r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*$")
            while index + 1 < len(lines):
                index += 1
                pending.append(lines[index])
                if closing.fullmatch(lines[index][1]):
                    break
            flush()
        elif heading := _HEADING.fullmatch(line):
            flush()
            left, right = heading.span("title")
            result.append(span(start + left, start + right, "paragraph", section_label=True,
                               heading_level=len(heading["marks"])))
        else:
            cells = _table_cells(line)
            divider = lines[index + 1][1] if index + 1 < len(lines) else ""
            alignment = _table_cells(divider)
            if (len(cells) >= 2 and len(cells) == len(alignment)
                    and all(_ALIGN.fullmatch(divider[left:right].strip()) for left, right in alignment)):
                flush()
                tables += 1
                row = 0
                while cells and len(cells) == len(alignment):
                    row += 1
                    for column, (left, right) in enumerate(cells, 1):
                        value = line[left:right]
                        left += len(value) - len(value.lstrip())
                        right = max(left, right - (len(value) - len(value.rstrip())))
                        result.append(span(start + left, start + right, "table_cell", table_id=tables, row=row,
                                           column=column, grid_column=column, column_span=1, vertical_merge=None))
                    result.append(span(start, start + len(line), "table_row", table_id=tables, row=row))
                    index += 2 if row == 1 else 1  # The alignment line carries no values.
                    if index >= len(lines):
                        break
                    start, line = lines[index]
                    cells = [] if _HEADING.fullmatch(line) or _FENCE.match(line) else _table_cells(line)
                continue
            pending.append(lines[index])
        index += 1
    flush()
    return result
