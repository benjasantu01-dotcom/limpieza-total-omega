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

# Plantilla SVG parametrizada: {s} tamaño, {stops} gradientes, {glow} color brillo, {bg} color fondo, {font} tipografía
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
    """Protocolo de interfaz para componentes de dibujo (Canvas) compatibles."""
    def create_rectangle(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = "", outline: str = "", width: int = 1) -> int: ...
    def create_polygon(self, *args: float, fill: str = "", outline: str = "") -> int: ...
    def create_oval(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = "", outline: str = "") -> int: ...
    def create_line(self, x0: float, y0: float, x1: float, y1: float, *, fill: str = "", width: int = 1, capstyle: str = "butt") -> int: ...
    def create_text(self, x: float, y: float, *, text: str, fill: str = "", font: tuple[str, int, str] | str = "", text_anchor: str = "center") -> int: ...
    def create_arc(self, x0: float, y0: float, x1: float, y1: float, *, start: float, extent: float, style: str = "arc", outline: str = "", width: int = 1) -> int: ...

class ColorSegment(NamedTuple):
    """Representa un rango continuo de píxeles que comparten un mismo color."""
    hex_color: ColorHex
    start_index: int
    end_index: int

class PaletteDict(TypedDict):
    """Esquema de colores centralizado para mantener consistencia en toda la UI."""
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
    """Escalado de fuentes: desde títulos destacados (display) hasta notas (caption)."""
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

def color(name: str) -> ColorHex:
    """
    Recupera un color de la paleta.
    Args:
        name: Clave del color en PALETTE.
    Returns:
        HEX del color o gris predeterminado si no existe.
    """
    if not isinstance(name, str):
        return "#808080"
    return PALETTE.get(name, "#808080")

@lru_cache(maxsize=16)
def font_size(name: str) -> int:
    """
    Obtiene el tamaño de fuente configurado para un identificador.
    Args:
        name: Clave en FONT_SIZES (display, body, etc).
    """
    if not isinstance(name, str):
        return UI_FONT_BODY_SIZE
    return FONT_SIZES.get(name, UI_FONT_BODY_SIZE)

@lru_cache(maxsize=32)
def icon(section: Optional[str]) -> str:
    """Retorna el glifo Unicode asociado a una sección."""
    if not isinstance(section, str):
        return "\u2022"
    return ICONS.get(section.strip(), "\u2022")

@lru_cache(maxsize=32)
def tab_label(section: Optional[str]) -> str:
    """Formatea la etiqueta de una pestaña con icono y texto."""
    if not isinstance(section, str): 
        return f"\u2022  Desconocido"
    return f"{icon(section)}  {section}"

def _parse_severity(severity: Optional[str]) -> Optional[SeverityType]:
    """Valida y convierte string a SeverityType."""
    if isinstance(severity, str):
        try:
            return SeverityType(severity.lower())
        except ValueError:
            pass
    return None

@lru_cache(maxsize=16)
def _get_severity_style(severity_key: Optional[SeverityType]) -> Tuple[ColorHex, str]:
    """Interno: recupera tupla (color, nombre) según severidad."""
    if severity_key and (style := SEVERITY_STYLES.get(severity_key)):
        return style
    return (C_TEXT_MUTED, "Desconocido")

def severity_color(severity: Optional[str]) -> ColorHex:
    """Retorna el color HEX para una severidad dada."""
    return _get_severity_style(_parse_severity(severity))[0]

def severity_label(severity: Optional[str]) -> str:
    """Retorna la etiqueta legible para una severidad."""
    sev = _parse_severity(severity)
    if sev:
        return SEVERITY_STYLES[sev][1]
    return severity.capitalize() if isinstance(severity, str) else "Desconocido"

def severity_icon(severity: Optional[str]) -> str:
    """Retorna el glifo unicode de severidad."""
    sev = _parse_severity(severity)
    return SEVERITY_MAP.get(sev, "\u2022") if sev else "\u2022"

def grade_color(grade: Optional[str]) -> ColorHex:
    """Retorna color para una nota (A-F)."""
    if not isinstance(grade, str) or not grade.strip():
        return C_TEXT_MUTED
    return GRADE_COLORS.get(grade.strip().upper()[0], C_TEXT_MUTED)

@lru_cache(maxsize=128)
def score_color(score: Union[float, int, None]) -> ColorHex:
    """
    Calcula el color del score (0-100) según umbrales definidos.
    Example: score_color(95.0) -> "#22e39a" (Success)
    """
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
    """
    Crea una representación en texto plano de una barra de progreso.
    Example: bar(50, width=4) -> "██░░"
    """
    try:
        valor = float(percent) if percent is not None else 0.0
        if not math.isfinite(valor): valor = 0.0
        ancho = max(1, int(width))
        llenos = int(round(max(0.0, min(100.0, valor)) / 100 * ancho))
        return filled * llenos + empty * (ancho - llenos)
    except (TypeError, ValueError):
        return empty * max(1, int(width))

