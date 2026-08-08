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
from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Type Aliases para mejorar la legibilidad de la semántica de datos
HexColor: TypeAlias = str
SeverityLevel: TypeAlias = Literal["ok", "info", "warning", "danger"]
GradeKey: TypeAlias = Literal["A", "B", "C", "D", "F"]
# Estructura de estilo para severidad: (color_hex, etiqueta_legible)
SeverityStyle: TypeAlias = Tuple[HexColor, str]
RGBTuple: TypeAlias = Tuple[int, int, int]

class PaletteDict(TypedDict):
    """Mapeo estricto de las claves de color requeridas por la interfaz."""
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

APP_NAME: Final[str] = "Limpieza Total Omega"
APP_SHORT_NAME: Final[str] = "Omega"
APP_TAGLINE: Final[str] = "Limpieza y seguridad, en un solo lugar"
APP_VERSION: Final[str] = "2.1.0"

# Paleta centralizada; el uso de MappingProxyType asegura inmutabilidad en tiempo de ejecución.
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

FONT_SIZES: Final[Mapping[str, int]] = MappingProxyType({
    "display": 46,
    "title": 26,
    "subtitle": 13,
    "heading": 16,
    "body": 12,
    "mono": 11,
    "caption": 10,
})

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

# Mapeo de secciones funcionales a caracteres Unicode para visualización consistente.
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
    """Retorna el nombre completo de la aplicación y su versión actual."""
    return f"{APP_NAME} v{APP_VERSION}"


@lru_cache(maxsize=32)
def color(name: str) -> HexColor:
    """Obtiene el código hexadecimal de un color de la paleta. Retorna gris por defecto si no existe."""
    return PALETTE.get(name, "#808080") if isinstance(name, str) else "#808080"


@lru_cache(maxsize=16)
def font_size(name: str) -> int:
    """Obtiene el tamaño tipográfico numérico según clave; usa 'body' como fallback."""
    return FONT_SIZES.get(name, FONT_SIZES["body"]) if isinstance(name, str) else FONT_SIZES["body"]


def icon(section: Optional[str]) -> str:
    """Retorna el glifo unicode asociado a una sección; usa un punto central como fallback."""
    return ICONS.get(section.strip(), "\u2022") if isinstance(section, str) else "\u2022"


def tab_label(section: str) -> str:
    """Formatea el título de una pestaña, anteponiendo su ícono correspondiente."""
    return f"{icon(section)}  {section}"


def severity_color(severity: Optional[str]) -> HexColor:
    """Resuelve el color hex asociado a una severidad (ok|info|warning|danger)."""
    if isinstance(severity, str) and (style := SEVERITY_STYLES.get(severity.lower())):  # type: ignore
        return style[0]
    return PALETTE["text_muted"]


def severity_label(severity: Optional[str]) -> str:
    """Retorna la etiqueta legible de una severidad o el input en mayúsculas."""
    if isinstance(severity, str):
        if style := SEVERITY_STYLES.get(severity.lower()):  # type: ignore
            return style[1]
        if severity.strip():
            return severity.upper()
    return "Desconocido"


def severity_icon(severity: Optional[str]) -> str:
    """Retorna el glifo representativo para un nivel de severidad dado."""
    simbolos = {"ok": "\u2713", "info": "\u2139", "warning": "\u26a0", "danger": "\u2716"}
    return simbolos.get(severity.lower(), "\u2022") if isinstance(severity, str) else "\u2022"


def grade_color(grade: Optional[str]) -> HexColor:
    """Resuelve el color hex según la calificación (A, B, C, D, F)."""
    if isinstance(grade, str) and grade.strip():
        return GRADE_COLORS.get(grade.upper()[0], PALETTE["text_muted"])
    return PALETTE["text_muted"]


def score_color(score: Union[float, int, None]) -> HexColor:
    """Asigna un color semántico basado en el valor numérico del puntaje (0-100)."""
    try:
        valor = float(score)  # type: ignore
    except (TypeError, ValueError):
        return PALETTE["text_muted"]
    if valor >= 90: return PALETTE["success"]
    if valor >= 80: return PALETTE["info"]
    if valor >= 65: return PALETTE["warning"]
    if valor >= 50: return "#ff7b39"
    return PALETTE["danger"]


