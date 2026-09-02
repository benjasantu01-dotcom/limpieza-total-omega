"""
branding.py — identidad visual de Limpieza Total Omega.

Centraliza nombre, versión, paleta, tipografía, iconografía y logo.

GLOSARIO VISUAL:
  - Surface: Fondos de contenedores y áreas de trabajo.
  - Accent: Colores de marca para llamados a la acción o elementos destacados.
  - Glow: Efectos de iluminación sutil para resaltar estados de salud.
  - Severity: Código cromático para niveles de riesgo (OK, Info, Warning, Danger).
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Final, TypeAlias, Literal, Mapping, Tuple, List, Optional, Union, TypedDict, Protocol, NamedTuple
from types import MappingProxyType
from functools import lru_cache
from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

class CanvasElement(Protocol):
    """Protocolo que define los métodos mínimos requeridos por el sistema de dibujo de customtkinter."""
    def create_rectangle(self, *args: float, **kwargs: Any) -> int: ...
    def create_polygon(self, *args: float, **kwargs: Any) -> int: ...
    def create_oval(self, *args: float, **kwargs: Any) -> int: ...
    def create_line(self, *args: float, **kwargs: Any) -> int: ...
    def create_text(self, *args: float, **kwargs: Any) -> int: ...
    def create_arc(self, *args: float, **kwargs: Any) -> int: ...

class ColorSegment(NamedTuple):
    """Representa un rango contiguo de píxeles/unidades con un color constante en un gradiente."""
    hex_color: HexColor
    start_index: int
    end_index: int

HexColor: TypeAlias = str  
SeverityLevel: TypeAlias = Literal["ok", "info", "warning", "danger"]
GradeKey: TypeAlias = Literal["A", "B", "C", "D", "F"]
SeverityStyle: TypeAlias = Tuple[HexColor, str]  
RGBTuple: TypeAlias = Tuple[int, int, int]  

class PaletteDict(TypedDict):
    background: HexColor
    surface: HexColor
    surface_alt: HexColor
    surface_hover: HexColor
    card: HexColor
    accent: HexColor
    accent_hover: HexColor
    accent_dim: HexColor
    accent2: HexColor
    accent2_hover: HexColor
    accent3: HexColor
    success: HexColor
    info: HexColor
    warning: HexColor
    danger: HexColor
    danger_hover: HexColor
    text: HexColor
    text_muted: HexColor
    text_dim: HexColor
    border: HexColor
    glow: HexColor

class FontSizesDict(TypedDict):
    display: int
    title: int
    subtitle: int
    heading: int
    body: int
    mono: int
    caption: int

APP_NAME: Final[str] = "Limpieza Total Omega"
APP_SHORT_NAME: Final[str] = "Omega"
APP_TAGLINE: Final[str] = "Limpieza y seguridad, en un solo lugar"
APP_VERSION: Final[str] = "2.1.0"

UI_FONT_FAMILY: Final[str] = "Segoe UI"
UI_FONT_BOLD: Final[str] = "bold"

PALETTE: Final[Mapping[str, HexColor]] = MappingProxyType({
    "background": "#0a0e17", "surface": "#141b2d", "surface_alt": "#1e2740",
    "surface_hover": "#28324f", "card": "#182135", "accent": "#00f0c0",
    "accent_hover": "#00d0a4", "accent_dim": "#0a6b58", "accent2": "#7c5cff",
    "accent2_hover": "#6a48f0", "accent3": "#ff2d78", "success": "#22e39a",
    "info": "#38bdf8", "warning": "#ffb020", "danger": "#ff4757",
    "danger_hover": "#e02e3d", "text": "#f0f6fc", "text_muted": "#94a3b8",
    "text_dim": "#5c6b85", "border": "#2a3654", "glow": "#00f0c0",
})

C_SURFACE: Final[HexColor] = PALETTE["surface"]
C_BACKGROUND: Final[HexColor] = PALETTE["background"]
C_GLOW: Final[HexColor] = PALETTE["glow"]
C_TEXT_MUTED: Final[HexColor] = PALETTE["text_muted"]
C_SUCCESS: Final[HexColor] = PALETTE["success"]
C_INFO: Final[HexColor] = PALETTE["info"]
C_WARNING: Final[HexColor] = PALETTE["warning"]
C_DANGER: Final[HexColor] = PALETTE["danger"]
C_SURFACE_ALT: Final[HexColor] = PALETTE["surface_alt"]

FONT_SIZES: Final[Mapping[str, int]] = MappingProxyType({
    "display": 46, "title": 26, "subtitle": 13, "heading": 16,
    "body": 12, "mono": 11, "caption": 10,
})

SEVERITY_STYLES: Final[Mapping[SeverityLevel, SeverityStyle]] = MappingProxyType({
    "ok": (C_SUCCESS, "Correcto"),
    "info": (C_INFO, "Informativo"),
    "warning": (C_WARNING, "Advertencia"),
    "danger": (C_DANGER, "Peligro"),
})

GRADE_COLORS: Final[Mapping[str, HexColor]] = MappingProxyType({
    "A": C_SUCCESS, "B": C_INFO, "C": C_WARNING, "D": "#ff7b39", "F": C_DANGER,
})

ICONS: Final[Mapping[str, str]] = MappingProxyType({
    "Salud": "\u25c9", "Limpieza": "\u2726", "Seguridad": "\u26ca",
    "Cuarentena": "\u2297", "Memoria": "\u25a4", "Disco": "\u25f4",
    "Duplicados": "\u29c9", "Navegadores": "\u25d0", "Inicio": "\u23fb",
    "Informe": "\u2263", "Asistente": "\u273b", "Ajustes": "\u2699",
})

GRADIENT_STOPS: Final[Tuple[HexColor, ...]] = ("#00f0c0", "#7c5cff", "#ff2d78")

SCORE_THRESHOLDS: Final[Tuple[Tuple[float, HexColor], ...]] = (
    (90.0, C_SUCCESS), (80.0, C_INFO), (65.0, C_WARNING), (50.0, "#ff7b39")
)

def app_title() -> str:
    """Retorna el nombre y versión actual de la aplicación."""
    return f"{APP_NAME} v{APP_VERSION}"

@lru_cache(maxsize=16)
def color(name: str) -> HexColor:
    """Retorna un color hexadecimal de la paleta global dado su identificador."""
    return PALETTE.get(name, "#808080")

@lru_cache(maxsize=8)
def font_size(name: str) -> int:
    """Retorna el tamaño en puntos para una categoría tipográfica."""
    return FONT_SIZES.get(name, 12)

@lru_cache(maxsize=16)
def icon(section: Optional[str]) -> str:
    """Retorna el carácter Unicode representativo de una sección de la UI."""
    return ICONS.get(section.strip(), "\u2022") if isinstance(section, str) else "\u2022"

@lru_cache(maxsize=16)
def tab_label(section: str) -> str:
    """Combina el icono y el nombre para etiquetas de pestañas."""
    return f"{icon(section)}  {section}"

@lru_cache(maxsize=8)
def severity_color(severity: Optional[str]) -> HexColor:
    """Selecciona el color según el nivel de severidad especificado."""
    if severity and (style := SEVERITY_STYLES.get(severity.lower())):
        return style[0]
    return C_TEXT_MUTED

@lru_cache(maxsize=8)
def severity_label(severity: Optional[str]) -> str:
    """Retorna el nombre legible del nivel de riesgo."""
    if severity and (style := SEVERITY_STYLES.get(severity.lower())):
        return style[1]
    return severity.capitalize() if severity else "Desconocido"

def severity_icon(severity: Optional[str]) -> str:
    """Retorna un símbolo de estado crítico o informativo."""
    simbolos: Final[Mapping[str, str]] = MappingProxyType({"ok": "\u2713", "info": "\u2139", "warning": "\u26a0", "danger": "\u2716"})
    return simbolos.get(severity.lower(), "\u2022") if isinstance(severity, str) else "\u2022"

@lru_cache(maxsize=8)
def grade_color(grade: Optional[str]) -> HexColor:
    """Mapea una calificación (A-F) a su color correspondiente."""
    if grade and grade.strip():
        return GRADE_COLORS.get(grade.upper()[0], C_TEXT_MUTED)
    return C_TEXT_MUTED

@lru_cache(maxsize=64)
def score_color(score: Union[float, int, None]) -> HexColor:
    """Calcula el color representativo para un puntaje de 0 a 100 según umbrales definidos."""
    if score is None: return C_TEXT_MUTED
    try:
        valor = float(score)
    except (TypeError, ValueError): return C_TEXT_MUTED
    if not (0.0 <= valor <= 100.0): return C_TEXT_MUTED
    for limit, color_val in SCORE_THRESHOLDS:
        if valor >= limit: return color_val
    return C_DANGER

@lru_cache(maxsize=32)
def bar(percent: Union[float, int, None], width: int = 24,
        filled: str = "\u2588", empty: str = "\u2591") -> str:
    """Genera una barra de progreso textual con caracteres Unicode."""
    try:
        valor = float(percent) if percent is not None else 0.0
        ancho = max(1, int(width))
        llenos = int(round(max(0.0, min(100.0, valor)) / 100 * ancho))
        return filled * llenos + empty * (ancho - llenos)
    except (TypeError, ValueError):
        return empty * max(1, int(width))

def _get_rgb_safe(val: float) -> int:
    """Asegura que un valor sea un entero válido en el rango [0, 255]."""
    return max(0, min(255, int(val)))

@lru_cache(maxsize=64)
def _hex_to_rgb(value: HexColor) -> RGBTuple:
    """Convierte un color hexadecimal en una tupla (R, G, B)."""
    if len(value) != 7 or not value.startswith("#"): return (0, 0, 0)
    try:
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    except (ValueError, IndexError): return (0, 0, 0)

def _rgb_to_hex(rgb: RGBTuple) -> HexColor:
    """Convierte una tupla (R, G, B) a su representación hexadecimal."""
    return "#{:02x}{:02x}{:02x}".format(*map(_get_rgb_safe, rgb))

@lru_cache(maxsize=32)
def blend(start: HexColor, end: HexColor, ratio: float) -> HexColor:
    """Mezcla linealmente dos colores hexadecimales basándose en un ratio [0.0, 1.0]."""
    if start == end: return start
    r1, g1, b1 = _hex_to_rgb(start)
    r2, g2, b2 = _hex_to_rgb(end)
    ratio = max(0.0, min(1.0, float(ratio)))
    return _rgb_to_hex((
        int(r1 + (r2 - r1) * ratio),
        int(g1 + (g2 - g1) * ratio),
        int(b1 + (b2 - b1) * ratio)
    ))

@lru_cache(maxsize=16)
def gradient_colors(steps: int, stops: Tuple[HexColor, ...] = GRADIENT_STOPS) -> Tuple[HexColor, ...]:
    """Genera una secuencia de colores interpolados para crear efectos de gradiente."""
    n = max(1, int(steps))
    if not stops: return (C_GLOW,) * n
    if len(stops) < 2: return (stops[0],) * n
    rgb_stops = [_hex_to_rgb(s) for s in stops]
    tramos = len(stops) - 1
    
    out = []
    for i in range(n):
        pos = (i / (n - 1) * tramos) if n > 1 else 0
        idx = min(int(pos), tramos - 1)
        delta = pos - idx
        s1, s2 = rgb_stops[idx], rgb_stops[idx + 1]
        out.append(_rgb_to_hex((
            int(s1[0] + (s2[0] - s1[0]) * delta),
            int(s1[1] + (s2[1] - s1[1]) * delta),
            int(s1[2] + (s2[2] - s1[2]) * delta)
        )))
    return tuple(out)

@lru_cache(maxsize=16)
def _get_grouped_segments(colors: Tuple[HexColor, ...]) -> Tuple[ColorSegment, ...]:
    """Agrupa colores consecutivos idénticos para optimizar operaciones de dibujo en Canvas."""
    if not colors: return ()
    segments = []
    current_color = colors[0]
    start = 0
    for i in range(1, len(colors)):
        if colors[i] != current_color:
            segments.append(ColorSegment(current_color, start, i))
            current_color = colors[i]
            start = i
    segments.append(ColorSegment(current_color, start, len(colors)))
    return tuple(segments)

@lru_cache(maxsize=8)
def _get_shield_coords(s: float) -> Tuple[float, ...]:
    """Retorna las coordenadas escaladas del polígono del escudo."""
    base: Final[Tuple[float, ...]] = (64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31)
    return tuple(v * float(s) for v in base)

@lru_cache(maxsize=4)
def logo_svg(size: int = 128) -> str:
    """Genera el código fuente SVG del logotipo principal de la aplicación."""
    s = max(1, min(4096, int(size)))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="omegaShield" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{GRADIENT_STOPS[0]}"/>
      <stop offset="55%" stop-color="{GRADIENT_STOPS[1]}"/>
      <stop offset="100%" stop-color="{GRADIENT_STOPS[2]}"/>
    </linearGradient>
    <radialGradient id="omegaGlow" cx="0.5" cy="0.4" r="0.6">
      <stop offset="0%" stop-color="{C_GLOW}" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="{C_GLOW}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="128" height="128" rx="30" fill="{C_SURFACE}"/>
  <circle cx="64" cy="56" r="52" fill="url(#omegaGlow)"/>
  <path d="M64 18 L100 31 V67 C100 90 83 104 64 110 C45 104 28 90 28 67 V31 Z" fill="url(#omegaShield)"/>
  <path d="M41 75 L75 41" stroke="{C_BACKGROUND}" stroke-width="8" stroke-linecap="round"/>
  <path d="M75 41 L89 38 L92 52 Z" fill="{C_BACKGROUND}"/>
  <text x="64" y="98" font-family="{UI_FONT_FAMILY}" font-size="26" font-weight="{UI_FONT_BOLD}" fill="{C_BACKGROUND}" text-anchor="middle">&#937;</text>
</svg>"""

