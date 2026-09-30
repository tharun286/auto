from pathlib import Path
import re
import unicodedata
from docx import Document
from docx.document import Document as DocumentType
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
try:
    import pymupdf as fitz
except ImportError:  # older installs
    import fitz
import os
from collections import Counter
# =====================================================================
# COMMON HELPERS
# =====================================================================
def clean_powerpoint_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    invisible_characters = [
        "\u200b",
        "\u200c",
        "\u200d",
        "\u200e",
        "\u200f",
        "\u2060",
        "\ufeff",
        "\u00ad",
    ]
    for character in invisible_characters:
        text = text.replace(character, "")
    text = text.replace("\u00a0", " ")
    return text
# =====================================================================
# DOCX HELPERS
# =====================================================================
def iter_block_items(parent):

    if isinstance(parent, DocumentType):
        parent_element = parent.element.body

    elif isinstance(parent, _Cell):
        parent_element = parent._tc

    else:
        parent_element = parent._element

    for child in parent_element.iterchildren():

        if child.tag.endswith("}p"):
            yield Paragraph(child, parent)

        elif child.tag.endswith("}tbl"):
            yield Table(child, parent)


def iter_paragraphs(parent):

    for block in iter_block_items(parent):

        if isinstance(block, Paragraph):
            yield block

        elif isinstance(block, Table):

            processed_cells = set()

            for row in block.rows:
                for cell in row.cells:

                    cell_id = id(cell._tc)

                    if cell_id in processed_cells:
                        continue

                    processed_cells.add(cell_id)

                    yield from iter_paragraphs(cell)
def _find_claim_spans(full_text, old_text):
    """
    All (start, end) spans of old_text in full_text. Exact match first; if none,
    a whitespace-tolerant match (the agent collapses runs of spaces/tabs/newlines
    when it reads the document, so the stored text may differ only in spacing).
    """
    if not old_text:
        return []

    spans = []
    position = 0
    while True:
        start = full_text.find(old_text, position)
        if start == -1:
            break
        spans.append((start, start + len(old_text)))
        position = start + len(old_text)

    if spans:
        return spans

    tokens = old_text.split()
    if not tokens:
        return []
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    return [(m.start(), m.end()) for m in re.finditer(pattern, full_text)]


def replace_text_in_word_paragraph(
    paragraph,
    old_text,
    new_text,
):

    runs = paragraph.runs

    if not runs:
        return 0

    full_text = "".join(run.text for run in runs)

    match_spans = _find_claim_spans(full_text, old_text)

    if not match_spans:
        return 0

    replacement_count = 0

    for match_start, match_end in reversed(match_spans):

        run_spans = []

        current_position = 0

        for run in runs:

            start = current_position
            end = start + len(run.text)

            run_spans.append({
                "run": run,
                "start": start,
                "end": end,
            })

            current_position = end

        affected_runs = [
            item
            for item in run_spans
            if item["start"] < match_end
            and item["end"] > match_start
        ]

        if not affected_runs:
            continue

        first_item = affected_runs[0]
        last_item = affected_runs[-1]

        first_run = first_item["run"]
        last_run = last_item["run"]

        start_inside_first = (
            match_start - first_item["start"]
        )

        end_inside_last = (
            match_end - last_item["start"]
        )

        text_before = first_run.text[:start_inside_first]
        text_after = last_run.text[end_inside_last:]

        if first_run is last_run:

            first_run.text = (
                text_before
                + new_text
                + text_after
            )

        else:

            first_run.text = (
                text_before + new_text
            )

            for item in affected_runs[1:-1]:
                item["run"].text = ""

            last_run.text = text_after

        replacement_count += 1

    return replacement_count


def replace_in_word_document(
    document,
    old_text,
    new_text,
):

    total_replacements = 0

    for paragraph in iter_paragraphs(document):

        total_replacements += (
            replace_text_in_word_paragraph(
                paragraph,
                old_text,
                new_text,
            )
        )

    processed_parts = set()

    for section in document.sections:

        header_footer_parts = [
            section.header,
            section.first_page_header,
            section.even_page_header,
            section.footer,
            section.first_page_footer,
            section.even_page_footer,
        ]

        for part in header_footer_parts:

            part_id = str(part.part.partname)

            if part_id in processed_parts:
                continue

            processed_parts.add(part_id)

            for paragraph in iter_paragraphs(part):

                total_replacements += (
                    replace_text_in_word_paragraph(
                        paragraph,
                        old_text,
                        new_text,
                    )
                )

    return total_replacements


# =====================================================================
# PPTX HELPERS
# =====================================================================

