"""Render bounded reader pages; never send the source document to the browser."""
import io
import textwrap
from contextlib import closing
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import pypdfium2 as pdfium
from app.utils.digital_books import epub_chapters, file_path


def font(size):
    for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                 'C:/Windows/Fonts/arial.ttf'):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise ValueError('Шрифт читалки недоступен')


def epub_pages(book):
    pages = []
    for chapter in epub_chapters(file_path(book.file_name)):
        lines = []
        for tag, text in chapter['blocks']:
            lines.extend(['\x00IMAGE:' + text] if tag == 'img' else textwrap.wrap(text, width=66, break_long_words=True))
            lines.append('')
        pages.extend([lines[i:i+32] for i in range(0, len(lines), 32)])
    return pages


def page_count(book):
    if book.format == 'epub':
        return len(epub_pages(book))
    try:
        with closing(pdfium.PdfDocument(str(file_path(book.file_name)))) as document:
            count = len(document)
            if not 0 < count <= 10000:
                raise ValueError('Недопустимое количество страниц PDF')
            return count
    except pdfium.PdfiumError as exc:
        raise ValueError('Не удалось прочитать PDF') from exc


def page_image(book, index, watermark):
    if book.format == 'pdf':
        with closing(pdfium.PdfDocument(str(file_path(book.file_name)))) as document:
            if not 0 <= index < len(document):
                raise IndexError()
            with closing(document[index]) as page:
                width, height = page.get_size()
                if min(width, height) <= 0:
                    raise ValueError('Недопустимый размер страницы')
                scale = min(1.8, 1600 / max(width, height))
                with closing(page.render(scale=scale)) as bitmap:
                    image = bitmap.to_pil().convert('RGB').copy()
    else:
        pages = epub_pages(book)
        if not 0 <= index < len(pages):
            raise IndexError()
        image = Image.new('RGB', (1000, 1450), 'white')
        draw = ImageDraw.Draw(image)
        for number, line in enumerate(pages[index]):
            draw.text((50, 55 + number * 40), line, font=font(24), fill='#222222')
    draw = ImageDraw.Draw(image)
    mark_font = font(max(12, image.width // 65))
    for y in (image.height // 3, image.height * 2 // 3, image.height - 30):
        draw.text((20, y), watermark, font=mark_font, fill='#b5bbc3', stroke_width=0)
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=88)
    output.seek(0)
    return output
