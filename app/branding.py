"""
branding.py — identidad visual de Limpieza Total Omega.

Centraliza la gestión de activos visuales, paletas de colores, jerarquías 
tipográficas y sistemas de renderizado vectorial (SVG/Canvas).

CONFIGURACIÓN Y ESTADOS:
  - La paleta se expone mediante tipos de solo lectura para garantizar 
    inmutabilidad y prevenir efectos secundarios durante la ejecución.
  - Los gradientes utilizan segmentación por agrupación de colores consecutivos 
    para optimizar el rendimiento de renderizado en el objeto Canvas.
  - Las funciones de dibujo capturan excepciones para mantener la estabilidad UI.

NOTA DE SEGURIDAD:
  Las funciones de dibujo (Canvas) y generación de archivos (SVG) operan 
  bajo principios de diseño defensivo, capturando excepciones para evitar que 
  una paleta mal configurada detenga el hilo principal. Las operaciones de 
  disco utilizan validadores estrictos mediante el módulo 'safety'.
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
_SVG_GRADIENT_STOPS: Final[str] = (
    '      <stop offset="0%" stop-color="#00f0c0"/>\n'
    '      <stop offset="55%" stop-color="#7c5cff"/>\n'
    '      <stop offset="100%" stop-color="#ff2d78"/>'
)

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

# Plantilla SVG parametrizada con marcadores de posición para inyección segura
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
    """Protocolo que define los métodos de dibujo esperados por los componentes visuales."""
    def create_rectangle(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., outline: str = ..., width: int = ...) -> int: ...
    def create_polygon(self, *args: float, fill: str = ..., outline: str = ...) -> int: ...
    def create_oval(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., outline: str = ...) -> int: ...
    def create_line(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., width: int = ..., capstyle: str = ...) -> int: ...
    def create_text(self, x: float, y: float, *, text: str, fill: str = ..., font: Union[Tuple[str, int, str], str] = ..., anchor: str = ...) -> int: ...
    def create_arc(self, x0: float, y0: float, x1: float, y1: float, *, start: float, extent: float, style: str = ..., outline: str = ..., width: int = ...) -> int: ...

class ColorSegment(NamedTuple):
    """Define una sección contigua de colores para optimizar el dibujo de gradientes."""
    hex_color: ColorHex
    start_index: int
    end_index: int

class PaletteDict(TypedDict):
    """Diccionario que agrupa los valores hexadecimales de todos los colores del sistema."""
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
    """Mapa de categorías de texto a tamaños definidos en puntos para consistencia tipográfica."""
    display: int
    title: int
    subtitle: int
    heading: int
    body: int
    mono: int
    caption: int

PALETTE: Final[PaletteDict] = MappingProxyType({
    "background": C_BACKGROUND, "surface": C_SURFACE, "surface_alt": C_SURFACE_ALT,
    "surface_hover": C_SURFACE_HOVER, "card": C_CARD, "accent": C_ACCENT,
    "accent_hover": C_ACCENT_HOVER, "accent_dim": C_ACCENT_DIM, "accent2": C_ACCENT2,
    "accent2_hover": C_ACCENT2_HOVER, "accent3": C_ACCENT3, "success": C_SUCCESS,
    "info": C_INFO, "warning": C_WARNING, "danger": C_DANGER,
    "danger_hover": C_DANGER_HOVER, "text": C_TEXT, "text_muted": C_TEXT_MUTED,
    "text_dim": C_TEXT_DIM, "border": C_BORDER, "glow": C_GLOW,
}) # type: ignore

FONT_SIZES: Final[FontSizesDict] = MappingProxyType({
    "display": 46, "title": 26, "subtitle": 13, "heading": 16,
    "body": UI_CONFIG.body_size, "mono": 11, "caption": 10,
}) # type: ignore

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

def app_title() -> str: 
    """Retorna el nombre completo de la aplicación con su versión actual."""
    return f"{APP_NAME} v{APP_VERSION}"

def color(name: Optional[str]) -> ColorHex: 
    """Acceso seguro a un color de la paleta corporativa por nombre."""
    if not isinstance(name, str): return "#808080"
    return PALETTE.get(name.lower(), "#808080")

def font_size(name: Optional[str]) -> int: 
    """Retorna el tamaño de fuente configurado para una categoría dada."""
    if not isinstance(name, str): return UI_CONFIG.body_size
    return FONT_SIZES.get(name.lower(), UI_CONFIG.body_size)

def icon(section: Optional[str]) -> str: 
    """Retorna el glifo Unicode asociado a una sección de la aplicación."""
    if not isinstance(section, str): return "\u2022"
    return ICONS.get(section, "\u2022")

def tab_label(section: Optional[str]) -> str: 
    """Formatea la etiqueta de una pestaña combinando icono y texto."""
    valid_section = section if isinstance(section, str) and section else "Desconocido"
    return f"{icon(valid_section)}  {valid_section}"

def _parse_severity(severity: Optional[str]) -> Optional[SeverityType]:
    """Valida y normaliza una cadena de texto a un enum de tipo SeverityType."""
    if not isinstance(severity, str): return None
    try: return SeverityType(severity.strip().lower())
    except (ValueError, AttributeError): return None

def _get_severity_style(severity_key: Optional[SeverityType]) -> SeverityStyle:
    """Busca el par color/etiqueta para un nivel de severidad determinado."""
    return SEVERITY_STYLES.get(severity_key, (C_TEXT_MUTED, "Desconocido"))

def severity_color(severity: Optional[str]) -> ColorHex: 
    """Retorna el color corporativo asociado a una severidad dada."""
    return _get_severity_style(_parse_severity(severity))[0]

def severity_label(severity: Optional[str]) -> str: 
    """Retorna la etiqueta legible para una severidad dada."""
    return _get_severity_style(_parse_severity(severity))[1]

def severity_icon(severity: Optional[str]) -> str: 
    """Retorna el icono Unicode para representar una severidad."""
    return SEVERITY_MAP.get(_parse_severity(severity), "\u2022")

def grade_color(grade: Optional[str]) -> ColorHex: 
    """Retorna el color asignado a una calificación de salud (A-F)."""
    if not grade or not isinstance(grade, str): return C_TEXT_MUTED
    return GRADE_COLORS.get(grade[0].upper(), C_TEXT_MUTED)

@lru_cache(maxsize=128)
def score_color(score: Union[float, int, None]) -> ColorHex:
    """Calcula el color representativo según el umbral de una puntuación (0-100)."""
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
    """Genera una barra de progreso visual en texto mediante caracteres Unicode."""
    try:
        valor = float(percent) if percent is not None else 0.0
        ancho = max(1, int(width))
        if not math.isfinite(valor): return empty * ancho
        llenos = int(round(max(0.0, min(100.0, valor)) / 100 * ancho))
        return filled * llenos + empty * (ancho - llenos)
    except (TypeError, ValueError): return empty * max(1, int(width))

@lru_cache(maxsize=256)
def _hex_to_rgb(value: ColorHex) -> RGBTuple:
    """Convierte un color hexadecimal (#RRGGBB) a una tupla de valores (R, G, B)."""
    if not isinstance(value, str) or not value.startswith('#') or len(value) != 7:
        return (0, 0, 0)
    try:
        val = int(value.lstrip('#'), 16)
        return ((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF)
    except ValueError: 
        return (0, 0, 0)

@lru_cache(maxsize=256)
def _rgb_to_hex(rgb: RGBTuple) -> ColorHex:
    """Convierte una tupla RGB a formato hexadecimal CSS, asegurando validación de rango."""
    r, g, b = [max(0, min(255, c)) for c in rgb]
    return f"#{r:02x}{g:02x}{b:02x}"

def blend(start: ColorHex, end: ColorHex, ratio: float) -> ColorHex:
    """Realiza una interpolación lineal (mezcla) entre dos colores dado un ratio (0.0-1.0)."""
    r1, g1, b1 = _hex_to_rgb(start)
    r2, g2, b2 = _hex_to_rgb(end)
    r = max(0.0, min(1.0, float(ratio)))
    return _rgb_to_hex((int(r1 + (r2 - r1) * r), int(g1 + (g2 - g1) * r), int(b1 + (b2 - b1) * r)))

def _get_grouped_segments(colors: Tuple[ColorHex, ...]) -> Tuple[ColorSegment, ...]:
    """Optimiza una secuencia de colores agrupando colores adyacentes idénticos."""
    if not colors: return ()
    segments = []
    curr, start = colors[0], 0
    for i in range(1, len(colors)):
        if colors[i] != curr:
            segments.append(ColorSegment(curr, start, i))
            curr, start = colors[i], i
    segments.append(ColorSegment(curr, start, len(colors)))
    return tuple(segments)

@lru_cache(maxsize=64)
def gradient_colors(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorHex, ...]:
    """Genera una lista de colores interpolados basada en puntos de control (stops)."""
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
    """Caché para los segmentos de gradiente calculados para una longitud específica."""
    return _get_grouped_segments(gradient_colors(steps, stops))

SHIELD_BASE_COORDS: Final[CoordinateSequence] = (64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31)

@lru_cache(maxsize=128)
def _get_scaled_poly(scale: float, x: float, y: float) -> CoordinateSequence:
    """Escala y traslada las coordenadas base del escudo de Omega."""
    if not math.isfinite(scale) or scale <= 0: return ()
    return tuple(x + (c * scale) if i % 2 == 0 else y + (c * scale) for i, c in enumerate(SHIELD_BASE_COORDS))

def logo_svg(size: int = 128) -> str:
    """Genera el contenido XML de un logo SVG basado en la identidad visual."""
    s = max(16, min(1024, int(size)))
    return _SVG_TEMPLATE.format(s=s, stops=_SVG_GRADIENT_STOPS)

def save_logo_svg(destination: Union[str, Path, None], size: int = 128) -> Optional[Path]:
    """Guarda el logo SVG en el sistema de archivos tras verificar permisos."""
    if destination is None: return None
    try:
        dest_path = Path(destination).resolve()
        parent = dest_path.parent
        
        # Validación defensiva estricta antes de proceder
        if is_protected_path(dest_path) or not is_safe_to_modify(dest_path): 
            return None
            
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            
        ensure_safe_to_modify(dest_path)
        dest_path.write_text(logo_svg(int(size)), encoding="utf-8")
        return dest_path
    except (OSError, PermissionError, RuntimeError, TypeError, ValueError): 
        return None

def logo_ascii() -> str: 
    """Retorna una versión ASCII art del logo para logs de terminal."""
    return "\n   ___  __  __ ___ ___   _\n  / _ \\|  \\/  | __/ __| /_\\\n | (_) | |\\/| | _|| (_ // _ \\\n  \\___/|_|  |_|___\\___/_/ \\_\\\n      Limpieza Total Omega\n"

STRIPE_THICKNESS_SCALE: Final[float] = 92.0
STRIPE_COUNT_FACTOR: Final[float] = 28.0
STRIPE_BASE_Y_OFFSET: Final[float] = 18.0

@lru_cache(maxsize=16)
def _get_stripe_params(scale: float, n: int) -> Tuple[Tuple[float, float, float], ...]:
    """Calcula los parámetros geométricos para las franjas dinámicas del escudo."""
    if not math.isfinite(scale) or scale <= 0 or n < 1: return ()
    tf = STRIPE_THICKNESS_SCALE * scale
    params = []
    for i in range(n):
        rel = i / (n - 1) if n > 1 else 0
        thick = tf * (1.0 if rel < 0.55 else 1.0 - ((rel - 0.55) * 1.9))
        params.append((thick, i * (tf / n), (i + 1) * (tf / n)))
    return tuple(params)

def _draw_shield_stripes(canvas: CanvasElement, x: float, y: float, scale: float) -> None:
    """Dibuja las franjas internas del escudo usando gradientes segmentados."""
    if not hasattr(canvas, "create_rectangle") or not math.isfinite(scale) or scale <= 0:
        return
    n = max(6, int(STRIPE_COUNT_FACTOR * scale))
    params = _get_stripe_params(scale, n)
    if not params: return
    by, cx = y + STRIPE_BASE_Y_OFFSET * scale, x + 64 * scale
    for s in get_gradient_segments(n):
        p_start, p_end = params[s.start_index], params[s.end_index - 1]
        canvas.create_rectangle(cx - p_start[0], by + p_start[1], cx + p_start[0], by + p_end[2], fill=s.hex_color, outline="")

def _draw_shield_icon_decorations(canvas: CanvasElement, x: float, y: float, scale: float) -> None:
    """Dibuja detalles decorativos (líneas, icono Omega) sobre la base del escudo."""
    if not (hasattr(canvas, "create_line") and hasattr(canvas, "create_polygon") and hasattr(canvas, "create_text")):
        return
    if not math.isfinite(scale) or scale <= 0:
        return
    ts = max(8, int(UI_CONFIG.header_size * scale))
    canvas.create_line(x+41*scale, y+75*scale, x+75*scale, y+41*scale, fill=C_BACKGROUND, width=max(2, int(8.0 * scale)), capstyle="round")
    canvas.create_polygon(x+75*scale, y+41*scale, x+89*scale, y+38*scale, x+92*scale, y+52*scale, fill=C_BACKGROUND, outline="")
    canvas.create_text(x+64*scale, y+96*scale, text="\u03a9", fill=C_BACKGROUND, font=(UI_CONFIG.font_family, ts, UI_CONFIG.bold_weight), anchor="center")

def draw_logo(canvas: CanvasElement, size: float = 56.0, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    """
    Renderiza el logo vectorial del escudo Omega en un widget Canvas mediante 
    primitivas gráficas escaladas y posicionadas dinámicamente.
    """
    try:
        if canvas is None or not hasattr(canvas, "create_oval"): return
        val_size = float(size)
        if not math.isfinite(val_size) or val_size <= 0: return
        s = max(0.1, min(10.0, val_size / 128.0))
        poly = _get_scaled_poly(s, canvas_x, canvas_y)
        if not poly: return
        canvas.create_oval(canvas_x+(64-75)*s, canvas_y+(58-75)*s, canvas_x+(64+75)*s, canvas_y+(58+75)*s, fill=blend(C_SURFACE, C_GLOW, 0.15), outline="")
        canvas.create_polygon(*poly, fill=GRADIENT_STOPS[1], outline="")
        _draw_shield_stripes(canvas, canvas_x, canvas_y, s)
        _draw_shield_icon_decorations(canvas, canvas_x, canvas_y, s)
    except Exception: pass

def draw_gradient_bar(canvas: CanvasElement, width: int, height: int = 3, canvas_x: float = 0.0, canvas_y: float = 0.0, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> None:
    """
    Dibuja una barra de progreso horizontal con degradado, optimizando las llamadas
    a la API del Canvas mediante el agrupamiento de segmentos monocromáticos.
    """
    try:
        w, h = max(1, int(width)), max(1, int(height))
        for s in get_gradient_segments(w, stops):
            canvas.create_line(canvas_x + s.start_index, canvas_y, canvas_x + s.end_index, canvas_y, fill=s.hex_color, width=h)
    except Exception: pass

def draw_ring(canvas: CanvasElement, percent: Union[float, int, None], size: int = 150, canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14, track: Optional[ColorHex] = None, fill: Optional[ColorHex] = None) -> None:
    """
    Dibuja un indicador circular de progreso (anillo) basado en un valor porcentual,
    utilizando arcos geométricos para representar métricas de estado de salud.
    """
    try:
        if percent is None: return
        val = float(percent)
        if not math.isfinite(val): return
        val = max(0.0, min(100.0, val))
        d, t = max(20, min(2048, int(size))), max(2, int(thickness))
        pad = t / 2.0
        bb = (canvas_x + pad, canvas_y + pad, canvas_x + d - pad, canvas_y + d - pad)
        canvas.create_arc(*bb, start=0.0, extent=359.9, style="arc", outline=track or C_SURFACE_ALT, width=t)
        if val > 0: canvas.create_arc(*bb, start=90.0, extent=-(val / 100.0 * 359.9), style="arc", outline=fill or score_color(val), width=t)
    except Exception: pass