def save_logo_svg(destination: Union[str, Path, None]) -> Optional[Path]:
    """Persiste el SVG del logo en el sistema de archivos tras verificar seguridad."""
    if not destination or len(str(destination)) > 4096: return None
    try:
        path_obj = Path(destination).resolve()
        # Verificar parent antes de intentar escritura para evitar race conditions básicas
        parent_dir = path_obj.parent
        ensure_safe_to_modify(path_obj)
        ensure_safe_to_modify(parent_dir)
        
        if not parent_dir.exists():
            parent_dir.mkdir(parents=True, exist_ok=True)
        elif not parent_dir.is_dir():
            return None
            
        path_obj.write_text(logo_svg(), encoding="utf-8")
        return path_obj
    except (OSError, PermissionError, TypeError, ValueError, RuntimeError):
        return None

def logo_ascii() -> str:
    """Retorna el logo en formato de texto ASCII para logs de consola."""
    return "\n   ___  __  __ ___ ___   _\n  / _ \\|  \\/  | __/ __| /_\\\n | (_) | |\\/| | _|| (_ // _ \\\n  \\___/|_|  |_|___\\___/_/ \\_\\\n      Limpieza Total Omega\n"

def _draw_shield_stripes(canvas: CanvasElement, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Dibuja las franjas decorativas internas del escudo sobre un componente Canvas."""
    try:
        scale_f = float(scale)
        franjas_count = max(6, int(28 * scale_f))
        colores = gradient_colors(franjas_count)
        base_y = canvas_y + 18 * scale_f
        factor_y = 92 * scale_f / franjas_count
        center_x = canvas_x + 64 * scale_f
        for segment in _get_grouped_segments(colores):
            mid = (segment.start_index + segment.end_index) / 2
            progreso = mid / max(1.0, float(franjas_count - 1))
            w = 36 * scale_f * (1.0 if progreso < 0.55 else 1.0 - (progreso - 0.55) * 1.9)
            canvas.create_rectangle(center_x - w, base_y + segment.start_index * factor_y, center_x + w, base_y + segment.end_index * factor_y + 1, fill=segment.hex_color, outline="")
    except (AttributeError, TypeError, ValueError, ZeroDivisionError): pass

def _draw_shield_icon_decorations(canvas: CanvasElement, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Dibuja la simbología interna (línea y Omega) del escudo."""
    canvas.create_line(canvas_x + 41 * scale, canvas_y + 75 * scale, canvas_x + 75 * scale, canvas_y + 41 * scale, fill=C_BACKGROUND, width=max(2, int(8 * scale)), capstyle="round")
    canvas.create_polygon(canvas_x + 75 * scale, canvas_y + 41 * scale, canvas_x + 89 * scale, canvas_y + 38 * scale, canvas_x + 92 * scale, canvas_y + 52 * scale, fill=C_BACKGROUND, outline="")
    canvas.create_text(canvas_x + 64 * scale, canvas_y + 96 * scale, text="\u03a9", fill=C_BACKGROUND, font=(UI_FONT_FAMILY, max(8, int(23 * scale)), UI_FONT_BOLD))

def draw_logo(canvas: CanvasElement, size: float = 56.0, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    """Dibuja el escudo del logo en un componente Canvas dado, manejando la escala y capas de brillo."""
    try:
        scale = max(0.1, min(10.0, float(size) / 128.0))
        coords = _get_shield_coords(scale)
        contorno = [canvas_x + coords[i] if i % 2 == 0 else canvas_y + coords[i] for i in range(len(coords))]
        for paso in range(4, 0, -1):
            r = 56 * scale * (0.6 + paso * 0.12)
            canvas.create_oval(canvas_x + 64 * scale - r, canvas_y + 58 * scale - r, canvas_x + 64 * scale + r, canvas_y + 58 * scale + r, fill=blend(C_SURFACE, C_GLOW, 0.04 * paso), outline="")
        canvas.create_polygon(contorno, fill=GRADIENT_STOPS[1], outline="")
        _draw_shield_stripes(canvas, canvas_x, canvas_y, scale)
        _draw_shield_icon_decorations(canvas, canvas_x, canvas_y, scale)
    except (ValueError, TypeError, AttributeError, ZeroDivisionError, OverflowError): pass

def draw_gradient_bar(canvas: CanvasElement, width: int, height: int = 3, canvas_x: float = 0.0, canvas_y: float = 0.0, stops: Tuple[HexColor, ...] = GRADIENT_STOPS) -> None:
    """Dibuja una línea decorativa con degradado sobre el Canvas."""
    try:
        w_int = max(1, int(width))
        for segment in _get_grouped_segments(gradient_colors(w_int, stops)):
            canvas.create_line(canvas_x + segment.start_index, canvas_y, canvas_x + segment.end_index, canvas_y, fill=segment.hex_color, width=max(1, int(height)))
    except (ValueError, TypeError, AttributeError): pass

def draw_ring(canvas: CanvasElement, percent: Union[float, int, None], size: int = 150, canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14, track: Optional[HexColor] = None, fill: Optional[HexColor] = None) -> None:
    """Dibuja un indicador circular de progreso basado en el estado (score)."""
    try:
        if percent is None: return
        valor = float(percent)
        diametro = max(20, int(size))
        grosor = max(2, min(int(thickness), (diametro // 2) - 1))
        color_fondo = track if isinstance(track, str) else C_SURFACE_ALT
        color_avance = fill if isinstance(fill, str) else score_color(valor)
        borde = grosor / 2.0
        caja = (canvas_x + borde, canvas_y + borde, canvas_x + diametro - borde, canvas_y + diametro - borde)
        canvas.create_arc(*caja, start=0, extent=359.9, style="arc", outline=color_fondo, width=grosor)
        if valor > 0: canvas.create_arc(*caja, start=90, extent=-(max(0.0, min(100.0, valor)) / 100 * 359.9), style="arc", outline=color_avance, width=grosor)
    except (TypeError, ValueError, AttributeError): return
