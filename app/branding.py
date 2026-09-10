"""
branding.py — identidad visual de Limpieza Total Omega.

Centraliza la gestión de activos visuales, paletas de colores, jerarquías 
tipográficas y sistemas de renderizado vectorial (SVG/Canvas).

GLOSARIO VISUAL:
  - Surface: Fondos de contenedores y áreas de trabajo.
  - Accent: Colores de marca para llamados a la carga o elementos destacados.
  - Glow: Efectos de iluminación sutil para resaltar estados de salud.
  - Severity: Código cromático para niveles de riesgo (OK, Info, Warning, Danger).

NOTA DE SEGURIDAD:
  Las funciones de dibujo (Canvas) y generación de archivos (SVG) operan 
  bajo principios de diseño defensivo, capturando excepciones de renderizado 
  para evitar que una paleta mal configurada o una entrada inválida 
  detengan el hilo principal de la aplicación.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Final, TypeAlias, Literal, Mapping, Tuple, List, Optional, Union, TypedDict, Protocol, NamedTuple
from types import MappingProxyType
from functools import lru_cache
from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path
import math

class CanvasElement(Protocol):
    """
    Protocolo Duck-typing que define los métodos necesarios para la integración 
    con `customtkinter.CTkCanvas`. Asegura compatibilidad con el sistema de 
    dibujo primitivo sin acoplamiento fuerte.
    """
    def create_rectangle(self, *args: float, **kwargs: Any) -> int: ...
    def create_polygon(self, *args: float, **kwargs: Any) -> int: ...
    def create_oval(self, *args: float, **kwargs: Any) -> int: ...
    def create_line(self, *args: float, **kwargs: Any) -> int: ...
    def create_text(self, *args: float, **kwargs: Any) -> int: ...
    def create_arc(self, *args: float, **kwargs: Any) -> int: ...

class ColorSegment(NamedTuple):
    """
    Representa un rango de píxeles/pasos continuo para un mismo color.
    Utilizado por los renderizadores para reducir llamadas individuales al canvas.
    """
    hex_color: HexColor
    start_index: int
    end_index: int

HexColor: TypeAlias = str  
SeverityLevel: TypeAlias = Literal["ok", "info", "warning", "danger"]
GradeKey: TypeAlias = Literal["A", "B", "C", "D", "F"]
SeverityStyle: TypeAlias = Tuple[HexColor, str]  
RGBTuple: TypeAlias = Tuple[int, int, int]  

class PaletteDict(TypedDict):
    """Estructura esperada de la paleta de colores de marca."""
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
    """Mapeo de roles de texto a tamaños de fuente (píxeles)."""
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

UI_FONT_HEADER_SIZE: Final[int] = 23
UI_FONT_BODY_SIZE: Final[int] = 12

_PALETTE_MAP: Final[dict[str, HexColor]] = {
    "background": "#0a0e17", "surface": "#141b2d", "surface_alt": "#1e2740",
    "surface_hover": "#28324f", "card": "#182135", "accent": "#00f0c0",
    "accent_hover": "#00d0a4", "accent_dim": "#0a6b58", "accent2": "#7c5cff",
    "accent2_hover": "#6a48f0", "accent3": "#ff2d78", "success": "#22e39a",
    "info": "#38bdf8", "warning": "#ffb020", "danger": "#ff4757",
    "danger_hover": "#e02e3d", "text": "#f0f6fc", "text_muted": "#94a3b8",
    "text_dim": "#5c6b85", "border": "#2a3654", "glow": "#00f0c0",
}
PALETTE: Final[Mapping[str, HexColor]] = MappingProxyType(_PALETTE_MAP)

C_SURFACE: Final[HexColor] = _PALETTE_MAP["surface"]
C_BACKGROUND: Final[HexColor] = _PALETTE_MAP["background"]
C_GLOW: Final[HexColor] = _PALETTE_MAP["glow"]
C_TEXT_MUTED: Final[HexColor] = _PALETTE_MAP["text_muted"]
C_SUCCESS: Final[HexColor] = _PALETTE_MAP["success"]
C_INFO: Final[HexColor] = _PALETTE_MAP["info"]
C_WARNING: Final[HexColor] = _PALETTE_MAP["warning"]
C_DANGER: Final[HexColor] = _PALETTE_MAP["danger"]
C_SURFACE_ALT: Final[HexColor] = _PALETTE_MAP["surface_alt"]

FONT_SIZES: Final[Mapping[str, int]] = MappingProxyType({
    "display": 46, "title": 26, "subtitle": 13, "heading": 16,
    "body": UI_FONT_BODY_SIZE, "mono": 11, "caption": 10,
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

_SEVERITY_MAP: Final[Mapping[str, str]] = MappingProxyType({"ok": "\u2713", "info": "\u2139", "warning": "\u26a0", "danger": "\u2716"})

def app_title() -> str:
    """Retorna el título completo de la aplicación incluyendo la versión actual."""
    return f"{APP_NAME} v{APP_VERSION}"

def color(name: str) -> HexColor:
    """Busca un color en la paleta global; retorna gris por defecto si la clave es inválida."""
    return _PALETTE_MAP.get(name, "#808080")

@lru_cache(maxsize=16)
def font_size(name: str) -> int:
    """Recupera el tamaño de fuente definido para un rol específico."""
    return FONT_SIZES.get(name, UI_FONT_BODY_SIZE)

@lru_cache(maxsize=32)
def icon(section: Optional[str]) -> str:
    """Obtiene el símbolo Unicode asociado al nombre de la sección."""
    return ICONS.get(section.strip(), "\u2022") if isinstance(section, str) else "\u2022"

@lru_cache(maxsize=32)
def tab_label(section: str) -> str:
    """Concatena el icono correspondiente con el nombre de la sección para pestañas de interfaz."""
    return f"{icon(section)}  {section}"

@lru_cache(maxsize=16)
def _get_severity_style(severity: Optional[str]) -> Tuple[HexColor, str]:
    """Helper interno: recupera color y etiqueta descriptiva según nivel de severidad."""
    if severity and (style := SEVERITY_STYLES.get(severity.lower())):
        return style
    return (C_TEXT_MUTED, "Desconocido")

def severity_color(severity: Optional[str]) -> HexColor:
    """Devuelve el código hexadecimal correspondiente a la severidad dada."""
    return _get_severity_style(severity)[0]

def severity_label(severity: Optional[str]) -> str:
    """Devuelve una cadena legible para el usuario representando la severidad."""
    if severity and severity.lower() in SEVERITY_STYLES:
        return _get_severity_style(severity)[1]
    return severity.capitalize() if severity else "Desconocido"

def severity_icon(severity: Optional[str]) -> str:
    """Devuelve el carácter representativo del nivel de severidad."""
    return _SEVERITY_MAP.get(severity.lower(), "\u2022") if isinstance(severity, str) else "\u2022"

def grade_color(grade: Optional[str]) -> HexColor:
    """Asigna un color a las calificaciones escolares (A-F)."""
    if grade and grade.strip():
        return GRADE_COLORS.get(grade.upper()[0], C_TEXT_MUTED)
    return C_TEXT_MUTED

@lru_cache(maxsize=128)
def score_color(score: Union[float, int, None]) -> HexColor:
    """Determina el color según puntaje (0-100) basándose en umbrales de salud definidos."""
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
    Renderiza una barra de progreso en texto para CLI o logs.
    Escala el progreso entre 0 y 100 al ancho especificado.
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
def _hex_to_rgb(value: HexColor) -> RGBTuple:
    """Convierte color #RRGGBB a una tupla de enteros (0-255)."""
    if len(value) != 7 or value[0] != "#": return (0, 0, 0)
    try:
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    except (ValueError, IndexError): return (0, 0, 0)

