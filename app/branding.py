"""
branding.py — identidad visual de Limpieza Total Omega.

Centraliza nombre, versión, paleta, tipografía, iconografía y logo.

GLOSARIO VISUAL:
  - Surface: Fondos de contenedores y áreas de trabajo.
  - Accent: Colores de marca para llamados a la acción o elementos destacados.
  - Glow: Efectos de iluminación sutil para resaltar estados de salud.
  - Severity: Código cromático para niveles de riesgo (OK, Info, Warning, Danger).

Referencia de funciones gráficas para el layout:
  - `draw_logo(...)`: Renderiza el escudo/omega en widgets Tkinter.
  - `draw_ring(...)`: Medidor circular de estado (HealthScore).
  - `draw_gradient_bar(...)`: Franja decorativa de alta fidelidad.
  - `bar(...)`: Generador de texto para paneles de consola/reporte.
  - `logo_svg(...)`: Serializador de identidad para exportación.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Final, TypeAlias, Literal, Mapping, Tuple, List, Optional, Union, TypedDict
from types import MappingProxyType
from functools import lru_cache
import os
from safety import is_safe_to_modify, ensure_safe_to_modify

# Type Aliases semánticos para el sistema de diseño
HexColor: TypeAlias = str  # Formato: "#RRGGBB"
SeverityLevel: TypeAlias = Literal["ok", "info", "warning", "danger"]
GradeKey: TypeAlias = Literal["A", "B", "C", "D", "F"]
SeverityStyle: TypeAlias = Tuple[HexColor, str]  # (Color, Etiqueta)
RGBTuple: TypeAlias = Tuple[int, int, int]  # Valores (R, G, B) de 0 a 255
PointCoords: TypeAlias = List[float]  # Lista plana [x1, y1, x2, y2, ...]

# Constantes de diseño base
UI_FONT_FAMILY: Final[str] = "Segoe UI"
UI_FONT_BOLD: Final[str] = "bold"

class PaletteDict(TypedDict):
    """Define el contrato de colores requerido para renderizar la UI consistentemente."""
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
    """Jerarquía tipográfica para mantener la legibilidad entre distintos componentes."""
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

# Mapa de colores centralizado usado por toda la UI
PALETTE: Final[Mapping[str, HexColor]] = MappingProxyType({
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
})

# Caché de conversión RGB para evitar re-cálculos costosos en ciclos de dibujo
PALETTE_RGB: Final[Mapping[str, RGBTuple]] = MappingProxyType({
    k: (int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16)) for k, v in PALETTE.items()
})

# Mapa inverso de Hex a Clave para lookup eficiente en _hex_to_rgb
HEX_TO_KEY: Final[Mapping[HexColor, str]] = MappingProxyType({v: k for k, v in PALETTE.items()})

# Escalas tipográficas basadas en em/px ajustadas para visibilidad óptima
FONT_SIZES: Final[FontSizesDict] = {
    "display": 46,
    "title": 26,
    "subtitle": 13,
    "heading": 16,
    "body": 12,
    "mono": 11,
    "caption": 10,
}

# Mapeo semántico para estados de salud de componentes
SEVERITY_STYLES: Final[Mapping[SeverityLevel, SeverityStyle]] = MappingProxyType({
    "ok": ("#22e39a", "Correcto"),
    "info": ("#38bdf8", "Informativo"),
    "warning": ("#ffb020", "Advertencia"),
    "danger": ("#ff4757", "Peligro"),
})

# Escala de calificación tipo auditoría (Academic Grading)
GRADE_COLORS: Final[Mapping[str, HexColor]] = MappingProxyType({
    "A": "#22e39a",
    "B": "#38bdf8",
    "C": "#ffb020",
    "D": "#ff7b39",
    "F": "#ff4757",
})

# Glifos Unicode para representar módulos y funciones
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

# Secuencia de colores para gradientes de alta fidelidad
GRADIENT_STOPS: Final[Tuple[HexColor, ...]] = ("#00f0c0", "#7c5cff", "#ff2d78")

def app_title() -> str:
    """Combina el nombre de marca y versión para encabezados de ventana."""
    return f"{APP_NAME} v{APP_VERSION}"


@lru_cache(maxsize=32)
def color(name: str) -> HexColor:
    """Recupera un color de la paleta definida; ante error devuelve gris neutro."""
    return PALETTE.get(name, "#808080")


@lru_cache(maxsize=16)
def font_size(name: str) -> int:
    """Accede a la configuración tipográfica; retorna tamaño base si la clave falta."""
    return FONT_SIZES.get(name, FONT_SIZES["body"])


@lru_cache(maxsize=32)
def icon(section: Optional[str]) -> str:
    """Asocia un glifo Unicode a una sección; usa '•' como fallback de seguridad."""
    if not isinstance(section, str):
        return "\u2022"
    return ICONS.get(section.strip(), "\u2022")


@lru_cache(maxsize=32)
def tab_label(section: str) -> str:
    """Formatea la etiqueta de una pestaña con su respectivo ícono y título."""
    return f"{icon(section)}  {section}"


@lru_cache(maxsize=16)
def severity_color(severity: Optional[str]) -> HexColor:
    """Deriva el color corporativo asociado a un nivel de severidad especificado."""
    if severity and (style := SEVERITY_STYLES.get(severity.lower())):
        return style[0]
    return PALETTE["text_muted"]


@lru_cache(maxsize=16)
def severity_label(severity: Optional[str]) -> str:
    """Traduce un código de severidad a texto legible para el usuario final."""
    if severity and (style := SEVERITY_STYLES.get(severity.lower())):
        return style[1]
    return severity.upper() if (severity and severity.strip()) else "Desconocido"


def severity_icon(severity: Optional[str]) -> str:
    """Selecciona el glifo Unicode estándar para representar un estado de riesgo."""
    simbolos: dict[str, str] = {"ok": "\u2713", "info": "\u2139", "warning": "\u26a0", "danger": "\u2716"}
    if not isinstance(severity, str):
        return "\u2022"
    return simbolos.get(severity.lower(), "\u2022")


@lru_cache(maxsize=16)
def grade_color(grade: Optional[str]) -> HexColor:
    """Mapea una calificación académica (A-F) al color de semáforo correspondiente."""
    if isinstance(grade, str) and grade.strip():
        return GRADE_COLORS.get(grade.upper()[0], PALETTE["text_muted"])
    return PALETTE["text_muted"]


@lru_cache(maxsize=128)
def score_color(score: Union[float, int, None]) -> HexColor:
    """Determina el color de salud basado en un puntaje numérico [0-100]."""
    if score is None:
        return PALETTE["text_muted"]
    try:
        valor: float = float(score)
        if not (0.0 <= valor <= 100.0):
            return PALETTE["text_muted"]
        
        if valor >= 90: return PALETTE["success"]
        if valor >= 80: return PALETTE["info"]
        if valor >= 65: return PALETTE["warning"]
        if valor >= 50: return "#ff7b39"
        return PALETTE["danger"]
    except (TypeError, ValueError):
        return PALETTE["text_muted"]


@lru_cache(maxsize=64)
def bar(percent: Union[float, int, None], width: int = 24,
        filled: str = "\u2588", empty: str = "\u2591") -> str:
    """
    Genera una representación visual de progreso en formato texto.
    
    Args:
        percent: Valor numérico de 0 a 100.
        width: Número total de caracteres de la barra.
        filled: Carácter para el segmento completado.
        empty: Carácter para el segmento vacío.
    """
    try:
        valor: float = max(0.0, min(100.0, float(percent) if percent is not None else 0.0))
        ancho: int = max(1, int(width))
        llenos: int = int(round(valor / 100 * ancho))
        return filled * llenos + empty * (ancho - llenos)
    except (TypeError, ValueError):
        return empty * max(1, int(width))


@lru_cache(maxsize=128)
def _hex_to_rgb(value: HexColor) -> RGBTuple:
    """Convierte un string HEX (#RRGGBB) a una tupla RGB (r, g, b) usando lookup directo."""
    if not isinstance(value, str) or not value.startswith("#") or len(value) != 7:
        return (0, 0, 0)
    
    key = HEX_TO_KEY.get(value)
    if key:
        return PALETTE_RGB[key]
        
    try:
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    except ValueError:
        return (0, 0, 0)


@lru_cache(maxsize=64)
def blend(start: HexColor, end: HexColor, ratio: float) -> HexColor:
    """Realiza una interpolación lineal (lerp) entre dos colores HEX."""
    ratio_clamped = max(0.0, min(1.0, float(ratio)))
    r1, g1, b1 = _hex_to_rgb(start)
    r2, g2, b2 = _hex_to_rgb(end)
    return "#{:02x}{:02x}{:02x}".format(
        int(r1 + (r2 - r1) * ratio_clamped),
        int(g1 + (g2 - g1) * ratio_clamped),
        int(b1 + (b2 - b1) * ratio_clamped),
    )


@lru_cache(maxsize=32)
def gradient_colors(steps: int, stops: Tuple[HexColor, ...] = GRADIENT_STOPS) -> Tuple[HexColor, ...]:
    """Calcula una secuencia de colores interpolados (n = steps) a lo largo de puntos de control."""
    n = max(1, int(steps))
    if not stops: return (PALETTE["accent"],) * n
    if len(stops) < 2: return (stops[0],) * n
    
    res = [stops[0]] * n
    tramos = len(stops) - 1
    for i in range(1, n):
        pos = (i * tramos) / (n - 1)
        idx = int(pos)
        res[i] = blend(stops[idx], stops[idx + 1], pos - idx) if idx < tramos else stops[-1]
    return tuple(res)


@lru_cache(maxsize=8)
def _get_grouped_segments(colors: Tuple[HexColor, ...]) -> Tuple[Tuple[HexColor, int, int], ...]:
    """Optimiza la serie de colores agrupando segmentos consecutivos idénticos."""
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
    """Calcula las coordenadas vectoriales (x, y) de la forma geométrica base del escudo."""
    base: List[float] = [64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31]
    return [v * float(s) for v in base]


@lru_cache(maxsize=4)
def logo_svg(size: int = 128) -> str:
    """Genera una representación vectorial (SVG) de la identidad de la marca."""
    s: int = max(1, min(4096, int(size)))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="omegaShield" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{GRADIENT_STOPS[0]}"/>
      <stop offset="55%" stop-color="{GRADIENT_STOPS[1]}"/>
      <stop offset="100%" stop-color="{GRADIENT_STOPS[2]}"/>
    </linearGradient>
    <radialGradient id="omegaGlow" cx="0.5" cy="0.4" r="0.6">
      <stop offset="0%" stop-color="{PALETTE['glow']}" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="{PALETTE['glow']}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="128" height="128" rx="30" fill="{PALETTE['surface']}"/>
  <circle cx="64" cy="56" r="52" fill="url(#omegaGlow)"/>
  <path d="M64 18 L100 31 V67 C100 90 83 104 64 110 C45 104 28 90 28 67 V31 Z"
        fill="url(#omegaShield)"/>
  <path d="M41 75 L75 41" stroke="{PALETTE['background']}" stroke-width="8" stroke-linecap="round"/>
  <path d="M75 41 L89 38 L92 52 Z" fill="{PALETTE['background']}"/>
  <text x="64" y="98" font-family="{UI_FONT_FAMILY}" font-size="26"
        font-weight="{UI_FONT_BOLD}" fill="{PALETTE['background']}" text-anchor="middle">&#937;</text>
</svg>
"""


def save_logo_svg(destination: Union[str, Path, None]) -> Optional[Path]:
    """Guarda el logo en disco previa validación de seguridad estricta."""
    if not destination:
        return None
    try:
        path_obj = Path(destination).expanduser().resolve()
        # Verificar seguridad antes de cualquier operación
        if not is_safe_to_modify(path_obj):
            return None
        target = ensure_safe_to_modify(path_obj)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(logo_svg(), encoding="utf-8")
        return target
    except (OSError, PermissionError, ValueError, TypeError, AttributeError):
        return None


def logo_ascii() -> str:
    """Devuelve la versión tipográfica ASCII del logo para logs o consola."""
    return r"""
   ___  __  __ ___ ___   _
  / _ \|  \/  | __/ __| /_\
 | (_) | |\/| | _|| (_ // _ \
  \___/|_|  |_|___\___/_/ \_\
      Limpieza Total Omega
"""


def _draw_shield_stripes(canvas: Any, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Renderiza los degradados internos de franjas decorativas del escudo."""
    if scale <= 0: return
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


def draw_logo(canvas: Any, size: int = 56, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    """
    Dibuja el logo de la marca en un canvas de Tkinter.

    Args:
        canvas: Widget canvas de Tkinter.
        size: Tamaño base en píxeles.
        canvas_x, canvas_y: Coordenadas del punto superior izquierdo del logo.
    """
    if not hasattr(canvas, "create_polygon"): return
    try:
        scale: float = max(0.1, min(10.0, float(size) / 128))
        base_coords: List[float] = _get_shield_coords(scale)
        contorno: List[float] = [canvas_x + base_coords[i] if i % 2 == 0 else canvas_y + base_coords[i] for i in range(len(base_coords))]
        for paso in range(4, 0, -1):
            r: float = 56 * scale * (0.6 + paso * 0.12)
            canvas.create_oval(canvas_x + 64 * scale - r, canvas_y + 58 * scale - r, 
                               canvas_x + 64 * scale + r, canvas_y + 58 * scale + r, 
                               fill=blend(PALETTE["surface"], PALETTE["glow"], 0.04 * paso), outline="")
        canvas.create_polygon(contorno, fill=GRADIENT_STOPS[1], outline="")
        _draw_shield_stripes(canvas, canvas_x, canvas_y, scale)
        canvas.create_line(canvas_x + 41 * scale, canvas_y + 75 * scale, canvas_x + 75 * scale, canvas_y + 41 * scale, 
                           fill=PALETTE["background"], width=max(2, int(8 * scale)), capstyle="round")
        canvas.create_polygon(canvas_x + 75 * scale, canvas_y + 41 * scale, canvas_x + 89 * scale, canvas_y + 38 * scale, 
                              canvas_x + 92 * scale, canvas_y + 52 * scale, fill=PALETTE["background"], outline="")
        canvas.create_text(canvas_x + 64 * scale, canvas_y + 96 * scale, text="\u03a9", 
                           fill=PALETTE["background"], font=(UI_FONT_FAMILY, max(8, int(23 * scale)), UI_FONT_BOLD))
    except (ValueError, TypeError, AttributeError, ZeroDivisionError, OverflowError):
        pass


def draw_gradient_bar(canvas: Any, width: int, height: int = 3,
                      canvas_x: float = 0.0, canvas_y: float = 0.0,
                      stops: Tuple[HexColor, ...] = GRADIENT_STOPS) -> None:
    """
    Renderiza una franja decorativa con gradiente horizontal en el canvas.

    Args:
        canvas: Widget canvas de Tkinter.
        width: Longitud total del gradiente.
        height: Grosor de la línea.
        canvas_x, canvas_y: Coordenadas de origen.
    """
    if not hasattr(canvas, "create_line"): return
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
    """
    Dibuja un medidor circular tipo anillo para representar porcentajes de salud.

    Args:
        canvas: Widget canvas de Tkinter.
        percent: Porcentaje actual (0-100).
        size: Diámetro del anillo en píxeles.
        thickness: Grosor del trazo.
        track: Color de fondo del anillo (track).
        fill: Color del segmento de progreso.
    """
    if not hasattr(canvas, "create_arc"): return
    try:
        valor: float = float(percent)
        diametro: int = max(20, int(size))
        grosor: int = max(2, min(int(thickness), diametro // 2 - 1))
        valor = max(0.0, min(100.0, valor))
        color_fondo: HexColor = track or PALETTE["surface_alt"]
        color_avance: HexColor = fill or score_color(valor)
        borde: float = grosor / 2
        caja: Tuple[float, float, float, float] = (
            canvas_x + borde, canvas_y + borde, 
            canvas_x + diametro - borde, canvas_y + diametro - borde
        )
        canvas.create_arc(*caja, start=0, extent=359.9, style="arc", outline=color_fondo, width=grosor)
        if valor > 0:
            canvas.create_arc(*caja, start=90, extent=-(valor / 100 * 359.9),
                              style="arc", outline=color_avance, width=grosor)
    except (TypeError, ValueError, ZeroDivisionError): 
        return
