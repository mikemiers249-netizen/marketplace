"""Private, bounded EPUB parsing. Publisher HTML is never rendered as HTML."""
import posixpath
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree as ET
from flask import current_app

MAX_FILE = 15 * 1024 * 1024


def storage():
    return Path(current_app.config['UPLOAD_FOLDER']) / '.private' / 'books'


def file_path(name):
    if not name or Path(name).name != name or '/' in name or '\\' in name:
        raise ValueError('Invalid book file')
    return storage() / name


def epub_chapters(path):
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 1000 or sum(i.file_size for i in infos) > 40 * 1024 * 1024:
                raise ValueError('Слишком большой EPUB после распаковки')
            if any(i.file_size > 4 * 1024 * 1024 or i.flag_bits & 1 for i in infos):
                raise ValueError('Недопустимый размер или шифрование EPUB')
            if archive.read('mimetype').strip() != b'application/epub+zip':
                raise ValueError('Неверный формат EPUB')

            def xml(name):
                if name.startswith('/') or '..' in PurePosixPath(name).parts or '\\' in name:
                    raise ValueError('Небезопасный путь EPUB')
                raw = archive.read(name)
                if b'<!ENTITY' in raw.upper():
                    raise ValueError('XML-декларации сущностей не поддерживаются')
                return ET.fromstring(raw)

            container = xml('META-INF/container.xml')
            rootfile = next(e for e in container.iter() if e.tag.split('}')[-1] == 'rootfile')
            opf_name = rootfile.attrib['full-path']
            package = xml(opf_name)
            manifest = {e.attrib['id']: e.attrib.get('href', '') for e in package.iter()
                        if e.tag.split('}')[-1] == 'item'}
            chapters = []
            for ref in package.iter():
                if ref.tag.split('}')[-1] != 'itemref':
                    continue
                name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_name),
                                                       manifest[ref.attrib['idref']]))
                root = xml(name)
                for parent in root.iter():
                    for child in list(parent):
                        if child.tag.split('}')[-1] in ('script', 'style', 'iframe', 'object'):
                            parent.remove(child)
                blocks = []
                for e in root.iter():
                    tag = e.tag.split('}')[-1]
                    if tag in ('h1', 'h2', 'h3', 'p', 'li', 'blockquote', 'pre'):
                        text = ' '.join(''.join(e.itertext()).split())
                        if text:
                            blocks.append((tag if tag in ('h1', 'h2', 'h3') else 'p', text))
                if not blocks:
                    text = ' '.join(''.join(root.itertext()).split())
                    if text:
                        blocks = [('p', text)]
                if blocks:
                    title = next((text for tag, text in blocks if tag.startswith('h')), f'Глава {len(chapters)+1}')
                    chapters.append({'title': title[:160], 'blocks': blocks})
            if not chapters:
                raise ValueError('В EPUB не найден текст книги')
            return chapters
    except (BadZipFile, KeyError, StopIteration, ET.ParseError, OSError) as exc:
        raise ValueError('Не удалось прочитать EPUB. Проверьте файл книги.') from exc
