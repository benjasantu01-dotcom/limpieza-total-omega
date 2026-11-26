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
# Coordenadas planas: (x1, y1, x2, y2, ...) para métodos de Tkinter Canvas
CoordinateSequence: TypeAlias = Tuple[float, ...]
# Rectángulos definidos como (x0, y0, x1, y1) para bounding boxes de Canvas
BoundingBox: TypeAlias = Tuple[float, float, float, float]
StopPoint: TypeAlias = Tuple[float, ColorHex]
AnchorPoint: TypeAlias = Literal["center", "n", "s", "e", "w", "nw", "ne", "sw", "se"]
# StripeParams: (grosor de la línea, coordenada Y inicial, coordenada Y final)
StripeParams: TypeAlias = Tuple[float, float, float] 

# --- Constantes de Layout y Renderizado ---
MIN_SCALE_FACTOR: Final[float] = 0.1
MAX_SCALE_FACTOR: Final[float] = 10.0
DEFAULT_SVG_SIZE: Final[int] = 128

# El escudo se normaliza en una cuadrícula de 128x128 unidades abstractas.
SHIELD_WIDTH: Final[float] = 128.0
SHIELD_CENTER_X: Final[float] = 64.0
SHIELD_CENTER_Y: Final[float] = 56.0
SHIELD_GLOW_OFFSET: Final[float] = 75.0

# Vértices base del contorno del escudo en coordenadas abstractas (0-128)
SHIELD_BASE_COORDS: Final[CoordinateSequence] = (
    64.0, 18.0, 100.0, 31.0, 100.0, 67.0, 90.0, 90.0, 
    64.0, 110.0, 38.0, 90.0, 28.0, 67.0, 28.0, 31.0
)

# Constantes de posicionamiento para los elementos decorativos internos (Omega, líneas)
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
    """Parámetros de estilo tipográfico para la interfaz."""
    font_family: str = "Segoe UI"
    header_size: int = 23
    body_size: int = 12
    bold_weight: str = "bold"

UI_CONFIG: Final[UIConfiguration] = UIConfiguration()

class Point(NamedTuple):
    x: float
    y: float

class SeverityType(Enum):
    """Niveles de criticidad mapeados a estilos de color en la UI."""
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
    """Estructura esperada del esquema de colores."""
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

_SVG_TEMPLATE_BASE: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 128 128">\n'
    '  <defs>\n'
    '    <linearGradient id="omegaShield" x1="0" y1="0" x2="1" y2="1">{stops}</linearGradient>\n'
    f'    <radialGradient id="omegaGlow" cx="0.5" cy="0.4" r="0.6">\n'
    f'      <stop offset="0%" stop-color="{PALETTE["glow"]}" stop-opacity="0.45"/>\n'
    f'      <stop offset="100%" stop-color="{PALETTE["glow"]}" stop-opacity="0"/>\n'
    '    </radialGradient>\n'
    '  </defs>\n'
    f'  <rect width="128" height="128" rx="30" fill="{PALETTE["surface"]}"/>\n'
    '  <circle cx="64" cy="56" r="52" fill="url(#omegaGlow)"/>\n'
    '  <path d="M64 18 L100 31 V67 C100 90 83 104 64 110 C45 104 28 90 28 67 V31 Z" fill="url(#omegaShield)"/>\n'
    f'  <path d="M41 75 L75 41" stroke="{PALETTE["surface"]}" stroke-width="8" stroke-linecap="round"/>\n'
    f'  <path d="M75 41 L89 38 L92 52 Z" fill="{PALETTE["surface"]}"/>\n'
    f'  <text x="64" y="98" font-family="{UI_CONFIG.font_family}" font-size="26" font-weight="bold" fill="{PALETTE["surface"]}" text-anchor="middle">&#937;</text>\n'
    '</svg>'
)

class CanvasElement(Protocol):
    def create_rectangle(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., outline: str = ..., width: int = ...) -> int: ...
    def create_polygon(self, *coords: float, fill: str = ..., outline: str = ...) -> int: ...
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
    SeverityType.OK: (PALETTE["success"], "Correcto"),
    SeverityType.INFO: (PALETTE["info"], "Informativo"),
    SeverityType.WARNING: (PALETTE["warning"], "Advertencia"),
    SeverityType.DANGER: (PALETTE["danger"], "Peligro"),
})

