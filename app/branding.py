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
from typing import Any, Final, TypeAlias, Literal, Mapping, Tuple, List, Optional, Union, TypedDict
from types import MappingProxyType
from functools import lru_cache
from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Type Aliases semánticos para el sistema de diseño
HexColor: TypeAlias = str  # Formato: "#RRGGBB"
SeverityLevel: TypeAlias = Literal["ok", "info", "warning", "danger"]
GradeKey: TypeAlias = Literal["A", "B", "C", "D", "F"]
SeverityStyle: TypeAlias = Tuple[HexColor, str]  # (Color, Etiqueta)
RGBTuple: TypeAlias = Tuple[int, int, int]  # Valores (R, G, B) de 0 a 255

class PaletteDict(TypedDict):
    """
    Definición de la paleta de colores del sistema.
    Cada clave representa un rol funcional, permitiendo consistencia visual 
    al referenciar colores por uso en lugar de por su valor hexadecimal.
    """
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
    """Escalafón de tamaños tipográficos usados en la jerarquía de UI."""
    display: int
    title: int
    subtitle: int
    heading: int
    body: int
    mono: int
    caption: int

# Metadatos del producto
APP_NAME: Final[str] = "Limpieza Total Omega"
APP_SHORT_NAME: Final[str] = "Omega"
APP_TAGLINE: Final[str] = "Limpieza y seguridad, en un solo lugar"
APP_VERSION: Final[str] = "2.1.0"

# Estilos de fuente base
UI_FONT_FAMILY: Final[str] = "Segoe UI"
UI_FONT_BOLD: Final[str] = "bold"

_PALETTE_RAW: Final[dict[str, HexColor]] = {
    "background": "#0a0e17",
    "surface": "#141b2d",
    "surface_alt": "#1e2740",
    "surface_hover": "#28324f",
    "card": "#182135",
    "accent": "#00f0c0",
    "accent_hover": "#00d0a4",
    "accent_dim": "#0a6b58",
    "accent2": "#7c5cff",
    "accent2_hover": "#6a48f0",
    "accent3": "#ff2d78",
    "success": "#22e39a",
    "info": "#38bdf8",
    "warning": "#ffb020",
    "danger": "#ff4757",
    "danger_hover": "#e02e3d",
    "text": "#f0f6fc",
    "text_muted": "#94a3b8",
    "text_dim": "#5c6b85",
    "border": "#2a3654",
    "glow": "#00f0c0",
}
PALETTE: Final[Mapping[str, HexColor]] = MappingProxyType(_PALETTE_RAW)

# Constantes pre-resueltas para optimización de renderizado (evita llamados a color())
C_SURFACE: Final[HexColor] = _PALETTE_RAW["surface"]
C_BACKGROUND: Final[HexColor] = _PALETTE_RAW["background"]
C_GLOW: Final[HexColor] = _PALETTE_RAW["glow"]
C_TEXT_MUTED: Final[HexColor] = _PALETTE_RAW["text_muted"]
C_SUCCESS: Final[HexColor] = _PALETTE_RAW["success"]
C_INFO: Final[HexColor] = _PALETTE_RAW["info"]
C_WARNING: Final[HexColor] = _PALETTE_RAW["warning"]
C_DANGER: Final[HexColor] = _PALETTE_RAW["danger"]
C_SURFACE_ALT: Final[HexColor] = _PALETTE_RAW["surface_alt"]

FONT_SIZES: FontSizesDict = {
    "display": 46,
    "title": 26,
    "subtitle": 13,
    "heading": 16,
    "body": 12,
    "mono": 11,
    "caption": 10,
}

SEVERITY_STYLES: Final[Mapping[SeverityLevel, SeverityStyle]] = MappingProxyType({
    "ok": ("#22e39a", "Correcto"),
    "info": ("#38bdf8", "Informativo"),
    "warning": ("#ffb020", "Advertencia"),
    "danger": ("#ff4757", "Peligro"),
})

GRADE_COLORS: Final[Mapping[str, HexColor]] = MappingProxyType({
    "A": "#22e39a",
    "B": "#38bdf8",
    "C": "#ffb020",
    "D": "#ff7b39",
    "F": "#ff4757",
})

ICONS: Final[Mapping[str, str]] = MappingProxyType({
    "Salud": "\u25c9",        
    "Limpieza": "\u2726",     
    "Seguridad": "\u26ca",    
    "Cuarentena": "\u2297",   
    "Memoria": "\u25a4",      
    "Disco": "\u25f4",        
    "Duplicados": "\u29c9",   
    "Navegadores": "\u25d0",  
    "Inicio": "\u23fb",       
    "Informe": "\u2263",      
    "Asistente": "\u273b",    
    "Ajustes": "\u2699",      
})

