"""
branding.py — identidad visual de Limpieza Total Omega.

Centraliza la gestión de activos visuales, paletas de colores, jerarquías 
tipográficas y sistemas de renderizado vectorial (SVG/Canvas).
"""

from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Final, TypeAlias, Literal, Mapping, Tuple, List, Optional, Union, TypedDict, Protocol, NamedTuple
from enum import Enum, auto
from types import MappingProxyType
from functools import lru_cache
from safety import ensure_safe_to_modify, is_safe_to_modify, is_protected_path, filter_safe_paths
import math

# --- Tipos de Datos Semánticos ---
ColorHex: TypeAlias = str  
GradeKey: TypeAlias = Literal["A", "B", "C", "D", "F"]
SeverityStyle: TypeAlias = Tuple[ColorHex, str]  
RGBTuple: TypeAlias = Tuple[int, int, int]
CoordinateSequence: TypeAlias = Tuple[float, ...]
BoundingBox: TypeAlias = Tuple[float, float, float, float]
StopPoint: TypeAlias = Tuple[float, ColorHex]
AnchorPoint: TypeAlias = Literal["center", "n", "s", "e", "w", "nw", "ne", "sw", "se"]
StripeParams: TypeAlias = Tuple[float, float, float]

# --- Constantes de Layout y Renderizado ---
_MIN_SCALE: Final[float] = 0.1
_MAX_SCALE: Final[float] = 10.0
_DEFAULT_SVG_SIZE: Final[int] = 128

SHIELD_WIDTH: Final[float] = 128.0
SHIELD_CENTER_X: Final[float] = 64.0
SHIELD_CENTER_Y: Final[float] = 56.0
SHIELD_RADIUS: Final[float] = 52.0

SHIELD_BASE_COORDS: Final[CoordinateSequence] = (64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31)

_DECOR_LINE_X1: Final[float] = 41.0
_DECOR_LINE_Y1: Final[float] = 75.0
_DECOR_LINE_X2: Final[float] = 75.0
_DECOR_LINE_Y2: Final[float] = 41.0
_DECOR_TRIANGLE_X3: Final[float] = 89.0
_DECOR_TRIANGLE_Y3: Final[float] = 38.0
_DECOR_TRIANGLE_X4: Final[float] = 92.0
_DECOR_TRIANGLE_Y4: Final[float] = 52.0
_DECOR_STROKE_WIDTH: Final[float] = 8.0

class UIConfiguration(NamedTuple):
    font_family: str = "Segoe UI"
    header_size: int = 23
    body_size: int = 12
    bold_weight: str = "bold"

UI_CONFIG: Final[UIConfiguration] = UIConfiguration()

class Point(NamedTuple):
    x: float
    y: float

class SeverityType(Enum):
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"

_SVG_GRADIENT_STOPS: Final[str] = (
    '      <stop offset="0%" stop-color="#00f0c0"/>\n'
    '      <stop offset="55%" stop-color="#7c5cff"/>\n'
    '      <stop offset="100%" stop-color="#ff2d78"/>'
)

APP_NAME: Final[str] = "Limpieza Total Omega"
APP_SHORT_NAME: Final[str] = "Omega"
APP_TAGLINE: Final[str] = "Limpieza y seguridad, en un solo lugar"
APP_VERSION: Final[str] = "2.1.0"

class PaletteDict(TypedDict):
    background: ColorHex; surface: ColorHex; surface_alt: ColorHex; surface_hover: ColorHex
    card: ColorHex; accent: ColorHex; accent_hover: ColorHex; accent_dim: ColorHex
    accent2: ColorHex; accent2_hover: ColorHex; accent3: ColorHex; success: ColorHex
    info: ColorHex; warning: ColorHex; danger: ColorHex; danger_hover: ColorHex
    text: ColorHex; text_muted: ColorHex; text_dim: ColorHex; border: ColorHex; glow: ColorHex

