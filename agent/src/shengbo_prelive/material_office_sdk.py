"""Small SDK adapters; container budgets and source coordinates live in parsers.

No Office file is saved here. XLSX uses a lightweight openpyxl style context,
not load_workbook(), so media-heavy and sparse workbooks stay bounded by the
actual source cells rather than the declared rectangular worksheet dimension.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from importlib.metadata import version
from io import BytesIO
import re
from types import SimpleNamespace


def library(name):
    return {"name": name, "version": version(name)}


def docx_document(data):
    from docx import Document
    return Document(BytesIO(data))


def docx_part(data):
    from docx.oxml import parse_xml
    return parse_xml(data)


def docx_paragraph(element, document, source_text):
    """SDK reads ordinary paragraphs; XML supplements unsupported story nodes."""
    from docx.text.paragraph import Paragraph
    text = Paragraph(element, document).text
    if text == source_text:
        return text, "python-docx"
    # Text boxes, content controls, revision wrappers and simple fields are not
    # all exposed by Paragraph.text. Keep their full source order when present.
    return source_text, "python-docx+ooxml_special_content"


def docx_cell(element, document, source_text):
    from docx.table import _Cell
    text = _Cell(element, document).text
    if text == source_text:
        return text, "python-docx"
    return source_text, "python-docx+ooxml_nested_or_special_content"


def rtf_visible_text(data, encoding):
    from striprtf.striprtf import rtf_to_text
    source = _rtf_sdk_source(data)
    # striprtf intentionally omits these Word story destinations. Their visible
    # text was supported by our source reader and is retained as ordinary groups.
    source = re.sub(r"\\(?:header[flr]?|footer[flr]?|footnote|shptxt)\b", "", source)
    # List labels and style tables must not turn into business facts. Do not
    # mutate striprtf's module-global destinations in a concurrent server.
    source = re.sub(r"\\(?:listtext|xmlnstbl)\b", lambda m: "\\*" + m.group(), source)
    # ANSI bytes can occur literally as well as in RTF hex escapes. Passing
    # Latin-1 text directly would bypass the SDK's per-font codepage decoding.
    source = re.sub(r"[\x80-\xff]", lambda m: "\\'" + format(ord(m.group()), "02x"), source)
    text = rtf_to_text(source, encoding=encoding, errors="replace")
    text = text.replace("\x1e", "\u2011").replace("\x1f", "\u00ad")
    return text.encode("utf-16", errors="surrogatepass").decode("utf-16", errors="replace")


def _rtf_sdk_source(data):
    """Mask binary payloads before passing a validated RTF to the text SDK.

    Their lengths and arbitrary brace/backslash bytes must never be interpreted
    as text syntax. Pictures are independently indexed from original bytes.
    """
    source = data.decode("latin1")
    pattern = re.compile(r"\\(?:[A-Za-z]+)(?:-?\d+)? ?|\\[^A-Za-z]")
    output, offset = [], 0
    while True:
        match = pattern.search(source, offset)
        if match is None:
            output.append(source[offset:])
            break
        output.append(source[offset:match.start()])
        binary = re.fullmatch(r"\\bin(\d+) ?", match.group())
        if binary:
            # Validation already rejected negative and out-of-bounds counts.
            offset = match.end() + int(binary.group(1))
        else:
            output.append(match.group())
            offset = match.end()
    return "".join(output)


class XlsxValueReader:
    """Reuse openpyxl's style, shared-string and Excel date decoding in sparse IO."""

    def __init__(self, archive, *, date1904=False):
        from openpyxl import Workbook
        from openpyxl.styles.stylesheet import apply_stylesheet
        from openpyxl.utils.datetime import CALENDAR_MAC_1904
        self.workbook = Workbook()
        if date1904:
            self.workbook.epoch = CALENDAR_MAC_1904
        apply_stylesheet(archive, self.workbook)
        self.sheet = SimpleNamespace(parent=self.workbook)

    @staticmethod
    def shared_strings(data):
        from openpyxl.reader.strings import read_string_table
        return read_string_table(BytesIO(data))

    def cell(self, raw, source_type, style_id):
        from openpyxl.cell.read_only import ReadOnlyCell
        from openpyxl.styles.numbers import is_date_format, is_timedelta_format
        from openpyxl.utils.datetime import from_excel, from_ISO8601
        try:
            style = int(style_id or 0)
        except (TypeError, ValueError):
            style = -1
        if style < 0 or style >= len(self.workbook._cell_styles):
            return {"raw_value": raw, "number_format": None, "display_text": raw or "",
                    "value_interpretation": "invalid_style_preserved_raw", "format_supported": False}
        sdk_cell = ReadOnlyCell(self.sheet, 1, 1, raw, data_type=source_type or "n", style_id=style)
        fmt = sdk_cell.number_format
        result = {"raw_value": raw, "number_format": fmt, "display_text": raw or "",
                  "value_interpretation": "raw_source_no_recalculation", "format_supported": True}
        if raw is None:
            return result
        if source_type == "b":
            result.update(display_text="TRUE" if raw == "1" else "FALSE", decoded_value=raw == "1",
                          value_interpretation="openpyxl_boolean")
            return result
        if source_type == "d":
            try:
                value = from_ISO8601(raw)
                rendered = str(value) if isinstance(value, timedelta) else value.isoformat()
                result.update(display_text=rendered, decoded_value=rendered,
                              value_interpretation="openpyxl_iso_date_normalized")
            except (TypeError, ValueError, AttributeError):
                result.update(format_supported=False, value_interpretation="invalid_date_preserved_raw")
            return result
        if source_type not in {None, "n"}:
            return result
        try:
            number = Decimal(raw)
            if not number.is_finite():
                raise InvalidOperation
            if is_date_format(fmt):
                value = from_excel(float(number), self.workbook.epoch, timedelta=is_timedelta_format(fmt))
                rendered = str(value) if isinstance(value, timedelta) else value.isoformat()
                result.update(display_text=rendered, decoded_value=rendered, value_interpretation="openpyxl_excel_date_normalized",
                              date_system="1904" if self.workbook.epoch.year == 1904 else "1900")
                return result
            rendered, interpretation = _number_display(number, fmt)
            if rendered is not None:
                result.update(display_text=rendered, value_interpretation=interpretation)
            elif fmt not in {"General", "@"}:
                result.update(format_supported=False, value_interpretation="unsupported_number_format_preserved_raw")
        except (InvalidOperation, ValueError, TypeError, OverflowError):
            result.update(format_supported=False, value_interpretation="numeric_decode_failed_preserved_raw")
        return result