GRADE_COLORS: Final[Mapping[str, ColorHex]] = MappingProxyType({
    "A": PALETTE["success"], "B": PALETTE["info"], "C": PALETTE["warning"], "D": "#ff7b39", "F": PALETTE["danger"],
})

ICONS: Final[Mapping[str, str]] = MappingProxyType({
    "Salud": "\u25c9", "Limpieza": "\u2726", "Seguridad": "\u26ca",
    "Cuarentena": "\u2297", "Memoria": "\u25a4", "Disco": "\u25f4",
    "Duplicados": "\u29c9", "Navegadores": "\u25d0", "Inicio": "\u23fb",
    "Informe": "\u2263", "Asistente": "\u273b", "Ajustes": "\u2699",
})

GRADIENT_STOPS: Final[Tuple[ColorHex, ...]] = (PALETTE["accent"], PALETTE["accent2"], PALETTE["accent3"])
SCORE_THRESHOLDS: Final[Tuple[StopPoint, ...]] = (
    (90.0, PALETTE["success"]), (80.0, PALETTE["info"]), (65.0, PALETTE["warning"]), (50.0, "#ff7b39")
)
SEVERITY_MAP: Final[Mapping[SeverityType, str]] = MappingProxyType({
    SeverityType.OK: "\u2713", SeverityType.INFO: "\u2139", 
    SeverityType.WARNING: "\u26a0", SeverityType.DANGER: "\u2716"
})

@lru_cache(maxsize=32)
def app_title() -> str: return f"{APP_NAME} v{APP_VERSION}"

@lru_cache(maxsize=64)
def color(name: Optional[str]) -> ColorHex: 
    """Retorna un color hexadecimal de la paleta global dado su nombre clave."""
    if not isinstance(name, str): return "#808080"
    return PALETTE.get(name, "#808080")

@lru_cache(maxsize=32)
def font_size(name: Optional[str]) -> int: 
    """Obtiene el tamaño de fuente configurado para elementos semánticos de la UI."""
    if not isinstance(name, str): return UI_CONFIG.body_size
    return FONT_SIZES.get(name, UI_CONFIG.body_size)

@lru_cache(maxsize=64)
def icon(section: Optional[str]) -> str: 
    """Retorna el símbolo Unicode asociado a una sección del sistema."""
    if not isinstance(section, str): return "\u2022"
    return ICONS.get(section, "\u2022")

@lru_cache(maxsize=64)
def tab_label(section: Optional[str]) -> str: return f"{icon(section)}  {section}" if section else "\u2022  Desconocido"

def _parse_severity(severity: Optional[str]) -> Optional[SeverityType]:
    if not isinstance(severity, str) or not severity: return None
    try: return SeverityType(severity.strip().lower())
    except (ValueError, AttributeError): return None

@lru_cache(maxsize=16)
def _get_severity_style(severity_key: Optional[SeverityType]) -> SeverityStyle:
    return SEVERITY_STYLES.get(severity_key, (PALETTE["text_muted"], "Desconocido"))

def severity_color(severity: Optional[str]) -> ColorHex: return _get_severity_style(_parse_severity(severity))[0]
def severity_label(severity: Optional[str]) -> str: return _get_severity_style(_parse_severity(severity))[1]
def severity_icon(severity: Optional[str]) -> str: return SEVERITY_MAP.get(_parse_severity(severity), "\u2022")
def grade_color(grade: Optional[str]) -> ColorHex: 
    if not grade or not isinstance(grade, str): return PALETTE["text_muted"]
    return GRADE_COLORS.get(grade[0].upper(), PALETTE["text_muted"])

@lru_cache(maxsize=128)
def score_color(score: Union[float, int, None]) -> ColorHex:
    """Selecciona el color según el puntaje, aplicando umbrales de seguridad."""
    if score is None: return PALETTE["text_muted"]
    try:
        valor = float(score)
        if not math.isfinite(valor) or not (0.0 <= valor <= 100.0): return PALETTE["text_muted"]
        for limit, color_val in SCORE_THRESHOLDS:
            if valor >= limit: return color_val
        return PALETTE["danger"]
    except (TypeError, ValueError): return PALETTE["text_muted"]

