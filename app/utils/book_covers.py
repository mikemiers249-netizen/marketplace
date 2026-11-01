"""One validated cover, normalized like physical-product photos."""
import io
import secrets
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError
from app.utils.digital_books import storage, file_path

MAX_COVER = 10 * 1024 * 1024


def save_cover(files):
    uploads = [f for f in files.getlist('cover') if f.filename]
    if len(uploads) > 1:
        raise ValueError('Для обложки можно загрузить только одно изображение')
    if not uploads:
        return None
    raw = uploads[0].stream.read(MAX_COVER + 1)
    if not raw or len(raw) > MAX_COVER:
        raise ValueError('Размер обложки должен быть от 1 байта до 10 МБ')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in ('JPEG', 'PNG', 'WEBP', 'GIF', 'BMP', 'TIFF'):
                    raise ValueError('Загрузите обложку в формате изображения')
                if source.width * source.height > 40000000:
                    raise ValueError('Слишком большое разрешение обложки')
                image = ImageOps.exif_transpose(source)
                image.thumbnail((800, 800), Image.Resampling.LANCZOS)
                # White background for transparent covers; discard metadata.
                rgba = image.convert('RGBA')
                result = Image.new('RGB', rgba.size, 'white')
                result.paste(rgba, mask=rgba.getchannel('A'))
                output = io.BytesIO()
                result.save(output, 'JPEG', quality=85, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('Не удалось прочитать обложку. Выберите другое изображение.') from exc
    storage().mkdir(parents=True, exist_ok=True)
    name = 'cover_' + secrets.token_hex(24) + '.jpg'
    path = file_path(name)
    try:
        path.write_bytes(output.getvalue())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return name
