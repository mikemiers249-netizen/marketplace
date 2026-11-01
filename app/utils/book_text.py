"""Reflow private books into running text. Source pages are bookmark anchors only."""
import re
from collections import Counter
from contextlib import closing
from functools import lru_cache
import pypdfium2 as pdfium
from pypdfium2 import raw as pdfium_raw
from app.utils.book_pages import epub_pages
from app.utils.digital_books import file_path
from app.utils.digital_books import epub_chapters


def epub_picture(raw, asset):
    import io
    from PIL import Image
    if asset.lower().endswith('.svg'):
        from xml.etree import ElementTree as ET
        if b'<!ENTITY' in raw.upper() or b'<!DOCTYPE' in raw.upper():
            raise ValueError('Недопустимая SVG-иллюстрация')
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError('Не удалось прочитать SVG-иллюстрацию') from exc
        if root.tag.split('}')[-1] != 'svg':
            raise ValueError('Недопустимая SVG-иллюстрация')
        allowed = {'svg', 'g', 'defs', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'text', 'tspan', 'use', 'symbol', 'clipPath', 'linearGradient', 'radialGradient', 'stop'}
        attrs = {'id', 'viewBox', 'width', 'height', 'x', 'y', 'x1', 'x2', 'y1', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'd', 'points', 'fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-opacity', 'opacity', 'transform', 'clip-path', 'offset', 'stop-color', 'stop-opacity', 'font-size', 'font-family', 'text-anchor', 'gradientUnits', 'gradientTransform', 'href'}
        for parent in root.iter():
            for child in list(parent):
                if child.tag.split('}')[-1] not in allowed:
                    parent.remove(child)
            for key, value in list(parent.attrib.items()):
                short = key.split('}')[-1]
                if short not in attrs or (short == 'href' and not value.startswith('#')) or ('url(' in value and not re.fullmatch(r'url\(#[\w-]+\)', value)):
                    del parent.attrib[key]
        view = re.split(r'[\s,]+', root.attrib.get('viewBox', '').strip())
        width, height = (float(view[2]), float(view[3])) if len(view) == 4 else (800, 600)
        return io.BytesIO(ET.tostring(root)), max(1, round(width)), max(1, round(height)), 'image/svg+xml'
    with Image.open(io.BytesIO(raw)) as original:
        width, height = original.size
        original.thumbnail((2400, 2400))
        output = io.BytesIO()
        original.convert('RGBA').save(output, 'PNG')
    output.seek(0)
    return output, width, height, 'image/png'


def body_font_size(sizes):
    threshold = sum(sizes.values()) * .75
    accumulated = 0
    for size, count in sorted(sizes.items()):
        accumulated += count
        if accumulated >= threshold:
            return size
    return None


def inline_pdf_notes(lines, main_size=None):
    """Keep footer notes in the flow and attach recognized references to their text."""
    body, notes = [], []
    current = None
    sizes = Counter()
    for line in lines:
        if len(line) > 3:
            sizes[round(line[3], 1)] += len(line[0])
    main_size = main_size or body_font_size(sizes)
    for line in lines:
        text, bottom, height = line[:3]
        small = line[3] < main_size * .92 if main_size and len(line) > 3 else bottom < height * .25
        marker = re.match(r'^\s*(?:\((\d{1,3})\)|(\d{1,3}|[*†‡])[.)]?)\s+(\S.*)$', text)
        footer = bottom < height * .3
        if small and footer and marker:
            current = [marker[1] or marker[2], marker[3]]
            notes.append(current)
        elif current is not None and small and footer:
            current[1] += '\n' + text
        elif small and bottom < height * .08:
            current = None
        else:
            body.append(text)
            current = None
    text = '\n'.join(body)
    for marker, note in notes:
        escaped = re.escape(marker)
        pattern = rf'\[{escaped}\]|\({escaped}\)|(?<=\w){escaped}(?!\d)'
        replacement = f' (Сноска {marker}: {" ".join(flowing_text(note).split())}) '
        if re.search(pattern, text):
            text = re.sub(pattern, lambda match: replacement, text, count=1)
    return text