@lru_cache(maxsize=256)
def _rgb_to_hex(rgb: RGBTuple) -> HexColor:
    """Convierte tupla (r, g, b) de vuelta a formato hexadecimal."""
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, c)) for c in rgb])

@lru_cache(maxsize=128)
def blend(start: HexColor, end: HexColor, ratio: float) -> HexColor:
    """Realiza una interpolación lineal entre dos colores para crear tonos intermedios."""
    if start == end: return start
    r1, g1, b1 = _hex_to_rgb(start)
    r2, g2, b2 = _hex_to_rgb(end)
    ratio = max(0.0, min(1.0, ratio))
    return _rgb_to_hex((
        int(r1 + (r2 - r1) * ratio),
        int(g1 + (g2 - g1) * ratio),
        int(b1 + (b2 - b1) * ratio)
    ))

@lru_cache(maxsize=64)
def gradient_colors(steps: int, stops: Tuple[HexColor, ...] = GRADIENT_STOPS) -> Tuple[HexColor, ...]:
    """Genera una secuencia suavizada de colores mediante interpolación entre paradas."""
    n = max(1, int(steps))
    if not stops or len(stops) < 2: return (stops[0] if stops else C_TEXT_MUTED,) * n
    
    rgb_stops = tuple(_hex_to_rgb(s) for s in stops)
    tramos = len(stops) - 1
    res: List[HexColor] = [C_TEXT_MUTED] * n
    
    for i in range(n):
        pos = (i / (n - 1) * tramos) if n > 1 else 0
        idx = min(int(pos), tramos - 1)
        delta = pos - idx
        s1, s2 = rgb_stops[idx], rgb_stops[idx + 1]
        res[i] = _rgb_to_hex((
            int(s1[0] + (s2[0] - s1[0]) * delta),
            int(s1[1] + (s2[1] - s1[1]) * delta),
            int(s1[2] + (s2[2] - s1[2]) * delta)
        ))
    return tuple(res)

