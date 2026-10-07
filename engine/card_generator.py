"""Vulture OSINT Terminal Dossier Renderer (Pillow).

Генерирует высокоточную карточку досье 2400x1350 (@2x) в стиле терминала кибер-разведки.
Использует шрифт JetBrains Mono, HUD-визиры, индикаторы графа связей и
полностью русифицированный интерфейс протокола.

Все тяжёлые Pillow-операции вынесены в ``asyncio.to_thread``, чтобы не блокировать
Event Loop при обработке сообщений из других чатов.
"""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Пути и шрифты
# ---------------------------------------------------------------------------

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

FONT_FILES = {
    "regular": "JetBrainsMono-Regular.ttf",
    "bold": "JetBrainsMono-Bold.ttf",
}

_GITHUB_BASE = "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/ttf"
FONT_URLS: dict[str, str] = {
    name: f"{_GITHUB_BASE}/{name}" for name in FONT_FILES.values()
}

# ---------------------------------------------------------------------------
# Цветовая палитра: Киберпанк / Закрытый терминал
# ---------------------------------------------------------------------------

COLOR_BG = (10, 12, 16)
COLOR_PANEL_BG = (16, 20, 26)
COLOR_PANEL_ALT = (20, 26, 35)
COLOR_BORDER = (38, 48, 62)
COLOR_GRID = (20, 25, 34)

COLOR_NEON = (0, 255, 170)        # #00FFAA (Мятный неон)
COLOR_NEON_DIM = (0, 100, 70)
COLOR_GOLD = (255, 184, 0)        # #FFB800 (Янтарный статус)
COLOR_CYAN = (94, 231, 255)       # #5EE7FF (Информационный)
COLOR_WHITE = (240, 246, 255)
COLOR_TEXT_DIM = (118, 134, 156)
COLOR_ALERT = (255, 75, 75)       # Тревожный красный
RETINA_SCALE = 2

# ---------------------------------------------------------------------------
# Загрузка шрифтов
# ---------------------------------------------------------------------------

async def ensure_fonts() -> None:
    """Автоматически скачивает шрифты JetBrains Mono при первом старте."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in FONT_URLS.items():
        filepath = ASSETS_DIR / filename
        if filepath.exists():
            continue
        try:
            logger.info("Загрузка шрифта: %s", filename)
            from config import get_settings
            proxy = get_settings().PROXY_URL or None
            async with httpx.AsyncClient(proxy=proxy, follow_redirects=True, timeout=25.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                filepath.write_bytes(resp.content)
                logger.info("Шрифт сохранен: %s", filepath)
        except Exception as exc:
            logger.warning("Не удалось загрузить шрифт %s: %s", filename, exc)


def _font(style: str, size: int, scale: int = 1) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Безопасная загрузка шрифта с фолбэком."""
    path = ASSETS_DIR / FONT_FILES.get(style, FONT_FILES["regular"])
    try:
        return ImageFont.truetype(str(path), size * scale)
    except Exception:
        return ImageFont.load_default()


class _RetinaDraw:
    """Scale Pillow drawing coordinates while keeping layout in logical pixels."""

    def __init__(self, image: Image.Image, scale: int) -> None:
        self._draw = ImageDraw.Draw(image)
        self._scale = scale

    def _point(self, point: tuple[int, int]) -> tuple[int, int]:
        return point[0] * self._scale, point[1] * self._scale

    def _box(self, box: list[tuple[int, int]] | tuple[tuple[int, int], tuple[int, int]]) -> list[tuple[int, int]]:
        return [self._point(point) for point in box]

    def line(self, points: list[tuple[int, int]], **kwargs) -> None:
        kwargs["width"] = kwargs.get("width", 1) * self._scale
        self._draw.line(self._box(points), **kwargs)

    def rectangle(self, box: list[tuple[int, int]] | tuple[tuple[int, int], tuple[int, int]], **kwargs) -> None:
        if "width" in kwargs:
            kwargs["width"] *= self._scale
        self._draw.rectangle(self._box(box), **kwargs)

    def ellipse(self, box: list[tuple[int, int]] | tuple[tuple[int, int], tuple[int, int]], **kwargs) -> None:
        self._draw.ellipse(self._box(box), **kwargs)

    def text(self, xy: tuple[int, int], text: str, **kwargs) -> None:
        self._draw.text(self._point(xy), text, **kwargs)

    def textbbox(self, xy: tuple[int, int], text: str, **kwargs) -> tuple[int, int, int, int]:
        return self._draw.textbbox(self._point(xy), text, **kwargs)


# ---------------------------------------------------------------------------
# Графические HUD-примитивы
# ---------------------------------------------------------------------------