_RAW_PALETTE: Final[PaletteDict] = {
    "background": "#0a0e17", "surface": "#141b2d", "surface_alt": "#1e2740",
    "surface_hover": "#28324f", "card": "#182135", "accent": "#00f0c0",
    "accent_hover": "#00d0a4", "accent_dim": "#0a6b58", "accent2": "#7c5cff",
    "accent2_hover": "#6a48f0", "accent3": "#ff2d78", "success": "#22e39a",
    "info": "#38bdf8", "warning": "#ffb020", "danger": "#ff4757",
    "danger_hover": "#e02e3d", "text": "#f0f6fc", "text_muted": "#94a3b8",
    "text_dim": "#5c6b85", "border": "#2a3654", "glow": "#00f0c0",
}

PALETTE: Final[Mapping[str, ColorHex]] = MappingProxyType(_RAW_PALETTE)

C_BACKGROUND: Final[ColorHex] = _RAW_PALETTE["background"]
C_SURFACE: Final[ColorHex] = _RAW_PALETTE["surface"]
C_SURFACE_ALT: Final[ColorHex] = _RAW_PALETTE["surface_alt"]
C_ACCENT: Final[ColorHex] = _RAW_PALETTE["accent"]
C_ACCENT2: Final[ColorHex] = _RAW_PALETTE["accent2"]
C_ACCENT3: Final[ColorHex] = _RAW_PALETTE["accent3"]
C_SUCCESS: Final[ColorHex] = _RAW_PALETTE["success"]
C_INFO: Final[ColorHex] = _RAW_PALETTE["info"]
C_WARNING: Final[ColorHex] = _RAW_PALETTE["warning"]
C_DANGER: Final[ColorHex] = _RAW_PALETTE["danger"]
C_TEXT: Final[ColorHex] = _RAW_PALETTE["text"]
C_TEXT_MUTED: Final[ColorHex] = _RAW_PALETTE["text_muted"]
C_GLOW: Final[ColorHex] = _RAW_PALETTE["glow"]

_SVG_TEMPLATE: Final[str] = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{{s}}" height="{{s}}" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="omegaShield" x1="0" y1="0" x2="1" y2="1">{{stops}}</linearGradient>
    <radialGradient id="omegaGlow" cx="0.5" cy="0.4" r="0.6">
      <stop offset="0%" stop-color="{C_GLOW}" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="{C_GLOW}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="128" height="128" rx="30" fill="{C_SURFACE}"/>
  <circle cx="64" cy="56" r="52" fill="url(#omegaGlow)"/>
  <path d="M64 18 L100 31 V67 C100 90 83 104 64 110 C45 104 28 90 28 67 V31 Z" fill="url(#omegaShield)"/>
  <path d="M41 75 L75 41" stroke="{C_SURFACE}" stroke-width="8" stroke-linecap="round"/>
  <path d="M75 41 L89 38 L92 52 Z" fill="{C_SURFACE}"/>
  <text x="64" y="98" font-family="{UI_CONFIG.font_family}" font-size="26" font-weight="bold" fill="{C_SURFACE}" text-anchor="middle">&#937;</text>