GRADIENT_STOPS: Final[Tuple[HexColor, ...]] = ("#00f0c0", "#7c5cff", "#ff2d78")

def app_title() -> str:
    """Retorna el nombre completo de la aplicación concatenado con la versión."""
    return f"{APP_NAME} v{APP_VERSION}"

@lru_cache(maxsize=32)
def color(name: str) -> HexColor:
    """Obtiene el color HEX desde la paleta global o un gris neutro de respaldo."""
    if not isinstance(name, str): return "#808080"
    return PALETTE.get(name, "#808080")

@lru_cache(maxsize=16)
def font_size(name: str) -> int:
    """Recupera el tamaño de fuente en píxeles según la clave jerárquica."""
    if not isinstance(name, str): return FONT_SIZES["body"]
    return FONT_SIZES.get(name, FONT_SIZES["body"])

@lru_cache(maxsize=32)
def icon(section: Optional[str]) -> str:
    """Mapea el nombre de una sección UI a su glifo Unicode."""
    if not isinstance(section, str):
        return "\u2022"
    return ICONS.get(section.strip(), "\u2022")

@lru_cache(maxsize=32)
def tab_label(section: str) -> str:
    """Combina el icono de sección con su nombre para etiquetas de pestañas."""
    return f"{icon(section)}  {section}"

@lru_cache(maxsize=16)
def severity_color(severity: Optional[str]) -> HexColor:
    """Resuelve el color hexadecimal según el nivel de severidad especificado."""
    if isinstance(severity, str) and (style := SEVERITY_STYLES.get(severity.lower())):
        return style[0]
    return C_TEXT_MUTED

@lru_cache(maxsize=16)
def severity_label(severity: Optional[str]) -> str:
    """Traduce el código de severidad a texto legible."""
    if isinstance(severity, str) and (style := SEVERITY_STYLES.get(severity.lower())):
        return style[1]
    return severity.upper() if (isinstance(severity, str) and severity.strip()) else "Desconocido"

def severity_icon(severity: Optional[str]) -> str:
    """Retorna el carácter Unicode representativo para estados de riesgo."""
    simbolos: dict[str, str] = {"ok": "\u2713", "info": "\u2139", "warning": "\u26a0", "danger": "\u2716"}
    if not isinstance(severity, str):
        return "\u2022"
    return simbolos.get(severity.lower(), "\u2022")

@lru_cache(maxsize=16)
def grade_color(grade: Optional[str]) -> HexColor:
    """Retorna el color asignado a una calificación (A-F)."""
    if isinstance(grade, str) and grade.strip():
        return GRADE_COLORS.get(grade.upper()[0], C_TEXT_MUTED)
    return C_TEXT_MUTED

@lru_cache(maxsize=128)
def score_color(score: Union[float, int, None]) -> HexColor:
    """Resuelve el color de un puntaje de salud (0-100) según rangos predefinidos."""
    if score is None:
        return C_TEXT_MUTED
    try:
        valor = float(score)
    except (TypeError, ValueError):
        return C_TEXT_MUTED
    
    if not (0.0 <= valor <= 100.0):
        return C_TEXT_MUTED

    thresholds: List[Tuple[float, HexColor]] = [
        (90.0, C_SUCCESS),
        (80.0, C_INFO),
        (65.0, C_WARNING),
        (50.0, "#ff7b39")
    ]
    
    for limit, color_val in thresholds:
        if valor >= limit:
            return color_val
            
    return C_DANGER

@lru_cache(maxsize=64)
def bar(percent: Union[float, int, None], width: int = 24,
        filled: str = "\u2588", empty: str = "\u2591") -> str:
    """Crea una representación visual tipo barra de texto para porcentajes."""
    try:
        valor: float = max(0.0, min(100.0, float(percent) if percent is not None else 0.0))
        ancho: int = max(1, int(width))
        llenos: int = int(round(valor / 100 * ancho))
        return filled * llenos + empty * (ancho - llenos)
    except (TypeError, ValueError):
        return empty * max(1, int(width))

@lru_cache(maxsize=128)
def _hex_to_rgb(value: HexColor) -> RGBTuple:
    """Transfomra color #RRGGBB a valores decimales (R, G, B)."""
    try:
        if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
            return (0, 0, 0)
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    except (ValueError, IndexError, AttributeError):
        return (0, 0, 0)

