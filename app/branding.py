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
    """Interfaz abstracta para los métodos de dibujo esperados de un objeto Canvas."""
    def create_rectangle(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., outline: str = ..., width: int = ...) -> int: ...
    def create_polygon(self, *args: float, fill: str = ..., outline: str = ...) -> int: ...
    def create_oval(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., outline: str = ...) -> int: ...
    def create_line(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = ..., width: int = ..., capstyle: str = ...) -> int: ...
    def create_text(self, x: float, y: float, *, text: str, fill: str = ..., font: tuple[str, int, str] | str = ..., text_anchor: str = ...) -> int: ...
    def create_arc(self, x0: float, y0: float, x1: float, y1: float, *, start: float, extent: float, style: str = ..., outline: str = ..., width: int = ...) -> int: ...

class ColorSegment(NamedTuple):
    """Define una sección contigua de colores para optimizar el dibujo de gradientes."""
    hex_color: ColorHex
    start_index: int
    end_index: int

class PaletteDict(TypedDict):
    """Estructura de datos para la configuración cromática del sistema."""
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
    """Jerarquía de tamaños tipográficos disponibles en la interfaz."""
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

# Estilos tipográficos base
UI_FONT_FAMILY: Final[str] = "Segoe UI"
UI_FONT_BOLD: Final[str] = "bold"
UI_FONT_HEADER_SIZE: Final[int] = 23
UI_FONT_BODY_SIZE: Final[int] = 12

# Paleta Maestra: Colores HEX
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
    "body": UI_FONT_BODY_SIZE, "mono": 11, "caption": 10,
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

def app_title() -> str:
    """Retorna el título completo de la aplicación incluyendo la versión."""
    return f"{APP_NAME} v{APP_VERSION}"

def color(name: Optional[str]) -> ColorHex:
    """Recupera un color de la paleta centralizada por nombre. Retorna gris ante llaves inexistentes o inválidas."""
    if not name or not isinstance(name, str):
        return "#808080"
    return PALETTE.get(name, "#808080")

@lru_cache(maxsize=16)
def font_size(name: Optional[str]) -> int:
    """Obtiene el tamaño de fuente configurado para un identificador. Retorna valor por defecto ante error."""
    if not name or not isinstance(name, str):
        return UI_FONT_BODY_SIZE
    return FONT_SIZES.get(name, UI_FONT_BODY_SIZE)

@lru_cache(maxsize=32)
def icon(section: Optional[str]) -> str:
    """Retorna el glifo Unicode asociado a una sección de la app. Sanitiza el input mediante strip()."""
    if not section or not isinstance(section, str):
        return "\u2022"
    return ICONS.get(section.strip(), "\u2022")

@lru_cache(maxsize=32)
def tab_label(section: Optional[str]) -> str:
    """Formatea la etiqueta de una pestaña con su icono y texto. Asegura un formato consistente."""
    if not section or not isinstance(section, str): 
        return f"\u2022  Desconocido"
    return f"{icon(section)}  {section}"

def _parse_severity(severity: Optional[str]) -> Optional[SeverityType]:
    """Valida y convierte una cadena a un enum de tipo SeverityType; absorbe excepciones de formato."""
    if isinstance(severity, str):
        try:
            return SeverityType(severity.lower())
        except ValueError:
            pass
    return None

@lru_cache(maxsize=16)
def _get_severity_style(severity_key: Optional[SeverityType]) -> Tuple[ColorHex, str]:
    """Interno: recupera tupla (color, nombre) según la severidad; fallback a texto neutro."""
    if severity_key and (style := SEVERITY_STYLES.get(severity_key)):
        return style
    return (C_TEXT_MUTED, "Desconocido")

def severity_color(severity: Optional[str]) -> ColorHex:
    """Retorna el color HEX correspondiente a una severidad dada utilizando la lógica centralizada."""
    return _get_severity_style(_parse_severity(severity))[0]