</svg>"""

class CanvasElement(Protocol):
    def create_rectangle(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., outline: str = ..., width: int = ...) -> int: ...
    def create_polygon(self, *args: float, fill: str = ..., outline: str = ...) -> int: ...
    def create_oval(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., outline: str = ...) -> int: ...
    def create_line(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., width: int = ..., capstyle: str = ...) -> int: ...
    def create_text(self, x: float, y: float, *, text: str, fill: str = ..., font: Union[Tuple[str, int, str], str] = ..., anchor: str = ...) -> int: ...
    def create_arc(self, x0: float, y0: float, x1: float, y1: float, *, start: float, extent: float, style: str = ..., outline: str = ..., width: int = ...) -> int: ...

class ColorSegment(NamedTuple):
    hex_color: ColorHex
    start_index: int
    end_index: int

class FontSizesDict(TypedDict):
    display: int; title: int; subtitle: int; heading: int; body: int; mono: int; caption: int

FONT_SIZES: Final[Mapping[str, int]] = MappingProxyType({
    "display": 46, "title": 26, "subtitle": 13, "heading": 16,
    "body": UI_CONFIG.body_size, "mono": 11, "caption": 10,
})

SEVERITY_STYLES: Final[Mapping[SeverityType, SeverityStyle]] = MappingProxyType({
    SeverityType.OK: (C_SUCCESS, "Correcto"),
    SeverityType.INFO: (C_INFO, "Informativo"),
    SeverityType.WARNING: (C_WARNING, "Advertencia"),
    SeverityType.DANGER: (C_DANGER, "Peligro"),
})

GRADE_COLORS: Final[Mapping[str, ColorHex]] = MappingProxyType({
    "A": C_SUCCESS, "B": C_INFO, "C": C_WARNING, "D": "#ff7b39", "F": C_DANGER,
})

ICONS: Final[Mapping[str, str]] = MappingProxyType({
    "Salud": "\u25c9", "Limpieza": "\u2726", "Seguridad": "\u26ca",
    "Cuarentena": "\u2297", "Memoria": "\u25a4", "Disco": "\u25f4",
    "Duplicados": "\u29c9", "Navegadores": "\u25d0", "Inicio": "\u23fb",
    "Informe": "\u2263", "Asistente": "\u273b", "Ajustes": "\u2699",
})

GRADIENT_STOPS: Final[Tuple[ColorHex, ...]] = (C_ACCENT, C_ACCENT2, C_ACCENT3)
SCORE_THRESHOLDS: Final[Tuple[StopPoint, ...]] = (
    (90.0, C_SUCCESS), (80.0, C_INFO), (65.0, C_WARNING), (50.0, "#ff7b39")
)
SEVERITY_MAP: Final[Mapping[SeverityType, str]] = MappingProxyType({
    SeverityType.OK: "\u2713", SeverityType.INFO: "\u2139", 
    SeverityType.WARNING: "\u26a0", SeverityType.DANGER: "\u2716"
})

# --- Funciones de Acceso a Metadatos ---
def app_title() -> str: 
    """Retorna el título formateado de la aplicación con su versión actual."""
    return f"{APP_NAME} v{APP_VERSION}"

def color(name: Optional[str]) -> ColorHex: 
    """Obtiene un código de color hexadecimal por su nombre clave; retorna gris por defecto."""
    return PALETTE.get(name, "#808080") if name else "#808080"

def font_size(name: Optional[str]) -> int: 
    """Retorna el tamaño de fuente definido para una categoría específica."""
    return FONT_SIZES.get(name, UI_CONFIG.body_size) if name else UI_CONFIG.body_size

def icon(section: Optional[str]) -> str: 
    """Devuelve el carácter Unicode representativo de una sección de la interfaz."""
    return ICONS.get(section, "\u2022") if section else "\u2022"

def tab_label(section: Optional[str]) -> str: 
    """Genera el texto formateado para etiquetas de pestañas (icono + nombre)."""
    return f"{icon(section)}  {section}" if section else "\u2022  Desconocido"

def _parse_severity(severity: Optional[str]) -> Optional[SeverityType]:
    """Convierte un string a un enum de severidad, validando su formato."""
    if not isinstance(severity, str) or not severity:
        return None
    try: return SeverityType(severity.strip().lower())
    except (ValueError, AttributeError): return None

@lru_cache(maxsize=16)
def _get_severity_style(severity_key: Optional[SeverityType]) -> SeverityStyle:
    """Obtiene el estilo (color y etiqueta) asociado a un nivel de severidad."""
    return SEVERITY_STYLES.get(severity_key, (C_TEXT_MUTED, "Desconocido"))

def severity_color(severity: Optional[str]) -> ColorHex: 
    """Retorna el color hexadecimal asociado a un string de severidad."""
    return _get_severity_style(_parse_severity(severity))[0]

def severity_label(severity: Optional[str]) -> str: 
    """Retorna la etiqueta legible para humanos de un string de severidad."""
    return _get_severity_style(_parse_severity(severity))[1]

def severity_icon(severity: Optional[str]) -> str: 
    """Retorna el icono de advertencia asociado a un string de severidad."""
    return SEVERITY_MAP.get(_parse_severity(severity), "\u2022")

def grade_color(grade: Optional[str]) -> ColorHex: 
    """Retorna el color asignado a una calificación (A-F)."""
    return GRADE_COLORS.get(grade[0].upper(), C_TEXT_MUTED) if grade else C_TEXT_MUTED

@lru_cache(maxsize=128)
def score_color(score: Union[float, int, None]) -> ColorHex:
    """Retorna el color asociado a un puntaje numérico basado en los umbrales definidos."""
    if score is None: return C_TEXT_MUTED
    try:
        valor = float(score)
        if not math.isfinite(valor) or not (0.0 <= valor <= 100.0): return C_TEXT_MUTED
        for limit, color_val in SCORE_THRESHOLDS:
            if valor >= limit: return color_val
        return C_DANGER
    except (TypeError, ValueError): return C_TEXT_MUTED

@lru_cache(maxsize=128)
def bar(percent: Union[float, int, None], width: int = 24, filled: str = "\u2588", empty: str = "\u2591") -> str:
    """Genera una cadena con una barra de progreso visual basada en caracteres Unicode."""
    try:
        valor = float(percent) if percent is not None else 0.0
        ancho = max(1, int(width))
        if not math.isfinite(valor): return empty * ancho
        llenos = int(round(max(0.0, min(100.0, valor)) / 100 * ancho))
        return (filled * llenos) + (empty * (ancho - llenos))
    except (TypeError, ValueError): return empty * max(1, int(width))

@lru_cache(maxsize=256)
def _hex_to_rgb(value: ColorHex) -> RGBTuple:
    """Convierte un string hexadecimal CSS (formato #RRGGBB) a una tupla RGB."""
    if not isinstance(value, str) or not value.startswith('#') or len(value) != 7: return (0, 0, 0)
    try:
        val = int(value.lstrip('#'), 16)
        return ((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF)
    except (ValueError, TypeError): return (0, 0, 0)

@lru_cache(maxsize=256)
def _rgb_to_hex(rgb: RGBTuple) -> ColorHex:
    """Convierte una tupla RGB a su representación en string hexadecimal CSS."""
    return f"#{max(0, min(255, int(rgb[0]))):02x}{max(0, min(255, int(rgb[1]))):02x}{max(0, min(255, int(rgb[2]))):02x}"

def blend(start: ColorHex, end: ColorHex, ratio: float) -> ColorHex:
    """Realiza una interpolación lineal (mezcla) entre dos colores hexadecimales."""
    try:
        r1, g1, b1 = _hex_to_rgb(start)
        r2, g2, b2 = _hex_to_rgb(end)
        r = max(0.0, min(1.0, float(ratio)))
        return _rgb_to_hex((int(r1 + (r2 - r1) * r), int(g1 + (g2 - g1) * r), int(b1 + (b2 - b1) * r)))
    except (TypeError, ValueError): return start

@lru_cache(maxsize=64)
def gradient_colors(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorHex, ...]:
    """Calcula una secuencia de colores interpolados a través de múltiples puntos de control."""
    n = max(1, int(steps))
    n_stops = len(stops)
    if n_stops < 2: return (stops[0],) * n
    rgb_stops = [_hex_to_rgb(s) for s in stops]
    res = []
    for i in range(n):
        pos = (i * (n_stops - 1)) / (n - 1) if n > 1 else 0
        idx = min(int(pos), n_stops - 2)
        r = pos - idx
        s1, s2 = rgb_stops[idx], rgb_stops[idx + 1]
        res.append(_rgb_to_hex((int(s1[0] + (s2[0] - s1[0]) * r), int(s1[1] + (s2[1] - s1[1]) * r), int(s1[2] + (s2[2] - s1[2]) * r))))
    return tuple(res)

@lru_cache(maxsize=64)
def get_gradient_segments(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorSegment, ...]:
    """Optimiza llamadas al motor gráfico agrupando colores contiguos en segmentos."""
    colors = gradient_colors(steps, stops)
    if not colors: return ()
    segments: List[ColorSegment] = []
    start = 0
    for i in range(1, len(colors)):
        if colors[i] != colors[start]:
            segments.append(ColorSegment(colors[start], start, i))
            start = i
    segments.append(ColorSegment(colors[start], start, len(colors)))
    return tuple(segments)

@lru_cache(maxsize=128)
def _get_scaled_poly(scale: float, x: float, y: float) -> CoordinateSequence:
    """Escala las coordenadas base del escudo a una posición y tamaño dados."""
    if not math.isfinite(scale) or scale <= 0 or not math.isfinite(x) or not math.isfinite(y): return ()
    n = len(SHIELD_BASE_COORDS)
    res = [0.0] * n
    for i in range(0, n, 2):
        res[i] = x + (SHIELD_BASE_COORDS[i] * scale)
        res[i+1] = y + (SHIELD_BASE_COORDS[i+1] * scale)
    return tuple(res)

def logo_svg(size: int = _DEFAULT_SVG_SIZE) -> str:
    """Genera el contenido del archivo SVG del logo (formato string)."""
    s = max(16, min(1024, int(size)))
    return _SVG_TEMPLATE.format(s=s, stops=_SVG_GRADIENT_STOPS)

def save_logo_svg(destination: Union[str, Path, None], size: int = _DEFAULT_SVG_SIZE) -> Optional[Path]:
    """Guarda el logo vectorial en una ruta especificada, previa validación de seguridad."""
    if destination is None: return None
    try:
        dest_path = Path(destination).resolve()
        # Valida escritura forzando el check de seguridad mediante excepción
        ensure_safe_to_modify(dest_path)
        
        parent = dest_path.parent
        if not parent.exists(): parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(logo_svg(size), encoding="utf-8")
        return dest_path
    except Exception:
        # La seguridad es prioritaria; cualquier error impide la escritura
        return None

_ASCII_LOGO: Final[str] = (
    "   ___  __  __ ___ ___   _\n"
    "  / _ \\|  \\/  | __/ __| /_\\\n"
    " | (_) | |\\/| | _|| (_ // _ \\\n"
    "  \\___/|_|  |_|___\\___/_/ \\_\\\n"
    "      Limpieza Total Omega\n"
)

def logo_ascii() -> str: 
    """Retorna el logo de la aplicación en arte ASCII."""
    return _ASCII_LOGO

STRIPE_THICKNESS_SCALE: Final[float] = 92.0
STRIPE_COUNT_FACTOR: Final[float] = 28.0
STRIPE_BASE_Y_OFFSET: Final[float] = 18.0

@lru_cache(maxsize=16)
def _get_stripe_params(scale: float, n: int) -> Tuple[StripeParams, ...]:
    """Calcula los parámetros geométricos para las franjas decorativas del escudo."""
    if not math.isfinite(scale) or scale <= 0 or n < 1: return ()
    tf = STRIPE_THICKNESS_SCALE * scale
    params = []
    step = tf / n
    for i in range(n):
        rel = i / (n - 1) if n > 1 else 0
        thick = tf * (1.0 if rel < 0.55 else 1.0 - ((rel - 0.55) * 1.9))
        params.append((thick, i * step, (i + 1) * step))
    return tuple(params)

def _draw_shield_stripes(canvas: CanvasElement, x: float, y: float, scale: float) -> None:
    """Dibuja las franjas degradadas sobre el objeto Canvas recibido."""
    try:
        n = max(6, int(STRIPE_COUNT_FACTOR * scale))
        params = _get_stripe_params(scale, n)
        by, cx = y + STRIPE_BASE_Y_OFFSET * scale, x + SHIELD_CENTER_X * scale
        for seg in get_gradient_segments(n):
            for i in range(seg.start_index, seg.end_index):
                if i < len(params):
                    p = params[i]
                    canvas.create_rectangle(cx - p[0], by + p[1], cx + p[0], by + p[2], fill=seg.hex_color, outline="")
    except (AttributeError, TypeError, ValueError, IndexError): pass

def _draw_shield_icon_decorations(canvas: CanvasElement, x: float, y: float, scale: float) -> None:
    """Renderiza elementos decorativos (líneas, triángulos, logo central) sobre el escudo."""
    try:
        ts = max(8, int(UI_CONFIG.header_size * scale))
        canvas.create_line(
            x + _DECOR_LINE_X1 * scale, y + _DECOR_LINE_Y1 * scale,
            x + _DECOR_LINE_X2 * scale, y + _DECOR_LINE_Y2 * scale,
            fill=C_BACKGROUND, width=max(2, int(_DECOR_STROKE_WIDTH * scale)), capstyle="round"
        )
        canvas.create_polygon(
            x + _DECOR_LINE_X2 * scale, y + _DECOR_LINE_Y2 * scale,
            x + _DECOR_TRIANGLE_X3 * scale, y + _DECOR_TRIANGLE_Y3 * scale,
            x + _DECOR_TRIANGLE_X4 * scale, y + _DECOR_TRIANGLE_Y4 * scale,
            fill=C_BACKGROUND, outline=""
        )
        canvas.create_text(
            x + SHIELD_CENTER_X * scale, y + (SHIELD_CENTER_Y + 40) * scale,
            text="\u03a9", fill=C_BACKGROUND,
            font=(UI_CONFIG.font_family, ts, UI_CONFIG.bold_weight), anchor="center"
        )
    except (AttributeError, TypeError, ValueError): pass

def draw_logo(canvas: CanvasElement, size: float = 56.0, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    """Función principal para dibujar el logo complejo compuesto en un widget Canvas."""
    if canvas is None or not math.isfinite(size) or not math.isfinite(canvas_x) or not math.isfinite(canvas_y): return
    try:
        s = max(_MIN_SCALE, min(_MAX_SCALE, float(size) / SHIELD_WIDTH))
        poly = _get_scaled_poly(s, float(canvas_x), float(canvas_y))
        if not poly: return
        offset = 75.0 * s
        canvas.create_oval(
            canvas_x + (SHIELD_CENTER_X * s) - offset, canvas_y + (SHIELD_CENTER_Y * s) - offset,
            canvas_x + (SHIELD_CENTER_X * s) + offset, canvas_y + (SHIELD_CENTER_Y * s) + offset,
            fill=blend(C_SURFACE, C_GLOW, 0.15), outline=""
        )
        canvas.create_polygon(*poly, fill=GRADIENT_STOPS[1], outline="")
        _draw_shield_stripes(canvas, canvas_x, canvas_y, s)
        _draw_shield_icon_decorations(canvas, canvas_x, canvas_y, s)
    except (AttributeError, TypeError, ValueError, ZeroDivisionError): pass

def draw_gradient_bar(canvas: CanvasElement, width: int, height: int = 3, canvas_x: float = 0.0, canvas_y: float = 0.0, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> None:
    """Renderiza una línea horizontal con un degradado lineal personalizado."""
    if canvas is None or not isinstance(width, int): return
    try:
        for s in get_gradient_segments(max(1, width), stops):
            canvas.create_line(canvas_x + s.start_index, canvas_y, canvas_x + s.end_index, canvas_y, fill=s.hex_color, width=height)
    except (AttributeError, TypeError, ValueError): pass

def draw_ring(canvas: CanvasElement, percent: Union[float, int, None], size: int = 150, canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14, track: Optional[ColorHex] = None, fill: Optional[ColorHex] = None) -> None:
    """Dibuja un anillo de progreso circular para métricas de salud o rendimiento."""
    if canvas is None or percent is None: return
    try:
        val = max(0.0, min(100.0, float(percent)))
        # Validar parámetros de tamaño y grosor
        if not math.isfinite(val): return
        d = max(20, min(2048, int(size)))
        thickness = max(1, int(thickness))
        pad = thickness / 2.0
        bb = (float(canvas_x) + pad, float(canvas_y) + pad, float(canvas_x) + d - pad, float(canvas_y) + d - pad)
        canvas.create_arc(*bb, start=0.0, extent=359.9, style="arc", outline=track or C_SURFACE_ALT, width=thickness)
        if val > 0: canvas.create_arc(*bb, start=90.0, extent=-(val / 100.0 * 359.9), style="arc", outline=fill or score_color(val), width=thickness)
    except (AttributeError, TypeError, ValueError): pass