def replace_text_in_ppt_paragraph(
    paragraph,
    old_text,
    new_text,
):

    runs = list(paragraph.runs)

    if not runs:
        return 0

    full_text = "".join(
        run.text for run in runs
    )

    search_text = old_text

    if search_text not in full_text:

        cleaned_full = clean_powerpoint_text(
            full_text
        )

        cleaned_old = clean_powerpoint_text(
            old_text
        )

        if (
            cleaned_old in cleaned_full
            and len(cleaned_old) <= len(cleaned_full)
        ):
            full_text = cleaned_full
            search_text = cleaned_old
        else:
            return 0

    match_positions = []

    search_position = 0

    while True:

        match_start = full_text.find(
            search_text,
            search_position,
        )

        if match_start == -1:
            break

        match_positions.append(match_start)

        search_position = (
            match_start + len(search_text)
        )

    replacement_count = 0

    for match_start in reversed(match_positions):

        match_end = (
            match_start + len(search_text)
        )

        run_spans = []
        current_position = 0

        for run in runs:

            start = current_position
            end = start + len(run.text)

            run_spans.append({
                "run": run,
                "start": start,
                "end": end,
            })

            current_position = end

        affected_runs = [
            item
            for item in run_spans
            if item["start"] < match_end
            and item["end"] > match_start
        ]

        if not affected_runs:
            continue

        first_item = affected_runs[0]
        last_item = affected_runs[-1]

        first_run = first_item["run"]
        last_run = last_item["run"]

        start_inside_first = (
            match_start - first_item["start"]
        )

        end_inside_last = (
            match_end - last_item["start"]
        )

        text_before = first_run.text[:start_inside_first]
        text_after = last_run.text[end_inside_last:]

        if first_run is last_run:

            first_run.text = (
                text_before
                + new_text
                + text_after
            )

        else:

            first_run.text = (
                text_before + new_text
            )

            for item in affected_runs[1:-1]:
                item["run"].text = ""

            last_run.text = text_after

        replacement_count += 1

    return replacement_count


def replace_in_text_frame(
    text_frame,
    old_text,
    new_text,
):

    total = 0

    for paragraph in text_frame.paragraphs:

        total += replace_text_in_ppt_paragraph(
            paragraph,
            old_text,
            new_text,
        )

    return total


def replace_in_table(
    table,
    old_text,
    new_text,
):

    total = 0
    processed_cells = set()

    for row in table.rows:
        for cell in row.cells:

            cell_id = id(cell._tc)

            if cell_id in processed_cells:
                continue

            processed_cells.add(cell_id)

            total += replace_in_text_frame(
                cell.text_frame,
                old_text,
                new_text,
            )

    return total


def replace_in_shape(
    shape,
    old_text,
    new_text,
):

    total = 0

    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:

        for child in shape.shapes:

            total += replace_in_shape(
                child,
                old_text,
                new_text,
            )

        return total

    if getattr(shape, "has_table", False):

        return replace_in_table(
            shape.table,
            old_text,
            new_text,
        )

    if getattr(shape, "has_text_frame", False):

        total += replace_in_text_frame(
            shape.text_frame,
            old_text,
            new_text,
        )

    return total


# =====================================================================
# PUBLIC FUNCTIONS USED BY AUTO UPDATE AGENT
# =====================================================================

def update_docx_claim(
    file_path: str,
    old_claim: str,
    new_claim: str,
) -> int:

    document = Document(file_path)

    replacements = replace_in_word_document(
        document,
        old_claim,
        new_claim,
    )
    if replacements > 0:
        document.save(file_path)

    return replacements
# =====================================================================
# PDF HELPERS  (same font, same size, same position)
# =====================================================================
PDF_MIN_FONT_SCALE = 0.90   # only used if the text cannot fit even after growing downward
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_BASE14 = {
    ("sans", False, False): "helv", ("sans", True, False): "hebo",
    ("sans", False, True): "heit", ("sans", True, True): "hebi",
    ("serif", False, False): "tiro", ("serif", True, False): "tibo",
    ("serif", False, True): "tiit", ("serif", True, True): "tibi",
    ("mono", False, False): "cour", ("mono", True, False): "cobo",
    ("mono", False, True): "coit", ("mono", True, True): "cobi",
}


def _alnum(text: str) -> str:
    return re.sub(r"\W+", "", (text or "").lower())


def _base14_name(font_name: str, flags: int) -> str:
    name = (font_name or "").lower()
    if flags & 8 or any(k in name for k in ("courier", "mono", "consol")):
        family = "mono"
    elif flags & 4 or any(
        k in name for k in ("times", "serif", "georgia", "garamond", "palatino",
                            "minion", "cambria", "bookman", "century")
    ) and "sans" not in name:
        family = "serif"
    else:
        family = "sans"
    bold = bool(flags & 16) or any(k in name for k in ("bold", "black", "heavy", "semibold"))
    italic = bool(flags & 2) or any(k in name for k in ("italic", "oblique"))
    return _BASE14[(family, bold, italic)]