@lru_cache(maxsize=128)
def bar(percent: Union[float, int, None], width: int = 24, filled: str = "\u2588", empty: str = "\u2591") -> str:
    """Genera una barra de progreso en texto usando caracteres de bloques Unicode."""
    try:
        ancho = max(1, int(width))
        valor = float(percent) if percent is not None else 0.0
        if not math.isfinite(valor): return empty * ancho
        llenos = int(round(max(0.0, min(100.0, valor)) / 100 * ancho))
        return (filled * llenos) + (empty * (ancho - llenos))
    except (TypeError, ValueError): return empty * ancho

@lru_cache(maxsize=512)
def _hex_to_rgb(value: ColorHex) -> RGBTuple:
    """Convierte un string hex '#RRGGBB' a una tupla de enteros (R, G, B)."""
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"): return (0, 0, 0)
    try:
        r, g, b = int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)
        return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))
    except (ValueError, TypeError, IndexError): return (0, 0, 0)

@lru_cache(maxsize=512)
def _rgb_to_hex(rgb: RGBTuple) -> ColorHex:
    """Convierte una tupla RGB a formato de string hexadecimal."""
    if not isinstance(rgb, (tuple, list)) or len(rgb) != 3: return "#000000"
    try:
        vals = [int(max(0, min(255, round(float(v))))) for v in rgb]
        return f"#{vals[0]:02x}{vals[1]:02x}{vals[2]:02x}"
    except (ValueError, TypeError): return "#000000"

@lru_cache(maxsize=256)
def blend(start: ColorHex, end: ColorHex, ratio: float) -> ColorHex:
    """Interpolación lineal (LERP) entre dos colores hex para efectos de degradado."""
    r1, g1, b1 = _hex_to_rgb(start)
    r2, g2, b2 = _hex_to_rgb(end)
    r_val = max(0.0, min(1.0, float(ratio) if math.isfinite(ratio) else 0.0))
    return _rgb_to_hex((
        r1 + (r2 - r1) * r_val, 
        g1 + (g2 - g1) * r_val, 
        b1 + (b2 - b1) * r_val
    ))

@lru_cache(maxsize=128)
def gradient_colors(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorHex, ...]:
    """Genera una secuencia de colores interpolados para crear transiciones suaves."""
    n = max(1, int(steps))
    n_stops = len(stops)
    if n_stops < 2: return (stops[0],) * n
    rgb_stops = [_hex_to_rgb(s) for s in stops]
    seg = n_stops - 1
    
    def get_color(i: int) -> ColorHex:
        pos = (i * seg) / (n - 1) if n > 1 else 0
        idx = min(int(pos), seg - 1)
        progress = pos - idx
        s_rgb, e_rgb = rgb_stops[idx], rgb_stops[idx + 1]
        return _rgb_to_hex((
            s_rgb[0] + (e_rgb[0] - s_rgb[0]) * progress, 
            s_rgb[1] + (e_rgb[1] - s_rgb[1]) * progress, 
            s_rgb[2] + (e_rgb[2] - s_rgb[2]) * progress
        ))

    return tuple(get_color(i) for i in range(n))

@lru_cache(maxsize=128)
def get_gradient_segments(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorSegment, ...]:
    """Optimiza el renderizado agrupando colores idénticos en segmentos para el Canvas."""
    colors = gradient_colors(steps, stops)
    if not colors: return ()
    segments: List[ColorSegment] = []
    start = 0
    current_color = colors[0]
    for i in range(1, len(colors)):
        if colors[i] != current_color:
            segments.append(ColorSegment(current_color, start, i))
            start, current_color = i, colors[i]
    segments.append(ColorSegment(current_color, start, len(colors)))
    return tuple(segments)

@lru_cache(maxsize=64)
def _get_scaled_poly(scale: float) -> CoordinateSequence:
    """Calcula las coordenadas escaladas para el contorno del escudo."""
    if not (math.isfinite(scale) and scale > 0): return ()
    return tuple(v * scale for v in SHIELD_BASE_COORDS)