def _draw_hud_brackets(draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int, length: int = 14, color=COLOR_NEON) -> None:
    """Отрисовка тактических угловых визиров рамки."""
    # Верхний левый
    draw.line([(x1, y1), (x1 + length, y1)], fill=color, width=2)
    draw.line([(x1, y1), (x1, y1 + length)], fill=color, width=2)
    # Верхний правый
    draw.line([(x2, y1), (x2 - length, y1)], fill=color, width=2)
    draw.line([(x2, y1), (x2, y1 + length)], fill=color, width=2)
    # Нижний левый
    draw.line([(x1, y2), (x1 + length, y2)], fill=color, width=2)
    draw.line([(x1, y2), (x1, y2 - length)], fill=color, width=2)
    # Нижний правый
    draw.line([(x2, y2), (x2 - length, y2)], fill=color, width=2)
    draw.line([(x2, y2), (x2, y2 - length)], fill=color, width=2)


def _draw_segmented_bar(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    ratio: float,
    segments: int = 16,
    active_color=COLOR_NEON,
    inactive_color=COLOR_PANEL_ALT,
) -> None:
    """Отрисовка сегментированного сенсорного бара."""
    ratio = max(0.0, min(1.0, ratio))
    gap = 4
    total_gaps = gap * (segments - 1)
    seg_w = max(2, (w - total_gaps) // segments)
    active_segments = int(round(ratio * segments))

    for i in range(segments):
        sx = x + i * (seg_w + gap)
        fill = active_color if i < active_segments else inactive_color
        draw.rectangle([(sx, y), (sx + seg_w, y + h)], fill=fill)


def _wrap_text(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Перенос строк по словам с ограничением ширины."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Синхронные рендереры (вызываются через asyncio.to_thread)
# ---------------------------------------------------------------------------

def _render_dossier_sync(
    profile_data: dict,
    bot_username: str = "VultureBot",
) -> BytesIO:
    """Рендерит карточку досье 2400x1350 (@2x) и возвращает PNG buffer.

    ВНИМАНИЕ: Это CPU-bound функция. Вызывать ТОЛЬКО через asyncio.to_thread().
    """
    w, h = 1200, 675
    img = Image.new("RGB", (w * RETINA_SCALE, h * RETINA_SCALE), COLOR_BG)
    draw = _RetinaDraw(img, RETINA_SCALE)

    # 1. Фоновая тактическая сетка
    for x in range(30, w - 30, 45):
        draw.line([(x, 30), (x, h - 30)], fill=COLOR_GRID, width=1)
    for y in range(30, h - 30, 45):
        draw.line([(30, y), (w - 30, y)], fill=COLOR_GRID, width=1)

    # 2. Главная рамка терминала
    draw.rectangle([(30, 24), (1170, 650)], fill=None, outline=COLOR_BORDER, width=2)
    _draw_hud_brackets(draw, 24, 18, 1176, 656, length=20, color=COLOR_NEON)

    # Инициализация шрифтов
    f_tag = _font("bold", 12, RETINA_SCALE)
    f_header = _font("bold", 16, RETINA_SCALE)
    f_title = _font("bold", 28, RETINA_SCALE)
    f_username = _font("regular", 18, RETINA_SCALE)
    f_metric_label = _font("bold", 12, RETINA_SCALE)
    f_metric_value = _font("bold", 24, RETINA_SCALE)
    f_body = _font("regular", 16, RETINA_SCALE)
    f_diag_title = _font("bold", 14, RETINA_SCALE)

    # 3. Верхняя панель статуса
    draw.rectangle([(32, 26), (1168, 72)], fill=COLOR_PANEL_BG)
    draw.line([(32, 72), (1168, 72)], fill=COLOR_BORDER, width=1)

    # Индикаторы протокола
    draw.rectangle([(48, 38), (175, 60)], fill=(0, 45, 30), outline=COLOR_NEON, width=1)
    draw.text((58, 42), "СОВ. СЕКРЕТНО", fill=COLOR_NEON, font=f_tag)

    draw.text((192, 42), "ПРОТОКОЛ VULTURE // АНАЛИЗ СОЦИАЛЬНОГО ГРАФА", fill=COLOR_WHITE, font=f_header)

    # Радар активности
    draw.ellipse([(1020, 44), (1032, 56)], fill=COLOR_NEON)
    draw.text((1042, 42), "РАДАР: АКТИВЕН", fill=COLOR_NEON, font=f_tag)

    # 4. Блок идентификации субъекта
    rank_title = str(profile_data.get("rank_title", "НЕКЛАССИФИЦИРОВАННЫЙ ОБЪЕКТ")).upper()
    username = str(profile_data.get("username", "НЕИЗВЕСТНЫЙ"))

    draw.text((52, 94), rank_title, fill=COLOR_GOLD, font=f_title)
    draw.text((54, 136), f"ОБЪЕКТ СЛЕЖКИ: @{username}", fill=COLOR_CYAN, font=f_username)

    # Бейдж уровня доступа
    draw.rectangle([(970, 96), (1148, 142)], fill=COLOR_PANEL_ALT, outline=COLOR_BORDER, width=1)
    draw.text((984, 104), "УРОВЕНЬ ДОСТУПА", fill=COLOR_TEXT_DIM, font=f_tag)
    draw.text((984, 118), "ГРИФ: КЛАСС-4", fill=COLOR_WHITE, font=f_header)

    # -----------------------------------------------------------------------
    # Левая матрица: Тактические метрики
    # -----------------------------------------------------------------------
    draw.rectangle([(50, 172), (480, 592)], fill=COLOR_PANEL_BG, outline=COLOR_BORDER, width=1)
    _draw_hud_brackets(draw, 50, 172, 480, 592, length=10, color=COLOR_BORDER)

    draw.rectangle([(50, 172), (480, 206)], fill=COLOR_PANEL_ALT)
    draw.text((64, 182), "ТЕЛЕМЕТРИЯ СВЯЗЕЙ И ВЛИЯНИЯ", fill=COLOR_TEXT_DIM, font=f_metric_label)

    # Метрика 1: Индекс влияния
    inf_score = float(profile_data.get("influence_score", 0.0))
    draw.text((68, 222), "ИНДЕКС ВЛИЯНИЯ (PAGERANK)", fill=COLOR_TEXT_DIM, font=f_metric_label)
    draw.text((68, 240), f"{inf_score:.1f} / 10.0", fill=COLOR_WHITE, font=f_metric_value)
    _draw_segmented_bar(draw, 68, 276, 375, 8, ratio=inf_score / 10.0, segments=18, active_color=COLOR_NEON)

    # Метрика 2: Синхронизация / Зависимость
    neg_rate = float(profile_data.get("neglected_rate", 0.0))
    dep_rate = max(0.0, min(1.0, 1.0 - neg_rate)) * 100.0
    draw.text((68, 304), "ИНДЕКС ВЗАИМНОСТИ И СИНХРОНИЗАЦИИ", fill=COLOR_TEXT_DIM, font=f_metric_label)
    draw.text((68, 322), f"{dep_rate:.0f}%", fill=COLOR_WHITE, font=f_metric_value)
    _draw_segmented_bar(draw, 68, 358, 375, 8, ratio=dep_rate / 100.0, segments=18, active_color=COLOR_GOLD)

    # Метрика 3: Главная связь
    top_target = profile_data.get("top_targeted_user")
    target_text = f"@{top_target}" if top_target else "НЕТ [ИЗОЛЯЦИЯ]"
    draw.text((68, 386), "ГЛАВНАЯ ЦЕЛЬ ГРАВИТАЦИИ", fill=COLOR_TEXT_DIM, font=f_metric_label)
    draw.text((68, 404), target_text, fill=COLOR_CYAN, font=f_username)

    # Метрика 4: Социальная гравитация (Перевод статуса)
    raw_grav = str(profile_data.get("gravity_label", "BALANCED")).upper()
    if "ПРИТЯЖЕН" in raw_grav:
        grav_label = "ЦЕНТР ПРИТЯЖЕНИЯ"
        grav_color = COLOR_NEON
    elif "ИЩУЩ" in raw_grav or "ВНИМАНИ" in raw_grav:
        grav_label = "ИЩУЩИЙ ВНИМАНИЯ"
        grav_color = COLOR_ALERT
    else:
        grav_label = "СБАЛАНСИРОВАННЫЙ"
        grav_color = COLOR_WHITE

    draw.text((68, 452), "ОРБИТАЛЬНЫЙ СТАТУС В ГРУППЕ", fill=COLOR_TEXT_DIM, font=f_metric_label)
    draw.text((68, 470), grav_label, fill=grav_color, font=f_header)

    # Входящие / Исходящие
    in_d = profile_data.get("in_degree", 0)
    out_d = profile_data.get("out_degree", 0)
    draw.line([(68, 515), (460, 515)], fill=COLOR_BORDER, width=1)
    draw.text((68, 532), f"БАЛАНС: ВХОДЯЩИЕ [{in_d}]  /  ИСХОДЯЩИЕ [{out_d}]", fill=COLOR_TEXT_DIM, font=f_tag)

    # -----------------------------------------------------------------------
    # Правая матрица: Психологический диагноз
    # -----------------------------------------------------------------------
    draw.rectangle([(504, 172), (1150, 592)], fill=COLOR_PANEL_BG, outline=COLOR_BORDER, width=1)
    _draw_hud_brackets(draw, 504, 172, 1150, 592, length=10, color=COLOR_BORDER)

    draw.rectangle([(504, 172), (1150, 206)], fill=COLOR_PANEL_ALT)
    draw.text((520, 182), "ПСИХОЛОГИЧЕСКИЙ АУТОПСИЙ // НЕЙРОСЕТЕВОЙ АНАЛИЗ", fill=COLOR_TEXT_DIM, font=f_metric_label)

    # Заголовок диагноза
    draw.text((520, 224), "ВЕРДИКТ ПОВЕДЕНЧЕСКОГО СКАНИРОВАНИЯ:", fill=COLOR_GOLD, font=f_diag_title)

    # Текст диагноза
    diag = profile_data.get("diagnosis", "Психологическая телеметрия по данному узлу отсутствует.")
    body_lines = _wrap_text(diag, f_body, max_width=590 * RETINA_SCALE, draw=draw)

    cur_y = 260
    for line in body_lines:
        draw.text((520, cur_y), line, fill=COLOR_WHITE, font=f_body)
        cur_y += 26

    # Логи консоли терминала
    draw.line([(520, 480), (1130, 480)], fill=COLOR_BORDER, width=1)
    draw.text((520, 498), "ROOT@VULTURE:~# анализ_графа --режим=глубокий", fill=COLOR_NEON, font=f_tag)
    draw.text((520, 520), f"[OK] УЗЕЛ ОБНАРУЖЕН: @{username} ЗАФИКСИРОВАН В БАЗЕ", fill=COLOR_TEXT_DIM, font=f_tag)
    draw.text((520, 542), "[ВНИМАНИЕ] ЭКСПОРТ ДОСЬЕ АВТОРИЗОВАН БЕЗ ШИФРОВАНИЯ", fill=COLOR_ALERT, font=f_tag)

    # -----------------------------------------------------------------------
    # 5. Нижняя полоса водяного знака
    # -----------------------------------------------------------------------
    draw.rectangle([(32, 608), (1168, 648)], fill=COLOR_PANEL_ALT)
    draw.line([(32, 608), (1168, 608)], fill=COLOR_BORDER, width=1)

    clean_bot_tag = bot_username.lstrip("@")
    footer_text = f"СФОРМИРОВАНО БОТОМ @{clean_bot_tag} // КОНФИДЕНЦИАЛЬНО // РАСПРОСТРАНЕНИЕ ОГРАНИЧЕНО"
    draw.text((52, 622), footer_text, fill=COLOR_TEXT_DIM, font=f_tag)
    draw.text((1075, 622), "[СТАТУС: ОК]", fill=COLOR_NEON, font=f_tag)

    # Экспорт в байтовый буфер PNG
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True, compress_level=9)
    buf.seek(0)
    return buf


def _render_sync_card_sync(pair_data: dict, bot_username: str = "VultureBot") -> BytesIO:
    """Рендерит парную карточку анализа связи 2400x1350 (@2x).

    ВНИМАНИЕ: Это CPU-bound функция. Вызывать ТОЛЬКО через asyncio.to_thread().
    """
    w, h = 1200, 675
    img = Image.new("RGB", (w * RETINA_SCALE, h * RETINA_SCALE), COLOR_BG)
    draw = _RetinaDraw(img, RETINA_SCALE)

    # Фоновая сетка
    for x in range(30, w - 30, 45):
        draw.line([(x, 30), (x, h - 30)], fill=COLOR_GRID, width=1)
    for y in range(30, h - 30, 45):
        draw.line([(30, y), (w - 30, y)], fill=COLOR_GRID, width=1)

    draw.rectangle([(30, 24), (1170, 650)], fill=None, outline=COLOR_BORDER, width=2)
    _draw_hud_brackets(draw, 24, 18, 1176, 656, length=20, color=COLOR_CYAN)

    f_tag = _font("bold", 12, RETINA_SCALE)
    f_header = _font("bold", 16, RETINA_SCALE)
    f_user = _font("bold", 24, RETINA_SCALE)
    f_val = _font("bold", 32, RETINA_SCALE)
    f_body = _font("regular", 16, RETINA_SCALE)

    # Верхняя полоса
    draw.rectangle([(32, 26), (1168, 72)], fill=COLOR_PANEL_BG)
    draw.line([(32, 72), (1168, 72)], fill=COLOR_BORDER, width=1)
    draw.rectangle([(48, 38), (175, 60)], fill=(0, 30, 45), outline=COLOR_CYAN, width=1)
    draw.text((58, 42), "ДУЭЛЬНЫЙ АНАЛИЗ", fill=COLOR_CYAN, font=f_tag)
    draw.text((192, 42), "ПРОТОКОЛ СИНХРОНИЗАЦИИ УЗЛОВ // ПАРНАЯ ТЕЛЕМЕТРИЯ", fill=COLOR_WHITE, font=f_header)

    # Имена участников
    user_a = str(pair_data["user_a"])
    user_b = str(pair_data["user_b"])
    draw.text((80, 105), f"УЗЕЛ А: @{user_a}", fill=COLOR_CYAN, font=f_user)
    draw.text((750, 105), f"УЗЕЛ B: @{user_b}", fill=COLOR_GOLD, font=f_user)

    # Центральный индикатор резонанса
    draw.rectangle([(50, 160), (1150, 310)], fill=COLOR_PANEL_BG, outline=COLOR_BORDER, width=1)
    _draw_hud_brackets(draw, 50, 160, 1150, 310, length=12, color=COLOR_BORDER)

    sync_pct = float(pair_data.get("sync_percent", 0.0))
    draw.text((70, 180), "ИНДЕКС ВЗАИМНОЙ СИНХРОНИЗАЦИИ", fill=COLOR_TEXT_DIM, font=f_tag)
    draw.text((70, 205), f"{sync_pct:.1f}%", fill=COLOR_WHITE, font=f_val)
    _draw_segmented_bar(draw, 70, 255, 1010, 14, ratio=sync_pct / 100.0, segments=32, active_color=COLOR_NEON)

    # Баланс сил (A -> B vs B -> A)
    draw.rectangle([(50, 330), (585, 490)], fill=COLOR_PANEL_BG, outline=COLOR_BORDER, width=1)
    draw.text((70, 348), f"ИМПУЛЬС: @{user_a}  ➔  @{user_b}", fill=COLOR_TEXT_DIM, font=f_tag)
    draw.text((70, 380), f"{pair_data['weight_a_to_b']}", fill=COLOR_CYAN, font=f_val)
    draw.text((70, 435), "Вес направленных ответов и реакций", fill=COLOR_TEXT_DIM, font=f_body)

    draw.rectangle([(615, 330), (1150, 490)], fill=COLOR_PANEL_BG, outline=COLOR_BORDER, width=1)
    draw.text((635, 348), f"ИМПУЛЬС: @{user_b}  ➔  @{user_a}", fill=COLOR_TEXT_DIM, font=f_tag)
    draw.text((635, 380), f"{pair_data['weight_b_to_a']}", fill=COLOR_GOLD, font=f_val)
    draw.text((635, 435), "Вес встречных импульсов", fill=COLOR_TEXT_DIM, font=f_body)

    # Диагноз пары
    draw.rectangle([(50, 510), (1150, 595)], fill=COLOR_PANEL_ALT, outline=COLOR_BORDER, width=1)
    verdict = pair_data.get("verdict", "НЕОПРЕДЕЛЕНО")
    draw.text((70, 525), f"ВЕРДИКТ: {verdict}", fill=COLOR_NEON, font=f_header)
    desc = pair_data.get("description", "")
    draw.text((70, 555), desc, fill=COLOR_WHITE, font=f_body)

    # Футер
    draw.rectangle([(32, 608), (1168, 648)], fill=COLOR_PANEL_ALT)
    draw.text((52, 622), f"СФОРМИРОВАНО @{bot_username.lstrip('@')} // СОВЕРШЕННО СЕКРЕТНО", fill=COLOR_TEXT_DIM, font=f_tag)
    draw.text((1060, 622), "[РЕЗОНАНС]", fill=COLOR_CYAN, font=f_tag)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True, compress_level=9)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Асинхронные обёртки (публичный API модуля)
# ---------------------------------------------------------------------------

async def render_dossier(
    profile_data: dict,
    bot_username: str = "VultureBot",
) -> BytesIO:
    """Async wrapper: offloads CPU-heavy Pillow render to a thread pool."""
    await ensure_fonts()
    return await asyncio.to_thread(_render_dossier_sync, profile_data, bot_username)


async def render_sync_card(pair_data: dict, bot_username: str = "VultureBot") -> BytesIO:
    """Async wrapper: offloads CPU-heavy Pillow render to a thread pool."""
    await ensure_fonts()
    return await asyncio.to_thread(_render_sync_card_sync, pair_data, bot_username)