@lru_cache(maxsize=64)
def _get_grouped_segments(colors: Tuple[HexColor, ...]) -> Tuple[ColorSegment, ...]:
    """Agrupa colores consecutivos iguales para optimizar el dibujo de rectángulos en Canvas."""
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
    """Calcula coordenadas escaladas para el trazado del icono del escudo."""
    base: Final[Tuple[float, ...]] = (64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31)
    return tuple(v * s for v in base)

@lru_cache(maxsize=8)
def logo_svg(size: int = 128) -> str:
    """Genera una cadena XML representando el logo de la marca en formato SVG."""
    s = max(1, min(4096, int(size)))
    stops = "\n".join([f'      <stop offset="{o}" stop-color="{c}"/>' 
                       for o, c in zip(["0%", "55%", "100%"], GRADIENT_STOPS)])
    
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="omegaShield" x1="0" y1="0" x2="1" y2="1">{stops}    </linearGradient>
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
    """Guarda el logo vectorial en una ruta física tras validación de seguridad de escritura."""
    try:
        if destination is None: return None
        path_input = Path(str(destination)).absolute()
        
        # Validaciones: si es directorio o ruta protegida, abortar.
        if path_input.is_dir() or is_protected_path(path_input):
            return None
        
        # Verifica integridad y permisos antes de cualquier operación
        ensure_safe_to_modify(path_input)
            
        if not path_input.parent.exists():
            path_input.parent.mkdir(parents=True, exist_ok=True)
            
        path_input.write_text(logo_svg(), encoding="utf-8")
        return path_input
    except (OSError, PermissionError, RuntimeError, ValueError, TypeError): 
        return None

def logo_ascii() -> str:
    """Retorna un arte ASCII para identificar la marca en logs de ejecución."""
    return "\n   ___  __  __ ___ ___   _\n  / _ \\|  \\/  | __/ __| /_\\\n | (_) | |\\/| | _|| (_ // _ \\\n  \\___/|_|  |_|___\\___/_/ \\_\\\n      Limpieza Total Omega\n"