def bar(percent: Union[float, int, None], width: int = 24,
        filled: str = "\u2588", empty: str = "\u2591") -> str:
    """
    Crea una representación visual de progreso en texto.
    
    Args:
        percent: Valor numérico (0-100).
        width: Cantidad total de caracteres.
        filled: Carácter para el relleno.
        empty: Carácter para el vacío.
    """
    try:
        valor = max(0.0, min(100.0, float(percent))) # type: ignore
    except (TypeError, ValueError):
        valor = 0.0
    ancho = max(1, int(width))
    llenos = int(round(valor / 100 * ancho))
    return filled * llenos + empty * (ancho - llenos)


@lru_cache(maxsize=128)
def _hex_to_rgb(value: HexColor) -> RGBTuple:
    """Convierte un color hex (#RRGGBB) a una tupla de componentes (R, G, B)."""
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return (0, 0, 0)
    try:
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    except ValueError:
        return (0, 0, 0)


@lru_cache(maxsize=64)
def blend(start: HexColor, end: HexColor, ratio: float) -> HexColor:
    """Interpola linealmente entre dos colores hex basado en una proporción (0.0 a 1.0)."""
    ratio = max(0.0, min(1.0, float(ratio)))
    r1, g1, b1 = _hex_to_rgb(start)
    r2, g2, b2 = _hex_to_rgb(end)
    return "#{:02x}{:02x}{:02x}".format(
        int(r1 + (r2 - r1) * ratio),
        int(g1 + (g2 - g1) * ratio),
        int(b1 + (b2 - b1) * ratio),
    )


@lru_cache(maxsize=32)
def gradient_colors(steps: int, stops: Tuple[HexColor, ...] = GRADIENT_STOPS) -> List[HexColor]:
    """Genera una secuencia de colores interpolados entre puntos de parada definidos."""
    cantidad = max(1, int(steps))
    if not stops: return [PALETTE["accent"]] * cantidad
    if len(stops) < 2: return [stops[0]] * cantidad
    
    tramos = len(stops) - 1
    res: List[HexColor] = []
    for i in range(cantidad):
        posicion = i / max(1, cantidad - 1) * tramos
        idx = min(tramos - 1, int(posicion))
        res.append(blend(stops[idx], stops[idx + 1], posicion - idx))
    return res


def _get_shield_coords(sx: float, sy: float, s: float) -> List[float]:
    """Calcula vértices normalizados para la geometría vectorial del escudo."""
    base = [64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31]
    return [sx + v * s if i % 2 == 0 else sy + v * s for i, v in enumerate(base)]


@lru_cache(maxsize=4)
def logo_svg(size: int = 128) -> str:
    """Serializa la identidad visual de la marca en formato SVG."""
    s = max(1, int(size))
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
  <text x="64" y="98" font-family="Segoe UI, Arial, sans-serif" font-size="26"
        font-weight="bold" fill="{PALETTE['background']}" text-anchor="middle">&#937;</text>