def severity_label(severity: Optional[str]) -> str:
    """Retorna la etiqueta legible asociada a una severidad; capitaliza input crudo si no es reconocida."""
    sev = _parse_severity(severity)
    style = SEVERITY_STYLES.get(sev)
    if style:
        return style[1]
    return severity.capitalize() if isinstance(severity, str) else "Desconocido"

def severity_icon(severity: Optional[str]) -> str:
    """Retorna el glifo unicode específico de la severidad, delegando en el mapa de constantes."""
    sev = _parse_severity(severity)
    return SEVERITY_MAP.get(sev, "\u2022") if sev else "\u2022"

def grade_color(grade: Optional[str]) -> ColorHex:
    """Retorna el color HEX para una nota académica (A-F). Normaliza a mayúsculas y limpia espacios."""
    if not isinstance(grade, str) or not grade.strip():
        return C_TEXT_MUTED
    return GRADE_COLORS.get(grade.strip().upper()[0], C_TEXT_MUTED)

@lru_cache(maxsize=128)
def score_color(score: Union[float, int, None]) -> ColorHex:
    """Calcula el color según el puntaje (0-100) y umbrales definidos; valida finitud numérica."""
    if score is None: 
        return C_TEXT_MUTED
    try:
        valor = float(score)
        if not math.isfinite(valor) or not (0.0 <= valor <= 100.0):
            return C_TEXT_MUTED
        for limit, color_val in SCORE_THRESHOLDS:
            if valor >= limit: return color_val
        return C_DANGER
    except (TypeError, ValueError):
        return C_TEXT_MUTED

@lru_cache(maxsize=64)
def bar(percent: Union[float, int, None], width: int = 24,
        filled: str = "\u2588", empty: str = "\u2591") -> str:
    """Genera una barra de progreso visual en texto plano (Unicode) escalada por ancho."""
    try:
        valor = float(percent) if percent is not None else 0.0
        if not math.isfinite(valor): valor = 0.0
        ancho = max(1, int(width))
        llenos = int(round(max(0.0, min(100.0, valor)) / 100 * ancho))
        return filled * llenos + empty * (ancho - llenos)
    except (TypeError, ValueError):
        return empty * max(1, int(width))