@lru_cache(maxsize=64)
def blend(start: HexColor, end: HexColor, ratio: float) -> HexColor:
    """Interpola linealmente colores (rango [0.0, 1.0])."""
    if start == end: return start
    try:
        ratio_clamped = max(0.0, min(1.0, float(ratio)))
        r1, g1, b1 = _hex_to_rgb(start)
        r2, g2, b2 = _hex_to_rgb(end)
        return "#{:02x}{:02x}{:02x}".format(
            int(r1 + (r2 - r1) * ratio_clamped),
            int(g1 + (g2 - g1) * ratio_clamped),
            int(b1 + (b2 - b1) * ratio_clamped),
        )
    except (ValueError, TypeError):
        return start

@lru_cache(maxsize=32)
def gradient_colors(steps: int, stops: Tuple[HexColor, ...] = GRADIENT_STOPS) -> Tuple[HexColor, ...]:
    """Genera secuencia de colores para degradados basados en puntos de control."""
    try:
        n = max(1, int(steps))
        if not stops: return (C_GLOW,) * n
        if len(stops) < 2: return (stops[0],) * n
        
        res = [stops[0]] * n
        tramos = len(stops) - 1
        for i in range(1, n):
            pos = (i * tramos) / (n - 1)
            idx = int(pos)
            res[i] = blend(stops[idx], stops[idx + 1], pos - idx) if idx < tramos else stops[-1]
        return tuple(res)
    except (ValueError, TypeError):
        return (C_GLOW,) * max(1, int(steps))

@lru_cache(maxsize=8)
def _get_grouped_segments(colors: Tuple[HexColor, ...]) -> Tuple[Tuple[HexColor, int, int], ...]:
    """Optimiza secuencias de colores agrupando segmentos idénticos adyacentes."""
    if not colors: return ()
    segments: List[Tuple[HexColor, int, int]] = []
    start = 0
    for i in range(1, len(colors)):
        if colors[i] != colors[start]:
            segments.append((colors[start], start, i))
            start = i
    segments.append((colors[start], start, len(colors)))
    return tuple(segments)

@lru_cache(maxsize=8)
def _get_shield_coords(s: float) -> List[float]:
    """Calcula vértices normalizados del logo para escalado vectorial."""
    base: List[float] = [64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31]
    return [v * float(s) for v in base]

@lru_cache(maxsize=4)
def logo_svg(size: int = 128) -> str:
    """Genera una representación SVG del logotipo corporativo."""
    try:
        s: int = max(1, min(4096, int(size)))
    except (ValueError, TypeError):
        s = 128
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
  <path d="M64 18 L100 31 V67 C100 90 83 104 64 110 C45 104 28 90 28 67 V31 Z"
        fill="url(#omegaShield)"/>
  <path d="M41 75 L75 41" stroke="{C_BACKGROUND}" stroke-width="8" stroke-linecap="round"/>
  <path d="M75 41 L89 38 L92 52 Z" fill="{C_BACKGROUND}"/>
  <text x="64" y="98" font-family="{UI_FONT_FAMILY}" font-size="26"
        font-weight="{UI_FONT_BOLD}" fill="{C_BACKGROUND}" text-anchor="middle">&#937;</text>
