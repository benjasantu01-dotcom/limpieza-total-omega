"""
branding.py — identidad visual de Limpieza Total Omega.

Centraliza la gestión de activos visuales, paletas de colores, jerarquías 
tipográficas y sistemas de renderizado vectorial (SVG/Canvas).

CONFIGURACIÓN Y ESTADOS:
  - La paleta se expone vía MappingProxyType para garantizar inmutabilidad,
    evitando efectos secundarios accidentales durante la ejecución.
  - Los gradientes utilizan segmentación por agrupación de colores consecutivos 
    para optimizar el rendimiento de renderizado en el objeto Canvas.
  - Las funciones de dibujo capturan excepciones para mantener la estabilidad UI.

NOTA DE SEGURIDAD:
  Las funciones de dibujo (Canvas) y generación de archivos (SVG) operan 
  bajo principios de diseño defensivo, capturando excepciones de renderizado 
  para evitar que una paleta mal configurada o una entrada inválida 
  detengan el hilo principal de la aplicación. Las operaciones de disco 
  utilizan validadores estrictos mediante el módulo 'safety'.
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

# Definición de tipos para mejorar la semántica del código
ColorHex: TypeAlias = str  
GradeKey: TypeAlias = Literal["A", "B", "C", "D", "F"]
SeverityStyle: TypeAlias = Tuple[ColorHex, str]  
RGBTuple: TypeAlias = Tuple[int, int, int]
CoordinateSequence: TypeAlias = Tuple[float, ...]

class UIConfiguration(NamedTuple):
    """Contenedor inmutable de parámetros globales de identidad visual."""
    font_family: str = "Segoe UI"
    header_size: int = 23
    body_size: int = 12
    bold_weight: str = "bold"

UI_CONFIG: Final[UIConfiguration] = UIConfiguration()

class Point(NamedTuple):
    """Representación semántica de un par de coordenadas 2D."""
    x: float
    y: float

class SeverityType(Enum):
    """Categorías estándar de severidad de riesgo para la aplicación."""
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"

# Pre-generación de fragmentos SVG estáticos para mejorar performance
_SVG_GRADIENT_STOPS: Final[str] = "\n".join(
    f'      <stop offset="{o}" stop-color="{c}"/>' 
    for o, c in zip(("0%", "55%", "100%"), ("#00f0c0", "#7c5cff", "#ff2d78"))
)

# Plantilla SVG parametrizada
_SVG_TEMPLATE: Final[str] = """<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="omegaShield" x1="0" y1="0" x2="1" y2="1">{stops}</linearGradient>
    <radialGradient id="omegaGlow" cx="0.5" cy="0.4" r="0.6">
      <stop offset="0%" stop-color="{glow}" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="{glow}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="128" height="128" rx="30" fill="{bg}"/>
  <circle cx="64" cy="56" r="52" fill="url(#omegaGlow)"/>
  <path d="M64 18 L100 31 V67 C100 90 83 104 64 110 C45 104 28 90 28 67 V31 Z" fill="url(#omegaShield)"/>
  <path d="M41 75 L75 41" stroke="{bg}" stroke-width="8" stroke-linecap="round"/>
  <path d="M75 41 L89 38 L92 52 Z" fill="{bg}"/>
  <text x="64" y="98" font-family="{font}" font-size="26" font-weight="bold" fill="{bg}" text-anchor="middle">&#937;</text>