@lru_cache(maxsize=512)
def _hex_to_rgb(value: Optional[str]) -> RGBTuple:
    """Transforma una cadena HEX a una tupla RGB (r, g, b). Retorna (0,0,0) ante error."""
    if isinstance(value, str) and len(value) == 7 and value.startswith('#'):
        try:
            val = int(value[1:], 16)
            return ((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF)
        except (ValueError, TypeError):
            pass
    return (0, 0, 0)

@lru_cache(maxsize=512)
def _rgb_to_hex(rgb: RGBTuple) -> ColorHex:
    """Transforma una tupla RGB (r, g, b) a una cadena HEX; asegura valores dentro de [0, 255]."""
    r, g, b = [max(0, min(255, int(c))) for c in rgb]
    return "#{:02x}{:02x}{:02x}".format(r, g, b)

@lru_cache(maxsize=256)
def blend(start: Optional[str], end: Optional[str], ratio: float) -> ColorHex:
    """Interpola linealmente entre dos colores HEX, validando ratio como [0.0, 1.0]."""
    try:
        if not isinstance(start, str) or not isinstance(end, str): 
            return start if isinstance(start, str) else C_TEXT_MUTED
        
        r1, g1, b1 = _hex_to_rgb(start)
        r2, g2, b2 = _hex_to_rgb(end)
        ratio = max(0.0, min(1.0, float(ratio))) if math.isfinite(ratio) else 0.0
        
        return _rgb_to_hex((
            int(r1 + (r2 - r1) * ratio),
            int(g1 + (g2 - g1) * ratio),
            int(b1 + (b2 - b1) * ratio)
        ))
    except (TypeError, ValueError): 
        return start if isinstance(start, str) else C_TEXT_MUTED

@lru_cache(maxsize=128)
def _get_grouped_segments(colors: Tuple[ColorHex, ...]) -> Tuple[ColorSegment, ...]:
    """Optimización: agrupa colores idénticos para reducir llamadas al Canvas."""
    if not colors: return ()
    segments: List[ColorSegment] = []
    current_color, start = colors[0], 0
    for i in range(1, len(colors)):
        if colors[i] != current_color:
            segments.append(ColorSegment(current_color, start, i))
            current_color, start = colors[i], i
    segments.append(ColorSegment(current_color, start, len(colors)))
    return tuple(segments)

@lru_cache(maxsize=32)
def gradient_colors(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorHex, ...]:
    """Genera una secuencia de colores interpolados para crear un gradiente lineal."""
    n = max(1, int(steps))
    if not stops or not all(isinstance(s, str) for s in stops) or len(stops) < 2: 
        return (stops[0] if (stops and isinstance(stops[0], str)) else C_TEXT_MUTED,) * n
    
    rgb_stops = tuple(tuple(_hex_to_rgb(s)) for s in stops)
    n_segments = len(stops) - 1
    div = n - 1 if n > 1 else 1
    
    colors: List[ColorHex] = []
    for i in range(n):
        pos = (i / div) * n_segments
        idx = min(int(pos), n_segments - 1)
        delta = pos - idx
        s1, s2 = rgb_stops[idx], rgb_stops[idx + 1]
        colors.append(_rgb_to_hex((
            int(s1[0] + (s2[0] - s1[0]) * delta),
            int(s1[1] + (s2[1] - s1[1]) * delta),
            int(s1[2] + (s2[2] - s1[2]) * delta)
        )))
    return tuple(colors)

@lru_cache(maxsize=64)
def get_gradient_segments(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorSegment, ...]:
    """Combina generación de gradiente y segmentación con cache Lru para uso frecuente en render."""
    return _get_grouped_segments(gradient_colors(steps, stops))

# Coordenadas relativas del icono principal (Escudo)
SHIELD_BASE_COORDS: Final[Tuple[float, ...]] = (64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31)

@lru_cache(maxsize=128)
def _get_scaled_poly(scale: float, canvas_x: float, canvas_y: float) -> Tuple[float, ...]:
    """Escala las coordenadas del polígono del escudo según un factor de zoom."""
    return tuple(canvas_x + (c * scale) if i % 2 == 0 else canvas_y + (c * scale) 
                 for i, c in enumerate(SHIELD_BASE_COORDS))

@lru_cache(maxsize=8)
def logo_svg(size: int = 128) -> str:
    """Genera contenido XML del logo en formato SVG parametrizado con validación de límites."""
    s = max(16, min(1024, int(size)))
    return _SVG_TEMPLATE.format(s=s, stops=_SVG_GRADIENT_STOPS, glow=C_GLOW, bg=C_SURFACE, font=UI_FONT_FAMILY)

def save_logo_svg(destination: Union[str, Path, None], size: int = 128) -> Optional[Path]:
    """
    Guarda el logo en disco usando validaciones de seguridad atómicas pre-escritura.
    Args:
        destination: Ruta de destino (str o Path).
        size: Dimensión en píxeles del logo (16 a 1024).
    Returns: Path de archivo escrito si tuvo éxito, None en caso contrario.
    """
    if destination is None or not isinstance(destination, (str, Path)):
        return None
    try:
        target = Path(destination).resolve()
        
        # Validar ruta de destino antes de intentar crear directorios o escribir
        # Chequeo extra de seguridad: verificar protección del sistema explícitamente
        if is_protected_path(target):
            return None
        safe_path = filter_safe_paths([target])
        if not safe_path or not is_safe_to_modify(target):
            return None
            
        if not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            
        validated_size = max(16, min(1024, int(size)))
        target.write_text(logo_svg(validated_size), encoding="utf-8")
        
        return target if target.is_file() else None
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError, AttributeError, IOError):
        return None