</svg>
"""


def save_logo_svg(destination: Union[str, Path, None]) -> Optional[Path]:
    """Persiste el logo SVG en disco tras validar la seguridad de la ruta destino."""
    if not destination: return None
    try:
        target = Path(destination).resolve()
        parent = target.parent
        
        # Validación: chequeo booleano preventivo para seguridad
        if not is_safe_to_modify(parent) or not is_safe_to_modify(target):
            return None
            
        # Creación de directorio si no existe, asegurando permisos antes
        if not parent.exists():
            ensure_safe_to_modify(parent)
            parent.mkdir(parents=True, exist_ok=True)
        
        # Operación final tras validación estricta
        ensure_safe_to_modify(target)
        target.write_text(logo_svg(), encoding="utf-8")
        return target
    except (OSError, PermissionError, TypeError, ValueError, RuntimeError, IOError, AttributeError):
        return None


def logo_ascii() -> str:
    """Retorna arte ASCII para la representación en registros de consola."""
    return r"""
   ___  __  __ ___ ___   _
  / _ \|  \/  | __/ __| /_\
 | (_) | |\/| | _|| (_ // _ \
  \___/|_|  |_|___\___/_/ \_\
      Limpieza Total Omega
"""


def draw_logo(canvas: Any, size: int = 56, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    """
    Renderiza el logo vectorial en un canvas de Tkinter.
    """
    if not hasattr(canvas, "create_polygon"): return
    try:
        s = max(0.1, float(size) / 128)
        x, y = float(canvas_x), float(canvas_y)
        contorno = _get_shield_coords(x, y, s)
        
        for paso in range(4, 0, -1):
            r = 56 * s * (0.6 + paso * 0.12)
            canvas.create_oval(x + 64*s - r, y + 58*s - r, x + 64*s + r, y + 58*s + r, 
                               fill=blend(PALETTE["surface"], PALETTE["glow"], 0.04 * paso), outline="")

        canvas.create_polygon(contorno, fill=GRADIENT_STOPS[1], outline="")
        franjas = max(6, int(28 * s))
        alto = max(0.1, 92 * s / franjas)
        colores = gradient_colors(franjas)
        
        i = 0
        while i < franjas:
            start_i, color_actual = i, colores[i]
            while i < franjas and colores[i] == color_actual: i += 1
            w = 36 * s * (1.0 if (start_i + i)/2 / (franjas - 1) < 0.55 else 1.0 - ((start_i + i)/2 / (franjas - 1) - 0.55) * 1.9)
            canvas.create_rectangle(x + 64*s - w, y + 18*s + start_i*alto, x + 64*s + w, y + 18*s + i*alto + 1, fill=color_actual, outline="")

        canvas.create_line(x + 41*s, y + 75*s, x + 75*s, y + 41*s, fill=PALETTE["background"], width=max(2, int(8*s)), capstyle="round")
        canvas.create_polygon(x + 75*s, y + 41*s, x + 89*s, y + 38*s, x + 92*s, y + 52*s, fill=PALETTE["background"], outline="")
        canvas.create_text(x + 64*s, y + 96*s, text="\u03a9", fill=PALETTE["background"], font=("Segoe UI", max(8, int(23*s)), "bold"))
    except (ValueError, TypeError, AttributeError, ZeroDivisionError, OverflowError):
        pass


def draw_gradient_bar(canvas: Any, width: int, height: int = 3,
                      canvas_x: float = 0.0, canvas_y: float = 0.0,
                      stops: Tuple[HexColor, ...] = GRADIENT_STOPS) -> None:
    """
    Dibuja una franja horizontal con degradado optimizado mediante bloques verticales.
    """
    if not hasattr(canvas, "create_line"): return
    try:
        ancho = max(1, int(width))
        colores = gradient_colors(ancho, stops)
        i = 0
        while i < ancho:
            start, color_actual = i, colores[i]
            while i < ancho and colores[i] == color_actual: i += 1
            canvas.create_line(canvas_x + start, canvas_y, canvas_x + i, canvas_y, fill=color_actual, width=max(1, int(height)))
    except (ValueError, TypeError, AttributeError): pass


def draw_ring(canvas: Any, percent: Union[float, int], size: int = 150,
              canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14,
              track: Optional[HexColor] = None,
              fill: Optional[HexColor] = None) -> None:
    """
    Renderiza un anillo circular dinámico indicando progreso.
    """
    if not hasattr(canvas, "create_arc"): return
    try:
        val_f = float(percent)
        valor = max(0.0, min(100.0, val_f))
        diametro = max(20, int(size))
        grosor = max(2, min(int(thickness), diametro // 2 - 1))
    except (TypeError, ValueError): return
    
    color_fondo, color_avance = track or PALETTE["surface_alt"], fill or score_color(valor)
    borde = grosor / 2
    caja = (canvas_x + borde, canvas_y + borde, canvas_x + diametro - borde, canvas_y + diametro - borde)
    
    canvas.create_arc(*caja, start=0, extent=359.9, style="arc", outline=color_fondo, width=grosor)
    if valor > 0:
        canvas.create_arc(*caja, start=90, extent=-(valor / 100 * 359.9),
                          style="arc", outline=color_avance, width=grosor)