def _draw_shield_stripes(canvas: CanvasElement, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Dibuja franjas decorativas graduadas sobre el componente gráfico del escudo."""
    try:
        franjas_count = max(6, int(28 * scale))
        colores = gradient_colors(franjas_count)
        base_y = canvas_y + 18 * scale
        factor_y = 92 * scale / franjas_count
        center_x = canvas_x + 64 * scale
        for seg in _get_grouped_segments(colores):
            mid = (seg.start_index + seg.end_index) / 2
            progreso = mid / max(1.0, float(franjas_count - 1))
            w = 36 * scale * (1.0 if progreso < 0.55 else 1.0 - (progreso - 0.55) * 1.9)
            canvas.create_rectangle(center_x - w, base_y + seg.start_index * factor_y, center_x + w, base_y + seg.end_index * factor_y + 1, fill=seg.hex_color, outline="")
    except Exception: pass

def _draw_shield_icon_decorations(canvas: CanvasElement, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Dibuja detalles iconográficos (flecha y omega) sobre el escudo del Canvas."""
    try:
        canvas.create_line(canvas_x + 41 * scale, canvas_y + 75 * scale, canvas_x + 75 * scale, canvas_y + 41 * scale, fill=C_BACKGROUND, width=max(2, int(8 * scale)), capstyle="round")
        canvas.create_polygon(canvas_x + 75 * scale, canvas_y + 41 * scale, canvas_x + 89 * scale, canvas_y + 38 * scale, canvas_x + 92 * scale, canvas_y + 52 * scale, fill=C_BACKGROUND, outline="")
        canvas.create_text(canvas_x + 64 * scale, canvas_y + 96 * scale, text="\u03a9", fill=C_BACKGROUND, font=(UI_FONT_FAMILY, max(8, int(UI_FONT_HEADER_SIZE * scale)), UI_FONT_BOLD))
    except Exception: pass

def draw_logo(canvas: CanvasElement, size: float = 56.0, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    """
    Renderiza la representación vectorial del escudo corporativo en el canvas.
    Aplica una transformación de escala basada en un tamaño base de 128x128.
    """
    try:
        s = float(size)
        if s <= 0: return
        scale = max(0.1, min(10.0, s / 128.0))
        
        # Generar puntos del polígono desplazados al origen (canvas_x, canvas_y)
        coords = _get_shield_coords(scale)
        poly_points: List[float] = []
        for i in range(0, len(coords), 2):
            poly_points.append(canvas_x + coords[i])
            poly_points.append(canvas_y + coords[i+1])
            
        # Dibujar elementos base del logo
        canvas.create_oval(
            canvas_x + 64 * scale - 75 * scale, canvas_y + 58 * scale - 75 * scale, 
            canvas_x + 64 * scale + 75 * scale, canvas_y + 58 * scale + 75 * scale, 
            fill=blend(C_SURFACE, C_GLOW, 0.15), outline=""
        )
        canvas.create_polygon(poly_points, fill=GRADIENT_STOPS[1], outline="")
        
        _draw_shield_stripes(canvas, canvas_x, canvas_y, scale)
        _draw_shield_icon_decorations(canvas, canvas_x, canvas_y, scale)
    except Exception: pass

def draw_gradient_bar(canvas: CanvasElement, width: int, height: int = 3, canvas_x: float = 0.0, canvas_y: float = 0.0, stops: Tuple[HexColor, ...] = GRADIENT_STOPS) -> None:
    """Dibuja una barra decorativa con gradiente horizontal de color."""
    try:
        w_val = max(1, int(width))
        for seg in _get_grouped_segments(gradient_colors(w_val, stops)):
            canvas.create_line(canvas_x + seg.start_index, canvas_y, canvas_x + seg.end_index, canvas_y, fill=seg.hex_color, width=max(1, int(height)))
    except Exception: pass

def draw_ring(canvas: CanvasElement, percent: Union[float, int, None], size: int = 150, canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14, track: Optional[HexColor] = None, fill: Optional[HexColor] = None) -> None:
    """Renderiza un gráfico circular de progreso (anillo de salud) en el Canvas."""
    if percent is None: return
    try:
        val = max(0.0, min(100.0, float(percent)))
        diam = max(20, int(size))
        thick = max(2, min(int(thickness), (diam // 2) - 1))
        borde = thick / 2.0
        caja = (canvas_x + borde, canvas_y + borde, canvas_x + diam - borde, canvas_y + diam - borde)
        canvas.create_arc(*caja, start=0, extent=359.9, style="arc", outline=track or C_SURFACE_ALT, width=thick)
        if val > 0: canvas.create_arc(*caja, start=90, extent=-(val / 100 * 359.9), style="arc", outline=fill or score_color(val), width=thick)
    except (Exception, ValueError, TypeError): return