def logo_ascii() -> str:
    """Retorna el logo corporativo en formato de texto ASCII."""
    return "\n   ___  __  __ ___ ___   _\n  / _ \\|  \\/  | __/ __| /_\\\n | (_) | |\\/| | _|| (_ // _ \\\n  \\___/|_|  |_|___\\___/_/ \\_\\\n      Limpieza Total Omega\n"

# Constantes de geometría para el decorado de franjas internas
STRIPE_THICKNESS_SCALE: Final[float] = 92.0
STRIPE_COUNT_FACTOR: Final[float] = 28.0
STRIPE_BASE_Y_OFFSET: Final[float] = 18.0

@lru_cache(maxsize=16)
def _get_stripe_params(scale: float, franjas_count: int) -> Tuple[Tuple[float, float, float], ...]:
    """Calcula dimensiones de franjas decorativas basadas en escala geométrica."""
    thickness_factor = STRIPE_THICKNESS_SCALE * scale
    params = []
    for i in range(franjas_count):
        rel = i / (franjas_count - 1) if franjas_count > 1 else 0
        thickness = thickness_factor * (1.0 if rel < 0.55 else 1.0 - ((rel - 0.55) * 1.9))
        params.append((thickness, i * (thickness_factor / franjas_count), (i + 1) * (thickness_factor / franjas_count)))
    return tuple(params)

@lru_cache(maxsize=128)
def _get_cached_stripe_data(scale: float, franjas_count: int) -> Tuple[Tuple[Tuple[float, float, float], ...], Tuple[ColorSegment, ...]]:
    """Cachea parámetros de franjas y segmentos de color para optimizar el dibujo de escudos."""
    return _get_stripe_params(scale, franjas_count), get_gradient_segments(franjas_count)

