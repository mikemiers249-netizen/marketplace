"""
Модели подвала (footer): ссылки в нижней части сайта.
"""
from datetime import datetime
from app import db


class FooterLink(db.Model):
    """
    Ссылка в подвале сайта. Может рендериться в одной из колонок
    ('info', 'support', 'additional'). Контент по ссылке — HTML,
    показывается модалкой или отдельной страницей по slug'у.
    """

    __tablename__ = 'footer_links'

    # Колонки подвала
    COLUMN_INFO = 'info'
    COLUMN_SUPPORT = 'support'
    COLUMN_ADDITIONAL = 'additional'
    COLUMNS = (COLUMN_INFO, COLUMN_SUPPORT, COLUMN_ADDITIONAL)

    # Способы показа
    DISPLAY_MODAL = 'modal'
    DISPLAY_PAGE = 'page'
    DISPLAY_MODES = (DISPLAY_MODAL, DISPLAY_PAGE)

    id = db.Column(db.Integer, primary_key=True)

    # Заголовок ссылки (то, что видно в подвале).
    title = db.Column(db.String(200), nullable=False)

    # URL-slug: используется для /page/<slug>, и для модалки как data-атрибут.
    slug = db.Column(db.String(200), nullable=False, unique=True, index=True)

    # HTML-содержимое, открывающееся по клику (уже отрендеренное/безопасно
    # вставленное админом). Может быть длинным.
    content = db.Column(db.Text, nullable=False, default='')

    # Способ показа: 'modal' (Bootstrap modal) или 'page' (отдельный URL).
    display_mode = db.Column(db.String(20), nullable=False, default=DISPLAY_MODAL)

    # Колонка подвала, в которой показывать ссылку.
    column = db.Column(db.String(20), nullable=False, default=COLUMN_INFO)

    # Включена ли ссылка (если False — не рендерится в подвале).
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    # Порядок внутри колонки.
    sort_order = db.Column(db.Integer, nullable=False, default=100)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<FooterLink {self.id} {self.column}/{self.slug!r}>'

    @staticmethod
    def normalize_slug(value: str) -> str:
        """Транслит + дефисы для slug."""
        import re
        # Простейшая транслитерация (рус -> lat).
        table = {
            'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
            'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
            'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
            'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
            'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        }
        v = (value or '').strip().lower()
        out = []
        for ch in v:
            if ch in table:
                out.append(table[ch])
            elif ch.isascii() and (ch.isalnum() or ch in '-_'):
                out.append(ch)
            elif ch.isspace():
                out.append('-')
        s = ''.join(out)
        s = re.sub(r'-+', '-', s).strip('-')
        return s or 'page'