@lru_cache(maxsize=256)
def _hex_to_rgb(value: ColorHex) -> RGBTuple:
    """Transforma HEX a tupla RGB con validación."""
    if isinstance(value, str) and len(value) == 7 and value.startswith('#'):
        try:
            val = int(value[1:], 16)
            return ((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF)
        except (ValueError, TypeError):
            pass
    return (0, 0, 0)

@lru_cache(maxsize=256)
def _rgb_to_hex(rgb: RGBTuple) -> ColorHex:
    """Transforma tupla RGB a HEX con clamping de seguridad."""
    def _clamp(c: Any) -> int: 
        try:
            val = int(c)
            return max(0, min(255, val))
        except (ValueError, TypeError):
            return 0
    return "#{:02x}{:02x}{:02x}".format(_clamp(rgb[0]), _clamp(rgb[1]), _clamp(rgb[2]))

@lru_cache(maxsize=128)
def blend(start: ColorHex, end: ColorHex, ratio: float) -> ColorHex:
    """
    Interpolación lineal entre dos colores.
    Example: blend("#000000", "#FFFFFF", 0.5) -> "#808080"
    """
    try:
        r1, g1, b1 = _hex_to_rgb(start)
        r2, g2, b2 = _hex_to_rgb(end)
        ratio = max(0.0, min(1.0, float(ratio)))
        if not math.isfinite(ratio): ratio = 0.0
        return _rgb_to_hex((
            int(r1 + (r2 - r1) * ratio),
            int(g1 + (g2 - g1) * ratio),
            int(b1 + (b2 - b1) * ratio)
        ))
    except (TypeError, ValueError): return start

@lru_cache(maxsize=32)
def gradient_colors(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorHex, ...]:
    """Genera secuencia de colores para un gradiente dado el número de pasos."""
    n = max(1, int(steps))
    if not stops or len(stops) < 2: 
        return (stops[0] if stops else C_TEXT_MUTED,) * n
    
    rgb_stops = tuple(_hex_to_rgb(s) for s in stops)
    n_segments = len(stops) - 1
    
    res = [""] * n
    for i in range(n):
        ratio = i / (n - 1) if n > 1 else 0.0
        pos = ratio * n_segments
        idx = int(pos)
        if idx >= n_segments: idx = n_segments - 1
        delta = pos - idx
        
        s1, s2 = rgb_stops[idx], rgb_stops[idx + 1]
        res[i] = _rgb_to_hex((
            int(s1[0] + (s2[0] - s1[0]) * delta),
            int(s1[1] + (s2[1] - s1[1]) * delta),
            int(s1[2] + (s2[2] - s1[2]) * delta)
        ))
    return tuple(res)

@lru_cache(maxsize=128)
def _get_grouped_segments(colors: Tuple[ColorHex, ...]) -> Tuple[ColorSegment, ...]:
    """Optimización: agrupa colores idénticos para reducir llamadas de dibujo."""
    if not colors: return ()
    segments = []
    current_color, start = colors[0], 0
    for i in range(1, len(colors)):
        if colors[i] != current_color:
            segments.append(ColorSegment(current_color, start, i))
            current_color, start = colors[i], i
    segments.append(ColorSegment(current_color, start, len(colors)))
    return tuple(segments)

# Coordenadas relativas del icono principal (Escudo)
# Definidas como (x, y) relativas a un viewBox de 128x128
SHIELD_BASE_COORDS: Final[Tuple[float, ...]] = (64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31)

@lru_cache(maxsize=128)
def _get_scaled_poly(scale: float, canvas_x: float, canvas_y: float) -> Tuple[float, ...]:
    """Escala las coordenadas del polígono del escudo según un factor de escala."""
    return tuple(canvas_x + (c * scale) if i % 2 == 0 else canvas_y + (c * scale) 
                 for i, c in enumerate(SHIELD_BASE_COORDS))

@lru_cache(maxsize=8)
def logo_svg(size: int = 128) -> str:
    """Genera contenido XML del logo en formato SVG."""
    s = max(16, min(1024, int(size)))
    return _SVG_TEMPLATE.format(s=s, stops=_SVG_GRADIENT_STOPS, glow=C_GLOW, bg=C_SURFACE, font=UI_FONT_FAMILY)

def save_logo_svg(destination: Union[str, Path, None], size: int = 128) -> Optional[Path]:
    """
    Guarda el logo en disco usando validaciones de seguridad atómicas.
    Returns:
        La ruta del archivo creado o None si la operación no es segura.
    """
    if destination is None: return None
    try:
        path = Path(destination).resolve()
        # Validación de ruta protegida y permisos mediante sistema centralizado
        if is_protected_path(path) or not is_safe_to_modify(path):
            return None
        
        # Verificar estado físico: evitar directorios y asegurar integridad
        if path.exists() and (path.is_dir() or path.is_symlink()):
            return None
        
        # Garantizar jerarquía de carpetas
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            
        validated_size = max(16, min(1024, int(size)))
        path.write_text(logo_svg(validated_size), encoding="utf-8")
        return path if path.exists() and path.is_file() else None
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError, AttributeError):
        return None

