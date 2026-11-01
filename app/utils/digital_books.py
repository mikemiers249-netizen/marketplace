"""Private, bounded EPUB parsing. Publisher HTML is never rendered as HTML."""
import posixpath
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree as ET
from urllib.parse import unquote, urldefrag
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
            roots = {}
            notes = {}
            for href in manifest.values():
                name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_name), unquote(href)))
                if name.lower().endswith(('.xhtml', '.html', '.htm')):
                    root = xml(name)
                    roots[name] = root
                    for element in root.iter():
                        kind = ' '.join(element.attrib.values()).lower().split()
                        if any(k in kind for k in ('footnote', 'endnote', 'doc-footnote', 'doc-endnote')):
                            note_id = element.attrib.get('id')
                            if note_id:
                                notes[(name, note_id)] = ' '.join(''.join(element.itertext()).split())
            # Move linked notes into the running text; never pass publisher HTML through.
            for name, root in roots.items():
                for parent in root.iter():
                    for child in list(parent):
                        tag = child.tag.split('}')[-1]
                        if (name, child.attrib.get('id')) in notes:
                            parent.remove(child)
                        elif tag == 'a':
                            target, fragment = urldefrag(unquote(child.attrib.get('href', '')))
                            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), target)) if target else name
                            note = notes.get((target, fragment))
                            if note:
                                tail = child.tail
                                child.clear()
                                child.text = f' (Сноска: {note}) '
                                child.tail = tail
            chapters = []
            for ref in package.iter():
                if ref.tag.split('}')[-1] != 'itemref':
                    continue
                name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_name),
                                                       manifest[ref.attrib['idref']]))
                root = roots.get(name)
                if root is None:
                    root = xml(name)
                for parent in root.iter():
                    for child in list(parent):
                        if child.tag.split('}')[-1] in ('script', 'style', 'iframe', 'object'):
                            parent.remove(child)
                blocks = []
                def walk(element, heading='p'):
                    tag = element.tag.split('}')[-1]
                    if tag in ('head', 'header', 'footer', 'nav'):
                        return
                    if tag in ('img', 'image'):
                        href = element.attrib.get('src') or element.attrib.get('{http://www.w3.org/1999/xlink}href', '')
                        asset = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(href)))
                        if asset in archive.namelist() and not asset.startswith('/') and '..' not in PurePosixPath(asset).parts:
                            blocks.append(('img', asset))
                        return
                    kind = tag if tag in ('h1', 'h2', 'h3') else heading
                    text = []
                    def flush():
                        value = ' '.join(''.join(text).split())
                        if value:
                            blocks.append((kind, value))
                        text.clear()
                    if element.text:
                        text.append(element.text)
                    for child in element:
                        child_tag = child.tag.split('}')[-1]
                        if child_tag in ('head', 'header', 'footer', 'nav'):
                            continue
                        if child_tag in ('img', 'image', 'h1', 'h2', 'h3', 'p', 'li', 'blockquote', 'pre', 'html', 'body', 'main', 'article', 'aside', 'div', 'section', 'figure', 'svg'):
                            flush()
                            walk(child, kind)
                        elif any(e.tag.split('}')[-1] in ('img', 'image') for e in child.iter()):
                            flush()
                            walk(child, kind)
                        else:
                            text.append(''.join(child.itertext()))
                        if child.tail:
                            text.append(child.tail)
                    flush()
                walk(root)
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