</svg>
"""

def save_logo_svg(destination: Union[str, Path, None]) -> Optional[Path]:
    """Guarda el archivo SVG tras validar que la ruta destino sea segura."""
    if destination is None:
        return None
    try:
        path_obj = Path(destination).resolve()
        if is_protected_path(path_obj) or not is_safe_to_modify(path_obj) or path_obj.is_dir():
            return None
        if path_obj.parent and not path_obj.parent.exists():
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            
        ensure_safe_to_modify(path_obj)
        path_obj.write_text(logo_svg(), encoding="utf-8")
        return path_obj
    except (OSError, PermissionError, RuntimeError, ValueError, AttributeError):
        return None

def logo_ascii() -> str:
    """Retorna la representación artística en texto del logo."""
    return r"""
   ___  __  __ ___ ___   _
  / _ \|  \/  | __/ __| /_\
 | (_) | |\/| | _|| (_ // _ \
  \___/|_|  |_|___\___/_/ \_\
      Limpieza Total Omega
"""

def _draw_shield_stripes(canvas: Any, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Renderiza las franjas degradadas internas del escudo en el canvas proporcionado."""
    try:
        if scale <= 0 or not hasattr(canvas, "create_rectangle"): return
        franjas_count: int = max(6, int(28 * scale))
        colores: Tuple[HexColor, ...] = gradient_colors(franjas_count)
        for color_hex, start, end in _get_grouped_segments(colores):
            mid: float = (start + end) / 2
            progreso: float = mid / (franjas_count - 1)
            w: float = 36 * scale * (1.0 if progreso < 0.55 else 1.0 - (progreso - 0.55) * 1.9)
            y_ini: float = canvas_y + 18 * scale + start * (92 * scale / franjas_count)
            y_fin: float = canvas_y + 18 * scale + end * (92 * scale / franjas_count)
            canvas.create_rectangle(
                canvas_x + 64 * scale - w, y_ini, 
                canvas_x + 64 * scale + w, y_fin + 1, 
                fill=color_hex, outline=""
            )
    except Exception: pass

def draw_logo(canvas: Any, size: int = 56, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    """Dibuja el escudo corporativo aplicando transformación de escala en el canvas."""
    if canvas is None or not hasattr(canvas, "create_polygon"): return
    try:
        s_val = float(size) if isinstance(size, (int, float)) else 56.0
        scale: float = max(0.1, min(10.0, s_val / 128.0))
        base_coords: List[float] = _get_shield_coords(scale)
        contorno: List[float] = [canvas_x + base_coords[i] if i % 2 == 0 else canvas_y + base_coords[i] for i in range(len(base_coords))]
        
        for paso in range(4, 0, -1):
            r: float = 56 * scale * (0.6 + paso * 0.12)
            canvas.create_oval(canvas_x + 64 * scale - r, canvas_y + 58 * scale - r, 
                               canvas_x + 64 * scale + r, canvas_y + 58 * scale + r, 
                               fill=blend(C_SURFACE, C_GLOW, 0.04 * paso), outline="")
        
        canvas.create_polygon(contorno, fill=GRADIENT_STOPS[1], outline="")
        _draw_shield_stripes(canvas, canvas_x, canvas_y, scale)
        canvas.create_line(canvas_x + 41 * scale, canvas_y + 75 * scale, canvas_x + 75 * scale, canvas_y + 41 * scale, 
                           fill=C_BACKGROUND, width=max(2, int(8 * scale)), capstyle="round")
        canvas.create_polygon(canvas_x + 75 * scale, canvas_y + 41 * scale, canvas_x + 89 * scale, canvas_y + 38 * scale, 
                              canvas_x + 92 * scale, canvas_y + 52 * scale, fill=C_BACKGROUND, outline="")
        canvas.create_text(canvas_x + 64 * scale, canvas_y + 96 * scale, text="\u03a9", 
                           fill=C_BACKGROUND, font=(UI_FONT_FAMILY, max(8, int(23 * scale)), UI_FONT_BOLD))
    except (ValueError, TypeError, AttributeError, ZeroDivisionError, OverflowError):
        pass

def draw_gradient_bar(canvas: Any, width: int, height: int = 3,
                      canvas_x: float = 0.0, canvas_y: float = 0.0,
                      stops: Tuple[HexColor, ...] = GRADIENT_STOPS) -> None:
    """Renderiza una línea horizontal con degradado de color sobre el canvas."""
    if canvas is None or not hasattr(canvas, "create_line"): return
    try:
        ancho: int = max(1, int(width))
        colores: Tuple[HexColor, ...] = gradient_colors(ancho, stops)
        for color_hex, start, end in _get_grouped_segments(colores):
            canvas.create_line(canvas_x + start, canvas_y, canvas_x + end, canvas_y, fill=color_hex, width=max(1, int(height)))
    except (ValueError, TypeError, AttributeError): pass

def draw_ring(canvas: Any, percent: Union[float, int], size: int = 150,
              canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14,
              track: Optional[HexColor] = None,
              fill: Optional[HexColor] = None) -> None:
    """Dibuja un indicador circular (donut) de progreso en un área de dibujo."""
    if canvas is None or not hasattr(canvas, "create_arc"): return
    try:
        valor: float = max(0.0, min(100.0, float(percent) if percent is not None else 0.0))
        diametro: int = max(20, int(size))
        grosor: int = max(2, min(int(thickness), (diametro // 2) - 1))
        
        color_fondo = track if isinstance(track, str) else C_SURFACE_ALT
        color_avance = fill if isinstance(fill, str) else score_color(valor)
        borde: float = grosor / 2
        
        caja = (
            canvas_x + borde, canvas_y + borde, 
            canvas_x + diametro - borde, canvas_y + diametro - borde
        )
        canvas.create_arc(*caja, start=0, extent=359.9, style="arc", outline=color_fondo, width=grosor)
        if valor > 0:
            canvas.create_arc(*caja, start=90, extent=-(valor / 100 * 359.9),
                              style="arc", outline=color_avance, width=grosor)
    except (TypeError, ValueError, AttributeError): 
        return