def _pdf_original_lines(page, rect):
    """Lines (with their styled spans) inside rect, in reading order."""
    lines = []
    for block in page.get_text("dict", clip=rect).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [sp for sp in line.get("spans", []) if sp.get("text", "").strip()]
            if spans:
                lines.append({"bbox": fitz.Rect(line["bbox"]), "dir": line.get("dir", (1, 0)),
                              "spans": spans})
    return lines


def _pdf_dominant_style(lines):
    weight = Counter()
    sample = {}
    for line in lines:
        for sp in line["spans"]:
            key = (sp["font"], round(float(sp["size"]), 2), int(sp.get("color", 0)), int(sp.get("flags", 0)))
            weight[key] += len(sp["text"].strip())
            sample.setdefault(key, sp)
    key = weight.most_common(1)[0][0]
    return {"font": key[0], "size": key[1], "color": key[2], "flags": key[3]}


def _pdf_resolve_font(document, page, style, text):
    """
    Returns (fontname_for_insert, measuring_font). Prefers the font that is
    already embedded in the PDF (true same font) when it contains every glyph
    needed; otherwise the closest standard font.
    """
    needed = {ord(c) for c in text if not c.isspace()}
    target = _SUBSET_PREFIX.sub("", style["font"])
    try:
        for entry in page.get_fonts(full=True):
            xref, ext, _type, basefont = entry[0], entry[1], entry[2], entry[3]
            if ext in ("n/a", "") or xref <= 0:
                continue
            if _SUBSET_PREFIX.sub("", basefont) != target:
                continue
            _n, _e, _t, buffer = document.extract_font(xref)
            if not buffer:
                continue
            measure = fitz.Font(fontbuffer=buffer)
            if all(measure.has_glyph(cp) for cp in needed):
                fontname = "CLM" + re.sub(r"\W", "", target)[:20]
                page.insert_font(fontname=fontname, fontbuffer=buffer)
                print(f"PDF FONT: reusing embedded font {style['font']}")
                return fontname, measure
            print(f"PDF FONT: embedded {style['font']} lacks needed glyphs, using closest standard font")
    except Exception as e:
        print(f"PDF FONT: could not reuse embedded font ({e})")

    base = _base14_name(style["font"], style["flags"])
    print(f"PDF FONT: {style['font']} -> standard font '{base}'")
    return base, fitz.Font(base)


def _pdf_wrap(font, text, size, width):
    lines = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if font.text_length(trial, fontsize=size) <= width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _pdf_free_bottom(page, rect):
    """How far down the text may grow without touching other text."""
    limit = page.rect.y1 - 18
    for block in page.get_text("blocks"):
        bx0, by0, bx1, by1 = block[:4]
        if by0 >= rect.y1 - 1 and bx1 > rect.x0 and bx0 < rect.x1:
            limit = min(limit, by0 - 2)
    return limit