def _number_format_sections(fmt):
    """Keep quoted/escaped literals distinct from percent and section controls.

    This only recognizes the lexical boundary needed to decline unsupported
    formats. It is deliberately not a complete Excel number-format renderer.
    """
    sections, start, quoted, escaped = [], 0, False, False
    literal_percent, percent_controls = False, 0
    for index, char in enumerate(fmt):
        if escaped:
            literal_percent |= char == "%"
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == "%":
            if quoted:
                literal_percent = True
            else:
                percent_controls += 1
        elif char == ";" and not quoted:
            sections.append((fmt[start:index], literal_percent, percent_controls))
            start, literal_percent, percent_controls = index + 1, False, 0
    if quoted or escaped:
        return None
    sections.append((fmt[start:], literal_percent, percent_controls))
    return sections


def _number_display(number, fmt):
    """Conservative common-format display, never a claim of Excel rendering.

    openpyxl supplies number-format definitions and date classification, but
    does not render arbitrary Excel formats. Unsupported condition/fraction/
    scaling formats keep source values and explicitly report the boundary.
    """
    if fmt in {"General", "@"}:
        return None, None
    sections = _number_format_sections(fmt)
    # Zero/text sections and quoted/escaped percent signs need more rendering
    # semantics than this common-format adapter promises. Preserve raw values.
    if sections is None or len(sections) > 2:
        return None, None
    section, literal_percent, percent_controls = sections[1] if number < 0 and len(sections) > 1 else sections[0]
    if literal_percent or percent_controls > 1:
        return None, None
    if re.search(r"\[(?!\$)[^\]]+\]|[Ee][+-]|/|[?#]?,\s*$", section):
        return None, None
    section = re.sub(r"\[\$([^\]-]*)[^\]]*\]", r"\1", section)
    section = re.sub(r"_(.)|\*(.)", "", section)
    section = section.replace('"', "").replace("\\", "")
    numeric = re.search(r"[0#,]+(?:\.[0#]+)?", section)
    if not numeric:
        return None, None
    prefix, suffix = section[:numeric.start()], section[numeric.end():]
    if any(char in prefix + suffix for char in "0123456789#?"):
        return None, None
    percent = "%" in suffix
    if percent_controls != int(percent):
        return None, None  # e.g. percent controls before the numeric pattern
    pattern = numeric.group()
    if pattern.endswith(","):
        return None, None
    decimals = len(pattern.partition(".")[2])
    if decimals > 12 or len(number.as_tuple().digits) > 100 or abs(number.adjusted()) > 100:
        return None, None
    value = abs(number) if number < 0 and len(sections) > 1 else number
    value *= 100 if percent else 1
    rendered = format(value, ("," if "," in pattern else "") + f".{decimals}f")
    if "." in pattern and pattern.endswith("#"):
        mandatory = len(pattern.partition(".")[2].rstrip("#"))
        whole, dot, fraction = rendered.partition(".")
        fraction = fraction.rstrip("0")
        fraction += "0" * max(0, mandatory - len(fraction))
        rendered = whole + (dot + fraction if fraction else "")
    return prefix + rendered + suffix, "common_excel_percent_display" if percent else "common_excel_numeric_display"
