"""Dark cyberpunk dossier-card renderer (Pillow).

Generates a 1200 × 675 PNG with JetBrains Mono typography, a matte-black
grid background, and neon-accent metrics layout.  Fonts are auto-downloaded
from GitHub on first use.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & URLs
# ---------------------------------------------------------------------------

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

FONT_FILES = {
    "regular": "JetBrainsMono-Regular.ttf",
    "bold": "JetBrainsMono-Bold.ttf",
}

_GITHUB_BASE = (
    "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/ttf"
)
FONT_URLS: dict[str, str] = {
    name: f"{_GITHUB_BASE}/{name}" for name in FONT_FILES.values()
}

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

BG = "#0D0D11"
GRID = "#1A1A2E"
ACCENT = "#00FFAA"
WHITE = "#E0E0E0"
DIM = "#4A4A5A"
DARK_ACCENT = "#003322"

# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

async def ensure_fonts() -> None:
    """Download JetBrains Mono Regular + Bold if not already present."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in FONT_URLS.items():
        filepath = ASSETS_DIR / filename
        if filepath.exists():
            continue
        try:
            logger.info("Downloading font: %s", filename)
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                filepath.write_bytes(resp.content)
                logger.info("Font saved: %s", filepath)
        except Exception as exc:
            logger.warning("Font download failed (%s): %s", filename, exc)


def _load_font(style: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a bundled font; fall back to PIL's built-in bitmap font."""
    path = ASSETS_DIR / FONT_FILES.get(style, FONT_FILES["regular"])
    try:
        return ImageFont.truetype(str(path), size)
    except (OSError, IOError):
        logger.warning("Font unavailable (%s) – falling back to default", path.name)
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _draw_grid(draw: ImageDraw.ImageDraw, w: int, h: int) -> None:
    """Subtle 40 px grid overlay."""
    for x in range(0, w, 40):
        draw.line([(x, 0), (x, h)], fill=GRID, width=1)
    for y in range(0, h, 40):
        draw.line([(0, y), (w, y)], fill=GRID, width=1)


def _wrap(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
          max_w: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Word-wrap *text* to fit inside *max_w* pixels."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        test = f"{cur} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def _bar(draw: ImageDraw.ImageDraw, x: int, y: int, w: int,
         value: float, max_val: float) -> None:
    """Horizontal progress bar."""
    h = 8
    draw.rectangle([(x, y), (x + w, y + h)], fill=DARK_ACCENT)
    fill_w = int((value / max_val) * w) if max_val > 0 else 0
    fill_w = max(0, min(fill_w, w))
    if fill_w:
        draw.rectangle([(x, y), (x + fill_w, y + h)], fill=ACCENT)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def render_dossier(
    profile_data: dict,
    bot_username: str = "VultureBot",
) -> BytesIO:
    """Render a 1200 × 675 cyberpunk dossier card and return a PNG buffer.

    Parameters
    ----------
    profile_data : dict
        Merged user-metrics + profiler output.  Expected keys:
        ``username``, ``influence_score``, ``neglected_rate``,
        ``top_targeted_user``, ``rank_title``, ``diagnosis``,
        ``gravity_label``, ``in_degree``, ``out_degree``.
    bot_username : str
        Dynamically fetched Telegram bot username for the watermark.
    """
    await ensure_fonts()

    W, H = 1200, 675
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    _draw_grid(draw, W, H)

    # Fonts
    f_header = _load_font("bold", 22)
    f_title = _load_font("bold", 36)
    f_label = _load_font("regular", 16)
    f_value = _load_font("bold", 28)
    f_body = _load_font("regular", 18)
    f_wm = _load_font("regular", 12)

    # ---- top accent stripe ------------------------------------------------
    draw.rectangle([(0, 0), (W, 3)], fill=ACCENT)

    # ---- header -----------------------------------------------------------
    uname = profile_data.get("username", "unknown")
    draw.text((40, 25), f"[ VULTURE DOSSIER // SUBJECT: @{uname} ]",
              fill=ACCENT, font=f_header)
    draw.line([(40, 60), (W - 40, 60)], fill=GRID, width=2)

    # ---- rank title -------------------------------------------------------
    rank = profile_data.get("rank_title", "UNCLASSIFIED")
    draw.text((40, 80), rank.upper(), fill=ACCENT, font=f_title)

    # ---- left column: metrics ---------------------------------------------
    y = 145

    # Influence Index
    inf = profile_data.get("influence_score", 0.0)
    draw.text((40, y), "INFLUENCE INDEX", fill=DIM, font=f_label)
    y += 22
    draw.text((40, y), f"{inf:.1f} / 10.0", fill=WHITE, font=f_value)
    y += 38
    _bar(draw, 40, y, 300, inf, 10.0)
    y += 30

    # Dependency Rate (100 – neglect %)
    neg = profile_data.get("neglected_rate", 0.0)
    dep = (1.0 - neg) * 100
    draw.text((40, y), "DEPENDENCY RATE", fill=DIM, font=f_label)
    y += 22
    draw.text((40, y), f"{dep:.0f}%", fill=WHITE, font=f_value)
    y += 38
    _bar(draw, 40, y, 300, dep, 100.0)
    y += 30

    # Top Affinity
    aff = profile_data.get("top_targeted_user") or "None"
    draw.text((40, y), "TOP AFFINITY", fill=DIM, font=f_label)
    y += 22
    draw.text((40, y), f"@{aff}", fill=WHITE, font=f_value)
    y += 45

    # Social Gravity
    grav = profile_data.get("gravity_label", "Unknown")
    draw.text((40, y), "SOCIAL GRAVITY", fill=DIM, font=f_label)
    y += 22
    draw.text((40, y), grav, fill=WHITE, font=f_value)

    # ---- right column: degree stats ---------------------------------------
    in_d = profile_data.get("in_degree", 0)
    out_d = profile_data.get("out_degree", 0)
    draw.text((420, 145), "INBOUND", fill=DIM, font=f_label)
    draw.text((420, 167), str(in_d), fill=WHITE, font=f_value)
    draw.text((540, 145), "OUTBOUND", fill=DIM, font=f_label)
    draw.text((540, 167), str(out_d), fill=WHITE, font=f_value)

    draw.line([(420, 215), (W - 40, 215)], fill=GRID, width=1)

    # ---- diagnosis --------------------------------------------------------
    draw.text((420, 230), "PSYCHOLOGICAL AUTOPSY", fill=DIM, font=f_label)
    diag = profile_data.get("diagnosis", "No analysis available.")
    lines = _wrap(diag, f_body, W - 420 - 60, draw)
    dy = 258
    for ln in lines:
        draw.text((420, dy), ln, fill=WHITE, font=f_body)
        dy += 26

    # ---- bottom accent stripe + watermark ---------------------------------
    draw.rectangle([(0, H - 3), (W, H)], fill=ACCENT)
    draw.text(
        (40, H - 30),
        f"Generated by @{bot_username} | Confidential",
        fill=DIM,
        font=f_wm,
    )

    # Decorative corner bracket (top-right)
    draw.line([(W - 60, 25), (W - 40, 25)], fill=ACCENT, width=2)
    draw.line([(W - 40, 25), (W - 40, 45)], fill=ACCENT, width=2)

    # ---- export -----------------------------------------------------------
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