def flowing_text(text):
    # Some publishers encode discretionary hyphens as U+0002 in the PDF font.
    text = re.sub(r'[\x02\u00ad]\n(?=\w)', '', text)
    text = re.sub(r'(\w)-\n(?=\w)', r'\1', text)
    return text.replace('\x02', '-').replace('\u00ad', '')


def join_source_continuations(sections):
    previous = None
    for section in sections:
        blocks = section['blocks']
        if not blocks:
            continue
        if previous is not None:
            tail = previous['blocks'][-1]
            # A source page ending mid-sentence/word has no visible boundary.
            plain = (previous.get('styles', [{}])[-1].get('tag', 'p') == 'p'
                     and section.get('styles', [{}])[0].get('tag', 'p') == 'p')
            if plain and tail and (tail[-1] == '-' or (tail[-1] not in '.!?…:»”' and blocks[0][0].islower())):
                previous['blocks'][-1] = tail[:-1] + blocks.pop(0) if tail.endswith('-') else tail + ' ' + blocks.pop(0)
                if section.get('styles'):
                    section['styles'].pop(0)
        if blocks:
            previous = section
    return sections


def pdf_blocks(lines, main_size, illustrations=()):
    """Keep headings and TOC rows distinct; join only typeset lines of body paragraphs."""
    blocks, styles, paragraph = [], [], []
    paragraph_kind = 'body'
    metadata = {line[0][:24]: line for line in lines}
    images = list(sorted(illustrations, key=lambda image: -image['top']))
    body_left = min((line[4] for line in lines if len(line) > 4
                     and abs(line[3] - main_size) < .8), default=0)

    def flush():
        if paragraph:
            blocks.append(' '.join(flowing_text('\n'.join(paragraph)).split()))
            styles.append({'tag': 'p', 'kind': paragraph_kind})
            paragraph.clear()

    for line in inline_pdf_notes(lines, main_size).splitlines():
        if not line.strip():
            flush()
            continue
        info = metadata.get(line[:24])
        while images and info and info[1] < images[0]['top']:
            flush()
            blocks.append('')
            styles.append(images.pop(0))
        clean = ' '.join(flowing_text(line).split())
        toc = re.match(r'^(.*?)\s*(?:\.{2,}|…+)\s*(\d{1,4})$', clean)
        if not toc and re.match(r'^ГЛАВА\s', clean, re.I):
            toc = re.match(r'^(.*?)\s+(\d{1,4})$', clean)
        if toc:
            flush()
            blocks.append(clean)
            styles.append({'tag': 'toc', 'label': toc[1].rstrip('. …'), 'page': toc[2]})
            continue
        letters = ''.join(c for c in clean if c.isalpha())
        heading = (len(clean) < 160 and letters and letters.isupper())
        heading = heading or bool(info and len(clean) < 160 and info[3] > main_size * 1.22)
        # Require a whole emphasized line, not just a bold opening word.
        # A sentence ending is normally body text even when printed in bold.
        subtitle = bool(info and len(clean) < 160 and letters
                        and not re.search(r'[.!?…,:;]$', clean)
                        and (info[3] > main_size * 1.08
                             or (len(info) > 5 and info[5] >= .8)))
        if heading or subtitle:
            flush()
            blocks.append(clean)
            styles.append({'tag': 'h2' if heading else 'h3'})
            continue
        kind = 'summary' if info and info[3] < main_size * .92 else 'body'
        indented = bool(kind == 'body' and info and len(info) > 4 and info[4] > body_left + main_size * .7)
        numbered = bool(re.match(r'^\d{1,3}[°º.)]\s', clean))
        sentence_boundary = bool(kind == 'body' and paragraph and re.search(r'[.!?…»]$', paragraph[-1].rstrip()))
        if indented or kind != paragraph_kind or numbered or sentence_boundary:
            flush()
        paragraph_kind = kind
        paragraph.append(line)
    flush()
    for image in images:
        blocks.append('')
        styles.append(image)
    return blocks, styles