</svg>"""

class CanvasElement(Protocol):
    """Protocolo que define los métodos de dibujo esperados por los componentes visuales de la interfaz."""
    def create_rectangle(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., outline: str = ..., width: int = ...) -> int: ...
    def create_polygon(self, *args: float, fill: str = ..., outline: str = ...) -> int: ...
    def create_oval(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., outline: str = ...) -> int: ...
    def create_line(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., width: int = ..., capstyle: str = ...) -> int: ...
    def create_text(self, x: float, y: float, *, text: str, fill: str = ..., font: Union[Tuple[str, int, str], str] = ..., text_anchor: str = ...) -> int: ...
    def create_arc(self, x0: float, y0: float, x1: float, y1: float, *, start: float, extent: float, style: str = ..., outline: str = ..., width: int = ...) -> int: ...

class ColorSegment(NamedTuple):
    """Define una sección contigua de colores para optimizar el dibujo de gradientes mediante segmentación."""
    hex_color: ColorHex
    start_index: int
    end_index: int

class PaletteDict(TypedDict):
    """Estructura de datos para la configuración cromática del sistema (hexadecimal)."""
    background: ColorHex
    surface: ColorHex
    surface_alt: ColorHex
    surface_hover: ColorHex
    card: ColorHex
    accent: ColorHex
    accent_hover: ColorHex
    accent_dim: ColorHex
    accent2: ColorHex
    accent2_hover: ColorHex
    accent3: ColorHex
    success: ColorHex
    info: ColorHex
    warning: ColorHex
    danger: ColorHex
    danger_hover: ColorHex
    text: ColorHex
    text_muted: ColorHex
    text_dim: ColorHex
    border: ColorHex
    glow: ColorHex

class FontSizesDict(TypedDict):
    """Jerarquía de tamaños tipográficos (en puntos) disponibles en la interfaz."""
    display: int
    title: int
    subtitle: int
    heading: int
    body: int
    mono: int
    caption: int

# Constantes de identidad corporativa
APP_NAME: Final[str] = "Limpieza Total Omega"
APP_SHORT_NAME: Final[str] = "Omega"
APP_TAGLINE: Final[str] = "Limpieza y seguridad, en un solo lugar"
APP_VERSION: Final[str] = "2.1.0"

# Paleta Maestra
C_BACKGROUND: Final[ColorHex] = "#0a0e17"
C_SURFACE: Final[ColorHex] = "#141b2d"
C_SURFACE_ALT: Final[ColorHex] = "#1e2740"
C_SURFACE_HOVER: Final[ColorHex] = "#28324f"
C_CARD: Final[ColorHex] = "#182135"
C_ACCENT: Final[ColorHex] = "#00f0c0"
C_ACCENT_HOVER: Final[ColorHex] = "#00d0a4"
C_ACCENT_DIM: Final[ColorHex] = "#0a6b58"
C_ACCENT2: Final[ColorHex] = "#7c5cff"
C_ACCENT2_HOVER: Final[ColorHex] = "#6a48f0"
C_ACCENT3: Final[ColorHex] = "#ff2d78"
C_SUCCESS: Final[ColorHex] = "#22e39a"
C_INFO: Final[ColorHex] = "#38bdf8"
C_WARNING: Final[ColorHex] = "#ffb020"
C_DANGER: Final[ColorHex] = "#ff4757"
C_DANGER_HOVER: Final[ColorHex] = "#e02e3d"
C_TEXT: Final[ColorHex] = "#f0f6fc"
C_TEXT_MUTED: Final[ColorHex] = "#94a3b8"
C_TEXT_DIM: Final[ColorHex] = "#5c6b85"
C_BORDER: Final[ColorHex] = "#2a3654"
C_GLOW: Final[ColorHex] = "#00f0c0"

PALETTE: Final[Mapping[str, ColorHex]] = MappingProxyType({
    "background": C_BACKGROUND, "surface": C_SURFACE, "surface_alt": C_SURFACE_ALT,
    "surface_hover": C_SURFACE_HOVER, "card": C_CARD, "accent": C_ACCENT,
    "accent_hover": C_ACCENT_HOVER, "accent_dim": C_ACCENT_DIM, "accent2": C_ACCENT2,
    "accent2_hover": C_ACCENT2_HOVER, "accent3": C_ACCENT3, "success": C_SUCCESS,
    "info": C_INFO, "warning": C_WARNING, "danger": C_DANGER,
    "danger_hover": C_DANGER_HOVER, "text": C_TEXT, "text_muted": C_TEXT_MUTED,
    "text_dim": C_TEXT_DIM, "border": C_BORDER, "glow": C_GLOW,
})

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
SCORE_THRESHOLDS: Final[Tuple[Tuple[float, ColorHex], ...]] = (
    (90.0, C_SUCCESS), (80.0, C_INFO), (65.0, C_WARNING), (50.0, "#ff7b39")
)
SEVERITY_MAP: Final[Mapping[SeverityType, str]] = MappingProxyType({
    SeverityType.OK: "\u2713", SeverityType.INFO: "\u2139", 
    SeverityType.WARNING: "\u26a0", SeverityType.DANGER: "\u2716"
})

def app_title() -> str: return f"{APP_NAME} v{APP_VERSION}"
def color(name: Optional[str]) -> ColorHex: return PALETTE.get(name, "#808080") if name else "#808080"
def font_size(name: Optional[str]) -> int: return FONT_SIZES.get(name, UI_CONFIG.body_size) if name else UI_CONFIG.body_size
def icon(section: Optional[str]) -> str: return ICONS.get(section.strip(), "\u2022") if section else "\u2022"
def tab_label(section: Optional[str]) -> str: return f"{icon(section)}  {section}" if section else "\u2022  Desconocido"

def _parse_severity(severity: Optional[str]) -> Optional[SeverityType]:
    if not isinstance(severity, str): return None
    try: return SeverityType(severity.strip().lower())
    except (ValueError, AttributeError): return None

def _get_severity_style(severity_key: Optional[SeverityType]) -> SeverityStyle:
    return SEVERITY_STYLES.get(severity_key, (C_TEXT_MUTED, "Desconocido"))

def severity_color(severity: Optional[str]) -> ColorHex: return _get_severity_style(_parse_severity(severity))[0]
def severity_label(severity: Optional[str]) -> str: return _get_severity_style(_parse_severity(severity))[1]
def severity_icon(severity: Optional[str]) -> str: return SEVERITY_MAP.get(_parse_severity(severity), "\u2022")
def grade_color(grade: Optional[str]) -> ColorHex: return GRADE_COLORS.get(grade.strip().upper()[0], C_TEXT_MUTED) if grade and grade.strip() else C_TEXT_MUTED

@lru_cache(maxsize=128)
def score_color(score: Union[float, int, None]) -> ColorHex:
    if score is None: return C_TEXT_MUTED
    try:
        valor = float(score)
        if not math.isfinite(valor) or not (0.0 <= valor <= 100.0): return C_TEXT_MUTED
        for limit, color_val in SCORE_THRESHOLDS:
            if valor >= limit: return color_val
        return C_DANGER
    except (TypeError, ValueError): return C_TEXT_MUTED

@lru_cache(maxsize=64)
def bar(percent: Union[float, int, None], width: int = 24, filled: str = "\u2588", empty: str = "\u2591") -> str:
    try:
        valor = float(percent) if percent is not None else 0.0
        ancho = max(1, int(width))
        llenos = int(round(max(0.0, min(100.0, valor)) / 100 * ancho))
        return filled * llenos + empty * (ancho - llenos)
    except (TypeError, ValueError): return empty * max(1, int(width))

@lru_cache(maxsize=512)
def _hex_to_rgb(value: str) -> RGBTuple:
    try:
        val = int(value[1:], 16)
        return ((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF)
    except (ValueError, TypeError, IndexError): return (0, 0, 0)

@lru_cache(maxsize=512)
def _rgb_to_hex(rgb: RGBTuple) -> ColorHex:
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, int(c))) for c in rgb])

@lru_cache(maxsize=256)
def blend(start: str, end: str, ratio: float) -> ColorHex:
    try:
        r1, g1, b1 = _hex_to_rgb(start)
        r2, g2, b2 = _hex_to_rgb(end)
        r = max(0.0, min(1.0, float(ratio)))
        return _rgb_to_hex((int(r1 + (r2 - r1) * r), int(g1 + (g2 - g1) * r), int(b1 + (b2 - b1) * r)))
    except (TypeError, ValueError): return start

@lru_cache(maxsize=128)
def _get_grouped_segments(colors: Tuple[ColorHex, ...]) -> Tuple[ColorSegment, ...]:
    if not colors: return ()
    segments = []
    curr, start = colors[0], 0
    for i in range(1, len(colors)):
        if colors[i] != curr:
            segments.append(ColorSegment(curr, start, i))
            curr, start = colors[i], i
    segments.append(ColorSegment(curr, start, len(colors)))
    return tuple(segments)

@lru_cache(maxsize=32)
def gradient_colors(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorHex, ...]:
    n = max(1, int(steps))
    n_stops = len(stops)
    rgb_stops = [_hex_to_rgb(s) for s in stops]
    res = []
    for i in range(n):
        ratio = i / (n - 1) if n > 1 else 0
        pos = ratio * (n_stops - 1)
        idx = min(int(pos), n_stops - 2)
        delta = pos - idx
        s1, s2 = rgb_stops[idx], rgb_stops[idx + 1]
        res.append(_rgb_to_hex((int(s1[0]+(s2[0]-s1[0])*delta), int(s1[1]+(s2[1]-s1[1])*delta), int(s1[2]+(s2[2]-s1[2])*delta))))
    return tuple(res)

@lru_cache(maxsize=64)
def get_gradient_segments(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorSegment, ...]:
    return _get_grouped_segments(gradient_colors(steps, stops))

SHIELD_BASE_COORDS: Final[CoordinateSequence] = (64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31)

@lru_cache(maxsize=128)
def _get_scaled_poly(scale: float, x: float, y: float) -> CoordinateSequence:
    return tuple(x + (c * scale) if i % 2 == 0 else y + (c * scale) for i, c in enumerate(SHIELD_BASE_COORDS))

@lru_cache(maxsize=8)
def logo_svg(size: int = 128) -> str:
    s = max(16, min(1024, int(size)))
    return _SVG_TEMPLATE.format(s=s, stops=_SVG_GRADIENT_STOPS, glow=C_GLOW, bg=C_SURFACE, font=UI_CONFIG.font_family)

def save_logo_svg(destination: Union[str, Path, None], size: int = 128) -> Optional[Path]:
    try:
        if not destination: return None
        target = Path(destination).resolve()
        ensure_safe_to_modify(target)
        if not target.parent.exists(): target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(logo_svg(size), encoding="utf-8")
        return target
    except (Exception, OSError): return None

def logo_ascii() -> str: return "\n   ___  __  __ ___ ___   _\n  / _ \\|  \\/  | __/ __| /_\\\n | (_) | |\\/| | _|| (_ // _ \\\n  \\___/|_|  |_|___\\___/_/ \\_\\\n      Limpieza Total Omega\n"

STRIPE_THICKNESS_SCALE: Final[float] = 92.0
STRIPE_COUNT_FACTOR: Final[float] = 28.0
STRIPE_BASE_Y_OFFSET: Final[float] = 18.0

@lru_cache(maxsize=16)
def _get_stripe_params(scale: float, n: int) -> Tuple[Tuple[float, float, float], ...]:
    tf = STRIPE_THICKNESS_SCALE * scale
    params = []
    for i in range(n):
        rel = i / (n - 1) if n > 1 else 0
        thick = tf * (1.0 if rel < 0.55 else 1.0 - ((rel - 0.55) * 1.9))
        params.append((thick, i * (tf / n), (i + 1) * (tf / n)))
    return tuple(params)

def _draw_shield_stripes(canvas: CanvasElement, x: float, y: float, scale: float) -> None:
    try:
        if scale <= 0: return
        n = max(6, int(STRIPE_COUNT_FACTOR * scale))
        by = y + STRIPE_BASE_Y_OFFSET * scale
        cx = x + 64 * scale
        params = _get_stripe_params(scale, n)
        segs = get_gradient_segments(n)
        for s in segs:
            canvas.create_rectangle(cx - params[s.start_index][0], by + params[s.start_index][1], cx + params[s.start_index][0], by + params[s.end_index - 1][2], fill=s.hex_color, outline="")
    except Exception: pass

def _draw_shield_icon_decorations(canvas: CanvasElement, x: float, y: float, scale: float) -> None:
    try:
        lw = max(2, int(8.0 * scale))
        ts = max(8, int(UI_CONFIG.header_size * scale))
        canvas.create_line(x+41*scale, y+75*scale, x+75*scale, y+41*scale, fill=C_BACKGROUND, width=lw, capstyle="round")
        canvas.create_polygon(x+75*scale, y+41*scale, x+89*scale, y+38*scale, x+92*scale, y+52*scale, fill=C_BACKGROUND, outline="")
        canvas.create_text(x+64*scale, y+96*scale, text="\u03a9", fill=C_BACKGROUND, font=(UI_CONFIG.font_family, ts, UI_CONFIG.bold_weight))
    except Exception: pass

def draw_logo(canvas: CanvasElement, size: float = 56.0, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    try:
        s = max(0.1, min(10.0, float(size) / 128.0))
        canvas.create_oval(canvas_x+(64-75)*s, canvas_y+(58-75)*s, canvas_x+(64+75)*s, canvas_y+(58+75)*s, fill=blend(C_SURFACE, C_GLOW, 0.15), outline="")
        canvas.create_polygon(*_get_scaled_poly(s, canvas_x, canvas_y), fill=GRADIENT_STOPS[1], outline="")
        _draw_shield_stripes(canvas, canvas_x, canvas_y, s)
        _draw_shield_icon_decorations(canvas, canvas_x, canvas_y, s)
    except Exception: pass

def draw_gradient_bar(canvas: CanvasElement, width: int, height: int = 3, canvas_x: float = 0.0, canvas_y: float = 0.0, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> None:
    try:
        segments = get_gradient_segments(max(1, width), stops)
        for s in segments:
            canvas.create_line(canvas_x + s.start_index, canvas_y, canvas_x + s.end_index, canvas_y, fill=s.hex_color, width=max(1, height))
    except Exception: pass

def draw_ring(canvas: CanvasElement, percent: Union[float, int, None], size: int = 150, canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14, track: Optional[ColorHex] = None, fill: Optional[ColorHex] = None) -> None:
    try:
        val = max(0.0, min(100.0, float(percent or 0.0)))
        d = max(20, min(2048, int(size)))
        t = max(2, int(thickness))
        pad = t / 2.0
        bb = (canvas_x + pad, canvas_y + pad, canvas_x + d - pad, canvas_y + d - pad)
        canvas.create_arc(*bb, start=0, extent=359.9, style="arc", outline=track or C_SURFACE_ALT, width=t)
        if val > 0: canvas.create_arc(*bb, start=90, extent=-(val / 100.0 * 359.9), style="arc", outline=fill or score_color(val), width=t)
    except Exception: pass