@lru_cache(maxsize=16)
def logo_svg(size: int = DEFAULT_SVG_SIZE) -> str:
    s = max(16, min(1024, int(size)))
    return _SVG_TEMPLATE_BASE.format(s=s, stops=_SVG_GRADIENT_STOPS)

def save_logo_svg(destination: Union[str, Path, None], size: int = DEFAULT_SVG_SIZE) -> Optional[Path]:
    if not isinstance(destination, (str, Path)) or not destination: return None
    try:
        target = Path(destination).resolve()
        if not filter_safe_paths([target]): return None
        if not is_safe_to_modify(target): return None
        if target.parent and not target.parent.exists(): target.parent.mkdir(parents=True, exist_ok=True)
        ensure_safe_to_modify(target)
        target.write_text(logo_svg(size), encoding="utf-8")
        return target
    except (OSError, PermissionError, TypeError, ValueError): return None

_ASCII_LOGO: Final[str] = (
    "   ___  __  __ ___ ___   _\n"
    "  / _ \\|  \\/  | __/ __| /_\\\n"
    " | (_) | |\\/| | _|| (_ // _ \\\n"
    "  \\___/|_|  |_|___\\___/_/ \\_\\\n"
    "      Limpieza Total Omega\n"
)

def logo_ascii() -> str: return _ASCII_LOGO

STRIPE_THICKNESS_SCALE: Final[float] = 92.0
STRIPE_COUNT_FACTOR: Final[float] = 28.0
STRIPE_BASE_Y_OFFSET: Final[float] = 18.0

@lru_cache(maxsize=16)
def _get_stripe_params(scale: float, n: int) -> Tuple[StripeParams, ...]:
    """Calcula los parámetros geométricos para las bandas decorativas del escudo."""
    if not math.isfinite(scale) or scale <= 0 or n < 1: return ()
    total_thickness = STRIPE_THICKNESS_SCALE * scale
    step = total_thickness / n
    def _gen_params():
        for i in range(n):
            rel = i / (n - 1) if n > 1 else 0
            thick = total_thickness * (1.0 if rel < 0.55 else 1.0 - ((rel - 0.55) * 1.9))
            yield (max(0.1, thick), i * step, (i + 1) * step)
    return tuple(_gen_params())

def _draw_shield_glow(canvas: CanvasElement, x: float, y: float, scale: float) -> None:
    """Dibuja un resplandor radial de fondo detrás del escudo, escalado según la proporción dada."""
    radius = SHIELD_GLOW_OFFSET * scale
    canvas.create_oval(
        x + (SHIELD_CENTER_X * scale) - radius, y + (SHIELD_CENTER_Y * scale) - radius,
        x + (SHIELD_CENTER_X * scale) + radius, y + (SHIELD_CENTER_Y * scale) + radius,
        fill=blend(PALETTE["surface"], PALETTE["glow"], 0.15), outline=""
    )

def _draw_shield_stripes(canvas: CanvasElement, x: float, y: float, scale: float) -> None:
    """Renderiza las bandas decorativas del interior del escudo usando segmentos de gradiente."""
    if not (math.isfinite(x) and math.isfinite(y) and scale > 0): return
    try:
        n = max(6, int(STRIPE_COUNT_FACTOR * scale))
        params = _get_stripe_params(scale, n)
        base_y = y + STRIPE_BASE_Y_OFFSET * scale
        center_x = x + SHIELD_CENTER_X * scale
        for seg in get_gradient_segments(n):
            c_fill = seg.hex_color
            for i in range(seg.start_index, seg.end_index):
                thickness, start_offset, end_offset = params[i]
                canvas.create_rectangle(
                    center_x - thickness, base_y + start_offset, 
                    center_x + thickness, base_y + end_offset, 
                    fill=c_fill, outline=""
                )
    except (AttributeError, TypeError, ValueError, IndexError): pass