def _draw_shield_stripes(canvas: CanvasElement, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Renderiza las franjas geométricas internas del escudo; incluye validación de finitud."""
    try:
        if canvas is None or not math.isfinite(scale) or scale <= 0 or not math.isfinite(canvas_x) or not math.isfinite(canvas_y): return
        
        franjas_count = max(6, int(STRIPE_COUNT_FACTOR * scale))
        base_y_coord = canvas_y + STRIPE_BASE_Y_OFFSET * scale
        center_x_coord = canvas_x + 64 * scale
        
        stripe_dims, color_segments = _get_cached_stripe_data(scale, franjas_count)
        
        for seg in color_segments:
            # Calcular límites de la franja basados en los segmentos de color
            start_y = base_y_coord + stripe_dims[seg.start_index][1]
            end_y = base_y_coord + stripe_dims[seg.end_index - 1][2]
            half_width = stripe_dims[seg.start_index][0]
            
            canvas.create_rectangle(
                center_x_coord - half_width, start_y, 
                center_x_coord + half_width, end_y, 
                fill=seg.hex_color, outline=""
            )
    except (TypeError, ValueError, ZeroDivisionError, IndexError, AttributeError): pass

def _draw_shield_icon_decorations(canvas: CanvasElement, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Renderiza glifo y detalles de superficie sobre el escudo; requiere escalado constante."""
    try:
        if canvas is None or not math.isfinite(scale) or scale <= 0: return
        
        LINE_WIDTH_FACTOR: Final[float] = 8.0
        TEXT_Y_OFFSET: Final[float] = 96.0
        
        canvas.create_line(canvas_x + 41 * scale, canvas_y + 75 * scale, 
                           canvas_x + 75 * scale, canvas_y + 41 * scale, 
                           fill=C_BACKGROUND, width=max(2, int(LINE_WIDTH_FACTOR * scale)), capstyle="round")
        canvas.create_polygon(canvas_x + 75 * scale, canvas_y + 41 * scale, 
                              canvas_x + 89 * scale, canvas_y + 38 * scale, 
                              canvas_x + 92 * scale, canvas_y + 52 * scale, 
                              fill=C_BACKGROUND, outline="")
        canvas.create_text(canvas_x + 64 * scale, canvas_y + TEXT_Y_OFFSET * scale, text="\u03a9", 
                           fill=C_BACKGROUND, font=(UI_FONT_FAMILY, max(8, int(UI_FONT_HEADER_SIZE * scale)), UI_FONT_BOLD))
    except (TypeError, ValueError, AttributeError): pass

def draw_logo(canvas: CanvasElement, size: float = 56.0, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    """
    Renderiza el escudo corporativo compuesto en el canvas.
    Args:
        canvas: Objeto tipo Canvas para dibujar.
        size: Tamaño base del icono.
        canvas_x, canvas_y: Desplazamiento del punto origen.
    """
    try:
        if canvas is None: return
        s = float(size) if size is not None else 56.0
        cx, cy = float(canvas_x) if canvas_x is not None else 0.0, float(canvas_y) if canvas_y is not None else 0.0
        if not math.isfinite(s) or s <= 0 or not math.isfinite(cx) or not math.isfinite(cy): return
        scale = max(0.1, min(10.0, s / 128.0))
        
        canvas.create_oval(
            cx + (64 * scale) - 75 * scale, cy + (58 * scale) - 75 * scale, 
            cx + (64 * scale) + 75 * scale, cy + (58 * scale) + 75 * scale, 
            fill=blend(C_SURFACE, C_GLOW, 0.15), outline=""
        )
        canvas.create_polygon(*_get_scaled_poly(scale, cx, cy), fill=GRADIENT_STOPS[1], outline="")
        _draw_shield_stripes(canvas, cx, cy, scale)
        _draw_shield_icon_decorations(canvas, cx, cy, scale)
    except (TypeError, ValueError, AttributeError): pass

def draw_gradient_bar(canvas: CanvasElement, width: int, height: int = 3, canvas_x: float = 0.0, canvas_y: float = 0.0, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> None:
    """Dibuja barra decorativa horizontal con gradiente segmentado."""
    try:
        if canvas is None or not stops: return
        w_val = max(1, min(4096, int(width)))
        h_val = max(1, min(1024, int(height)))
        cx, cy = float(canvas_x), float(canvas_y)
        if not math.isfinite(cx) or not math.isfinite(cy): return
        
        segments = get_gradient_segments(w_val, stops)
            
        for seg in segments:
            canvas.create_line(cx + seg.start_index, cy, 
                               cx + seg.end_index, cy, 
                               fill=seg.hex_color, width=h_val)
    except (TypeError, ValueError, AttributeError, ZeroDivisionError, IndexError): pass

def draw_ring(canvas: CanvasElement, percent: Union[float, int, None], size: int = 150, 
              canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14, 
              track: Optional[ColorHex] = None, fill: Optional[ColorHex] = None) -> None:
    """
    Dibuja indicador circular de progreso (anillo de carga) con soporte para track y fill.
    Args:
        canvas: Canvas destino.
        percent: Porcentaje completado (0-100).
        size: Diámetro del anillo.
        thickness: Grosor del trazo.
    """
    try:
        if canvas is None: return
        val = float(percent) if percent is not None else 0.0
        val = max(0.0, min(100.0, val)) if math.isfinite(val) else 0.0
        
        diam = max(20, min(2048, int(size)))
        thick = max(2, min(int(thickness), (diam // 2) - 1))
        
        cx, cy = float(canvas_x), float(canvas_y)
        if not math.isfinite(cx) or not math.isfinite(cy): return
        
        borde: float = float(thick) / 2.0
        caja = (cx + borde, cy + borde, cx + diam - borde, cy + diam - borde)
        
        canvas.create_arc(*caja, start=0, extent=359.9, style="arc", outline=track or C_SURFACE_ALT, width=thick)
        if val > 0: 
            fill_color = fill or score_color(val)
            extent = -(val / 100.0 * 359.9)
            if math.isfinite(extent):
                canvas.create_arc(*caja, start=90, extent=extent, style="arc", outline=fill_color, width=thick)
    except (ValueError, TypeError, AttributeError, OverflowError, ZeroDivisionError): pass