def pdf_sections(path):
    pages = []
    margins = Counter()
    font_sizes = Counter()
    illustrations = []
    def margin_key(text):
        return re.sub(r'\d+', '#', ' '.join(text.casefold().split()))
    with closing(pdfium.PdfDocument(str(path))) as document:
        if not 0 < len(document) <= 10000:
            raise ValueError('Недопустимое количество страниц PDF')
        for index in range(len(document)):
            with closing(document[index]) as page, closing(page.get_textpage()) as textpage:
                height = page.get_height()
                images = []
                parents = {}
                for obj in page.get_objects(max_depth=8):
                    if obj.type == pdfium_raw.FPDF_PAGEOBJ_FORM:
                        parents[obj.level] = obj.get_matrix()
                        continue
                    if obj.type not in (pdfium_raw.FPDF_PAGEOBJ_IMAGE, pdfium_raw.FPDF_PAGEOBJ_PATH):
                        continue
                    bounds = obj.get_pos()
                    for level in reversed(range(obj.level)):
                        bounds = parents[level].on_rect(*bounds)
                    left, bottom, right, top = bounds
                    if obj.type == pdfium_raw.FPDF_PAGEOBJ_PATH and (right-left)*(top-bottom) > page.get_width()*height*.85:
                        continue
                    if right - left >= 24 and top - bottom >= 24:
                        images.append(dict(tag='img', left=left, bottom=bottom, right=right, top=top,
                                           width=round(right-left), height=round(top-bottom)))
                # Several PDF objects can be layers of a single illustration.
                merged = []
                for image in images:
                    for other in list(merged):
                        if (image['left'] <= other['right']+2 and image['right'] >= other['left']-2
                                and image['bottom'] <= other['top']+2 and image['top'] >= other['bottom']-2):
                            for key in ('left', 'bottom'):
                                image[key] = min(image[key], other[key])
                            for key in ('right', 'top'):
                                image[key] = max(image[key], other[key])
                            merged.remove(other)
                    image['width'], image['height'] = round(image['right']-image['left']), round(image['top']-image['bottom'])
                    merged.append(image)
                illustrations.append(merged)
                raw = textpage.get_text_range(index=0, count=textpage.count_chars())
                def char_index(index):
                    return pdfium_raw.FPDFText_GetCharIndexFromTextIndex(textpage, index)

                def clean_line(line, start):
                    cleaned = []
                    for offset, character in enumerate(line):
                        if character in ('\ufffe', '\x02'):
                            index = start + offset
                            try:
                                before = textpage.get_charbox(char_index(max(0, index - 1)))
                                after = textpage.get_charbox(char_index(index + 1))
                                # Drop typesetting hyphens when the next letter is on another line.
                                if abs(before[1] - after[1]) <= 2:
                                    cleaned.append('-')
                            except (IndexError, RuntimeError):
                                cleaned.append('-')
                        else:
                            cleaned.append(character)
                    return ''.join(cleaned)
                lines, offset = [], 0
                for line in raw.splitlines(keepends=True):
                    text = clean_line(line, offset).strip()
                    if text:
                        try:
                            index = char_index(offset + len(line) - len(line.lstrip()))
                            box = textpage.get_charbox(index)
                            bottom, left = box[1], box[0]
                            size = pdfium_raw.FPDFText_GetFontSize(textpage, index)
                        except (IndexError, RuntimeError):
                            bottom = height / 2
                            size = 12
                            left = 0
                        weights = []
                        for position, character in enumerate(line):
                            if character.isalpha():
                                glyph = char_index(offset + position)
                                weights.append(pdfium_raw.FPDFText_GetFontWeight(textpage, glyph) >= 600)
                        bold_fraction = sum(weights) / len(weights) if weights else 0
                        lines.append((text, bottom, height, size, left, bold_fraction))
                        font_sizes[round(size, 1)] += len(text)
                        if bottom > height * .88 or bottom < height * .08:
                            margins[margin_key(text)] += 1
                    offset += len(line)
                pages.append(lines)
    sections = []
    for index, lines in enumerate(pages):
        lines = [line for line in lines
                 if not ((line[1] > line[2] * .88 or line[1] < line[2] * .08)
                         and (re.fullmatch(r'[—–\-\s]*\d{1,5}[—–\-\s]*', line[0])
                              or margins[margin_key(line[0])] >= max(2, len(pages) // 8)))]
        blocks, styles = pdf_blocks(lines, body_font_size(font_sizes) or 12, illustrations[index])
        sections.append({'index': index, 'blocks': blocks, 'styles': styles})
    if not any(section['blocks'] for section in sections):
        raise ValueError('В PDF нет текстового слоя. Для непрерывного чтения нужен распознанный текст.')
    if sum(len(block) for section in sections for block in section['blocks']) > 40 * 1024 * 1024:
        raise ValueError('Слишком большой текст книги')
    return join_source_continuations(sections)


@lru_cache(maxsize=4)
def _sections(path, modified, size, fmt):
    if fmt == 'pdf':
        return pdf_sections(path)
    from types import SimpleNamespace
    pages = epub_pages(SimpleNamespace(file_name=path.name))
    headings = {text: ('h2' if tag == 'h1' else 'h3') for chapter in epub_chapters(path)
                for tag, text in chapter['blocks'] if tag in ('h1', 'h2', 'h3')}
    sections = []
    for index, lines in enumerate(pages):
        blocks, styles = [], []
        for part in re.split(r'(\x00IMAGE:[^\n]+)', '\n'.join(lines)):
            if part.startswith('\x00IMAGE:'):
                asset = part[len('\x00IMAGE:'):]
                from zipfile import ZipFile
                with ZipFile(path) as archive:
                    _, width, height, _ = epub_picture(archive.read(asset), asset)
                blocks.append('')
                styles.append(dict(tag='img', asset=asset, width=width, height=height))
            else:
                for paragraph in re.split(r'\n\s*\n', part):
                    block = ' '.join(paragraph.split())
                    if block:
                        blocks.append(block)
                        styles.append({'tag': headings.get(block, 'p')})
        sections.append({'index': index, 'blocks': blocks, 'styles': styles})
    return sections


def reading_sections(book):
    path = file_path(book.file_name)
    stat = path.stat()
    return _sections(path, stat.st_mtime_ns, stat.st_size, book.format)


def illustration_image(book, source, block):
    """Serve only an authorized illustration, never publisher paths or source files."""
    import io
    from PIL import Image
    from zipfile import ZipFile
    sections = reading_sections(book)
    style = sections[source]['styles'][block]
    if style['tag'] != 'img':
        raise IndexError()
    path = file_path(book.file_name)
    if book.format == 'pdf':
        with closing(pdfium.PdfDocument(str(path))) as document, closing(document[source]) as page:
            left, bottom = max(0, style['left']), max(0, style['bottom'])
            right, top = min(page.get_width(), style['right']), min(page.get_height(), style['top'])
            width, height = right-left, top-bottom
            if min(width, height) <= 0:
                raise ValueError('Недопустимые границы иллюстрации')
            # Rasterize the illustration itself at reading resolution. PDF points
            # describe its layout, not the pixel resolution of the original image.
            scale = 2400 / max(width, height)
            crop = (left, bottom, page.get_width()-right, page.get_height()-top)
            with closing(page.render(scale=scale, crop=crop)) as bitmap:
                image = bitmap.to_pil().convert('RGB').copy()
    else:
        with ZipFile(path) as archive:
            data, _, _, mimetype = epub_picture(archive.read(style['asset']), style['asset'])
            return data, mimetype
    output = io.BytesIO()
    image.save(output, 'PNG')
    output.seek(0)
    return output, 'image/png'