def _draw_shield_icon_decorations(canvas: CanvasElement, x: float, y: float, scale: float) -> None:
    """Aplica detalles visuales superiores (línea, triángulo y omega) sobre el escudo."""
    if not (math.isfinite(x) and math.isfinite(y) and scale > 0): return
    try:
        ts = max(8, int(UI_CONFIG.header_size * scale))
        canvas.create_line(
            x + _DECOR_LINE_X1 * scale, y + _DECOR_LINE_Y1 * scale,
            x + _DECOR_LINE_X2 * scale, y + _DECOR_LINE_Y2 * scale,
            fill=PALETTE["background"], width=max(2, int(_DECOR_STROKE_WIDTH * scale)), capstyle="round"
        )
        canvas.create_polygon(
            x + _DECOR_LINE_X2 * scale, y + _DECOR_LINE_Y2 * scale,
            x + _DECOR_TRIANGLE_X3 * scale, y + _DECOR_TRIANGLE_Y3 * scale,
            x + _DECOR_TRIANGLE_X4 * scale, y + _DECOR_TRIANGLE_Y4 * scale,
            fill=PALETTE["background"], outline=""
        )
        canvas.create_text(
            x + SHIELD_CENTER_X * scale, y + (SHIELD_CENTER_Y + 40) * scale,
            text="\u03a9", fill=PALETTE["background"],
            font=(UI_CONFIG.font_family, ts, UI_CONFIG.bold_weight), anchor="center"
        )
    except (AttributeError, TypeError, ValueError): pass

def draw_logo(canvas: CanvasElement, size: float = 56.0, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    """Función principal para renderizar el logo de la marca en un canvas de Tkinter."""
    if canvas is None: return
    try:
        s_val = float(size) if isinstance(size, (int, float)) else 56.0
        x_val, y_val = float(canvas_x), float(canvas_y)
        if not all(math.isfinite(v) for v in (s_val, x_val, y_val)): return
        scale = max(MIN_SCALE_FACTOR, min(MAX_SCALE_FACTOR, s_val / SHIELD_WIDTH))
        poly_base = _get_scaled_poly(scale)
        if not poly_base: return
        poly = tuple(val + (x_val if i % 2 == 0 else y_val) for i, val in enumerate(poly_base))
        _draw_shield_glow(canvas, x_val, y_val, scale)
        canvas.create_polygon(*poly, fill=GRADIENT_STOPS[1], outline="")
        _draw_shield_stripes(canvas, x_val, y_val, scale)
        _draw_shield_icon_decorations(canvas, x_val, y_val, scale)
    except (AttributeError, TypeError, ValueError, ZeroDivisionError): pass

def draw_gradient_bar(canvas: CanvasElement, width: int, height: int = 3, canvas_x: float = 0.0, canvas_y: float = 0.0, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> None:
    """Dibuja una línea de ancho fijo que exhibe una transición de color (degradado)."""
    if canvas is None or not isinstance(width, int) or width < 1: return
    if not all(math.isfinite(v) for v in (canvas_x, canvas_y, height)): return
    try:
        for segment in get_gradient_segments(width, stops):
            canvas.create_line(
                canvas_x + segment.start_index, canvas_y, 
                canvas_x + segment.end_index, canvas_y, 
                fill=segment.hex_color, width=max(1, height)
            )
    except (AttributeError, TypeError, ValueError): pass

def draw_ring(canvas: CanvasElement, percent: Union[float, int, None], size: int = 150, canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14, track: Optional[ColorHex] = None, fill: Optional[ColorHex] = None) -> None:
    """Dibuja un anillo circular indicador (usado para visualización de progreso o métricas)."""
    if canvas is None or percent is None: return
    try:
        val = float(percent)
        if not math.isfinite(val): return
        val = max(0.0, min(100.0, val))
        d = max(20, min(2048, int(size)))
        thickness = max(1, int(thickness))
        pad = thickness / 2.0
        x_val, y_val = float(canvas_x), float(canvas_y)
        if not all(math.isfinite(v) for v in (x_val, y_val)): return
        bb = (x_val + pad, y_val + pad, x_val + d - pad, y_val + d - pad)
        canvas.create_arc(*bb, start=0.0, extent=359.9, style="arc", outline=track or PALETTE["surface_alt"], width=thickness)
        if val > 0: canvas.create_arc(*bb, start=90.0, extent=-(val / 100.0 * 359.9), style="arc", outline=fill or score_color(val), width=thickness)
    except (AttributeError, TypeError, ValueError): pass
