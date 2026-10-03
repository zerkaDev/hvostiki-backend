"""Обработка аватара профиля: проверка, нормализация и перекодирование в JPEG."""
import io

from django.core.files.base import ContentFile
from PIL import Image, ImageOps
from rest_framework.exceptions import ValidationError

MAX_AVATAR_BYTES = 10 * 1024 * 1024
MAX_AVATAR_SIDE = 1024
ALLOWED_FORMATS = {'JPEG', 'PNG', 'HEIF', 'HEIC', 'MPO'}


def process_avatar(uploaded_file):
    """Возвращает ``ContentFile`` с JPEG не больше 1024 px по большей стороне.

    Проверяет размер и содержимое (а не расширение), применяет EXIF-поворот
    и перекодирует HEIC/PNG в JPEG, чтобы клиенты получали единый формат.
    """
    if uploaded_file.size > MAX_AVATAR_BYTES:
        raise ValidationError({'avatar': ['Файл слишком большой. Максимум — 10 МБ.']})

    try:
        uploaded_file.seek(0)
        probe = Image.open(uploaded_file)
        image_format = probe.format
        probe.verify()
        uploaded_file.seek(0)
        image = Image.open(uploaded_file)
        image.load()
    except Exception:
        raise ValidationError({'avatar': ['Не удалось прочитать изображение.']})

    if image_format not in ALLOWED_FORMATS:
        raise ValidationError(
            {'avatar': ['Неподдерживаемый формат. Допустимы JPG, PNG и HEIC.']}
        )

    image = ImageOps.exif_transpose(image)
    if image.mode in ('RGBA', 'LA', 'P'):
        image = image.convert('RGBA')
        background = Image.new('RGB', image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel('A'))
        image = background
    else:
        image = image.convert('RGB')
    image.thumbnail((MAX_AVATAR_SIDE, MAX_AVATAR_SIDE))

    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=85, optimize=True)
    return ContentFile(buffer.getvalue(), name='avatar.jpg')