def update_pdf_claim(
    file_path: str,
    page_number: int,
    rect,
    old_claim: str,
    new_claim: str,
) -> int:
    """
    Replace the text inside `rect` on `page_number` with `new_claim`, keeping the
    original font, size, colour, line spacing and position. Edits `file_path`
    IN PLACE (via a temp file) and returns 1 on success, -1 on failure. On any
    failure the original file is left untouched.
    """
    document = None
    tmp_path = file_path + ".claim_tmp.pdf"
    try:
        new_text = re.sub(r"\s+", " ", (new_claim or "")).strip()
        if not new_text:
            print("❌ PDF UPDATE ABORTED: new text is empty")
            return -1

        document = fitz.open(file_path)
        if document.needs_pass:
            print("❌ PDF UPDATE ABORTED: file is password protected")
            return -1
        page = document[page_number]
        if page.rotation != 0:
            print("❌ PDF UPDATE ABORTED: rotated pages are not supported")
            return -1

        rect = fitz.Rect(rect)
        lines = _pdf_original_lines(page, rect)
        if not lines:
            print("❌ PDF UPDATE ABORTED: no text found in the target area")
            return -1
        if any(abs(l["dir"][0] - 1) > 0.01 or abs(l["dir"][1]) > 0.01 for l in lines):
            print("❌ PDF UPDATE ABORTED: rotated/vertical text is not supported")
            return -1

        # Safety: the area must really contain the old claim (prevents editing the wrong place)
        found = _alnum(" ".join(sp["text"] for l in lines for sp in l["spans"]))
        wanted = _alnum(old_claim)
        if wanted and difflib_ratio(wanted, found) < 0.75:
            print("❌ PDF UPDATE ABORTED: target area does not contain the old claim text")
            return -1

        style = _pdf_dominant_style(lines)
        orig_size = float(style["size"])
        fontname, measure = _pdf_resolve_font(document, page, style, new_text)

        first_line = lines[0]
        first_span = first_line["spans"][0]
        base_x = float(first_span["origin"][0])
        base_y = float(first_span["origin"][1])
        next_x = lines[1]["bbox"].x0 if len(lines) > 1 else first_line["bbox"].x0
        if len(lines) > 1:
            line_gap = float(lines[1]["spans"][0]["origin"][1]) - base_y
            if line_gap <= 0:
                line_gap = orig_size * 1.2
        else:
            line_gap = orig_size * (measure.ascender - measure.descender)

        # alignment, from the original block
        align = "left"
        if len(lines) > 1:
            lefts = [l["bbox"].x0 for l in lines]
            rights = [l["bbox"].x1 for l in lines]
            centres = [(l["bbox"].x0 + l["bbox"].x1) / 2 for l in lines]
            if max(lefts) - min(lefts) > 2:
                if max(centres) - min(centres) <= 2:
                    align = "center"
                elif max(rights) - min(rights) <= 2:
                    align = "right"

        width = rect.width + 2
        bottom_limit = max(rect.y1, _pdf_free_bottom(page, rect))

        chosen = None
        scale = 1.0
        while scale >= PDF_MIN_FONT_SCALE - 1e-9:
            size = orig_size * scale
            gap = line_gap * scale
            wrapped = _pdf_wrap(measure, new_text, size, width - (base_x - rect.x0 if len(lines) == 1 else 0))
            last_baseline = base_y + gap * (len(wrapped) - 1)
            descent = size * abs(measure.descender)
            if last_baseline + descent <= bottom_limit + 0.5:
                chosen = (size, gap, wrapped)
                break
            scale -= 0.025
        if not chosen:
            print("❌ PDF UPDATE ABORTED: new text does not fit in the available space "
                  f"at {int(PDF_MIN_FONT_SCALE * 100)}%+ of the original size")
            return -1
        size, gap, wrapped = chosen
        if size < orig_size - 1e-6:
            print(f"⚠️ PDF font reduced {orig_size:.1f} -> {size:.2f} pt to fit")

        # ---- edit: remove old text only (no background fill), then write new text ----
        c = int(style["color"])
        color = (((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)
        inset = fitz.Rect(rect.x0 + 0.5, rect.y0 + 0.5, rect.x1 - 0.5, rect.y1 - 0.5)
        try:
            page.add_redact_annot(inset, fill=False)
        except Exception:
            page.add_redact_annot(inset, fill=(1, 1, 1))
        try:
            page.apply_redactions(images=0, graphics=0)
        except TypeError:
            try:
                page.apply_redactions(images=0)
            except TypeError:
                page.apply_redactions()

        for index, text_line in enumerate(wrapped):
            line_width = measure.text_length(text_line, fontsize=size)
            left = base_x if index == 0 else next_x
            if align == "center":
                x = (rect.x0 + rect.x1) / 2 - line_width / 2
            elif align == "right":
                x = rect.x1 - line_width
            else:
                x = left
            page.insert_text(
                fitz.Point(x, base_y + gap * index),
                text_line,
                fontname=fontname,
                fontsize=size,
                color=color,
            )

        document.save(tmp_path, garbage=0, deflate=True)
        document.close()
        document = None

        # verify the saved copy BEFORE it replaces the original
        with fitz.open(tmp_path) as check:
            check_text = _alnum(check[page_number].get_text())
        if _alnum(new_text) not in check_text:
            print("❌ PDF UPDATE ABORTED: new text not found after saving (original untouched)")
            return -1

        os.replace(tmp_path, file_path)
        print(f"✅ PDF UPDATED IN PLACE: {file_path}")
        return 1
    except Exception as e:
        print(f"❌ PDF UPDATE FAILED: {e}")
        return -1
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def difflib_ratio(a: str, b: str) -> float:
    """1.0 if one text contains the other, else difflib similarity."""
    from difflib import SequenceMatcher

    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def update_pptx_claim(
    file_path: str,
    old_claim: str,
    new_claim: str,
) -> int:

    presentation = Presentation(file_path)

    replacements = 0

    for slide in presentation.slides:

        for shape in slide.shapes:

            replacements += replace_in_shape(
                shape,
                old_claim,
                new_claim,
            )

    if replacements > 0:
        presentation.save(file_path)

    return replacements