def logo_ascii() -> str:
    """Logo corporativo en formato texto plano (ASCII Art)."""
    return "\n   ___  __  __ ___ ___   _\n  / _ \\|  \\/  | __/ __| /_\\\n | (_) | |\\/| | _|| (_ // _ \\\n  \\___/|_|  |_|___\\___/_/ \\_\\\n      Limpieza Total Omega\n"

# Constantes de geometría para el decorado de franjas internas
STRIPE_THICKNESS_SCALE: Final[float] = 92.0
STRIPE_COUNT_FACTOR: Final[float] = 28.0
STRIPE_BASE_Y_OFFSET: Final[float] = 18.0

@lru_cache(maxsize=16)
def _get_stripe_params(scale: float, franjas_count: int) -> Tuple[Tuple[float, float, float], ...]:
    """Calcula la geometría (ancho, y_start, y_end) de cada franja decorativa basada en escala."""
    return tuple((STRIPE_THICKNESS_SCALE * scale * (1.0 if (i / (franjas_count - 1)) < 0.55 else 1.0 - (((i / (franjas_count - 1)) - 0.55) * 1.9)),
                  i * (STRIPE_THICKNESS_SCALE * scale / franjas_count),
                  (i + 1) * (STRIPE_THICKNESS_SCALE * scale / franjas_count)) for i in range(franjas_count))

@lru_cache(maxsize=128)
def _get_cached_stripe_data(scale: float, franjas_count: int) -> Tuple[Tuple[Tuple[float, float, float], ...], Tuple[ColorHex, ...]]:
    """Cachea parámetros de franjas y sus colores correspondientes."""
    return _get_stripe_params(scale, franjas_count), gradient_colors(franjas_count)

def _draw_shield_stripes(canvas: CanvasElement, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Renderiza las franjas internas geométricas del escudo."""
    try:
        if canvas is None or not math.isfinite(scale) or scale <= 0: return
        franjas_count = max(6, int(STRIPE_COUNT_FACTOR * scale))
        base_y = canvas_y + STRIPE_BASE_Y_OFFSET * scale
        center_x = canvas_x + 64 * scale
        params, colors = _get_cached_stripe_data(scale, franjas_count)
        
        for i, hex_color in enumerate(colors):
            w, y_start, y_end = params[i]
            canvas.create_rectangle(center_x - w, base_y + y_start, 
                                    center_x + w, base_y + y_end, 
                                    fill=hex_color, outline="")
    except (TypeError, ValueError, ZeroDivisionError, IndexError, AttributeError): pass

def _draw_shield_icon_decorations(canvas: CanvasElement, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Renderiza glifo y decoraciones superficiales del escudo (línea diagonal y omega)."""
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
    """Renderiza el escudo corporativo compuesto (Polígono, franjas y glifo)."""
    try:
        if canvas is None: return
        s = float(size)
        cx, cy = float(canvas_x), float(canvas_y)
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
    """Dibuja barra decorativa con gradiente segmentado para optimizar el número de elementos Canvas."""
    try:
        if canvas is None or stops is None: return
        w_val = max(1, min(4096, int(width)))
        h_val = max(1, min(1024, int(height)))
        cx, cy = float(canvas_x), float(canvas_y)
        if not math.isfinite(cx) or not math.isfinite(cy): return
        
        segments = _get_grouped_segments(gradient_colors(w_val, stops))
            
        for segment in segments:
            canvas.create_line(cx + segment.start_index, cy, 
                               cx + segment.end_index, cy, 
                               fill=segment.hex_color, width=h_val)
    except (TypeError, ValueError, AttributeError): pass

def draw_ring(canvas: CanvasElement, percent: Union[float, int, None], size: int = 150, 
              canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14, 
              track: Optional[ColorHex] = None, fill: Optional[ColorHex] = None) -> None:
    """
    Dibuja indicador circular de progreso (anillo de carga) con validación de límites.
    Example: draw_ring(canvas, 75, size=100) -> Dibuja un arco de 75%
    """
    try:
        if canvas is None: return
        val = float(percent) if percent is not None else 0.0
        if not math.isfinite(val): val = 0.0
        val = max(0.0, min(100.0, val))
        
        diam = max(20, min(2048, int(size)))
        max_thick = (diam // 2) - 1
        thick = max(2, min(int(thickness), max_thick))
        
        cx, cy = float(canvas_x), float(canvas_y)
        if not math.isfinite(cx) or not math.isfinite(cy): return
        borde: float = float(thick) / 2.0
        caja = (cx + borde, cy + borde, cx + diam - borde, cy + diam - borde)
        
        canvas.create_arc(*caja, start=0, extent=359.9, style="arc", outline=track or C_SURFACE_ALT, width=thick)
        if val > 0: 
            fill_color = fill or score_color(val)
            canvas.create_arc(*caja, start=90, extent=-(val / 100 * 359.9), style="arc", outline=fill_color, width=thick)
    except (ValueError, TypeError, AttributeError, OverflowError, ZeroDivisionError): return
