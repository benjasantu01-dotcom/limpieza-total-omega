"""
branding.py — identidad visual de Limpieza Total Omega.

Centraliza nombre, versión, paleta, tipografía y logo en un solo lugar,
para que la interfaz sea consistente y para que el bucle autónomo pueda
mejorar el diseño sin tocar la lógica de la app.

El logo se genera por código, sin archivos de imagen ni dependencias:
  - `logo_svg()` devuelve un SVG (texto plano) para el README o un ícono.
  - `draw_logo()` lo dibuja sobre un canvas de Tkinter para la ventana.

El motivo es un escudo (seguridad) cruzado por un trazo de limpieza, con
la letra omega abajo: las dos mitades del producto en una sola marca.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Final, TypeAlias, Literal, Mapping
from types import MappingProxyType
from functools import lru_cache
from safety import is_safe_to_modify

# Type Aliases para mejorar la legibilidad de la semántica de datos
HexColor: TypeAlias = str
SeverityKey: TypeAlias = Literal["ok", "info", "warning", "danger"]
GradeKey: TypeAlias = Literal["A", "B", "C", "D", "F"]

APP_NAME: Final = "Limpieza Total Omega"
APP_SHORT_NAME: Final = "Omega"
APP_TAGLINE: Final = "Limpieza y seguridad, en un solo lugar"
APP_VERSION: Final = "2.0.0"

# Paleta oscura con acento cian.
PALETTE: Final = MappingProxyType({
    "background": "#0f1419",
    "surface": "#1a2028",
    "surface_alt": "#232b35",
    "accent": "#00d4aa",
    "accent_hover": "#00b092",
    "danger": "#e5484d",
    "danger_hover": "#c13438",
    "warning": "#f5a623",
    "text": "#e6edf3",
    "text_muted": "#8b949e",
    "border": "#30363d",
})

FONT_SIZES: Final = MappingProxyType({
    "title": 24,
    "subtitle": 13,
    "heading": 15,
    "body": 12,
    "mono": 11,
    "caption": 10,
})

# Diccionarios de mapeo directo para evitar lógica repetitiva y búsquedas extra
SEVERITY_STYLES: Final[Mapping[str, tuple[HexColor, str]]] = MappingProxyType({
    "ok": ("#00d4aa", "Correcto"),
    "info": ("#58a6ff", "Informativo"),
    "warning": ("#f5a623", "Advertencia"),
    "danger": ("#e5484d", "Peligro"),
})

GRADE_COLORS: Final[Mapping[str, HexColor]] = MappingProxyType({
    "A": "#00d4aa",
    "B": "#58a6ff",
    "C": "#f5a623",
    "D": "#ff7b39",
    "F": "#e5484d",
})


def app_title() -> str:
    """Retorna el nombre completo de la aplicación y su versión actual."""
    return f"{APP_NAME} v{APP_VERSION}"


@lru_cache(maxsize=16)
def color(name: str) -> HexColor:
    """
    Obtiene un código hexadecimal de la paleta.
    
    Args:
        name: Clave del color en el diccionario PALETTE.
    Returns:
        Hexadecimal de color si existe, o un gris neutro de respaldo.
    """
    return PALETTE.get(name, "#808080")


@lru_cache(maxsize=16)
def font_size(name: str) -> int:
    """
    Obtiene el tamaño tipográfico por nombre.
    
    Args:
        name: Clave del tamaño en FONT_SIZES.
    Returns:
        Valor entero en puntos, o tamaño de 'body' si la clave no se encuentra.
    """
    return FONT_SIZES.get(name, FONT_SIZES["body"])


def severity_color(severity: str | None) -> HexColor:
    """
    Mapea un nivel de severidad al color hexadecimal correspondiente.
    
    Args:
        severity: String identificador (ej: 'ok', 'danger').
    Returns:
        Color hexadecimal definido en SEVERITY_STYLES o gris si es desconocido.
    """
    if isinstance(severity, str) and (style := SEVERITY_STYLES.get(severity.lower())):
        return style[0]
    return PALETTE["text_muted"]


def severity_label(severity: str | None) -> str:
    """
    Obtiene la etiqueta legible para un nivel de severidad determinado.
    
    Args:
        severity: String identificador. Si es inválido, retorna el input en mayúsculas.
    """
    if isinstance(severity, str) and severity.strip():
        if style := SEVERITY_STYLES.get(severity.lower()):
            return style[1]
        return severity.upper()
    return "Desconocido"


def grade_color(grade: str | None) -> HexColor:
    """
    Retorna el color asignado a una letra de calificación (A, B, C, D, F).
    """
    if isinstance(grade, str) and grade.strip():
        return GRADE_COLORS.get(grade.upper()[0], PALETTE["text_muted"])
    return PALETTE["text_muted"]


@lru_cache(maxsize=4)
def logo_svg(size: int = 128) -> str:
    """
    Genera el logo de la aplicación en formato SVG (XML plano).
    
    Args:
        size: Tamaño en píxeles del lado del contenedor cuadrado.
    """
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="omegaShield" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{PALETTE['accent']}"/>
      <stop offset="100%" stop-color="#0891b2"/>
    </linearGradient>
  </defs>
  <rect width="128" height="128" rx="28" fill="{PALETTE['surface']}"/>
  <path d="M64 20 L98 32 V66 C98 88 82 102 64 108 C46 102 30 88 30 66 V32 Z"
        fill="url(#omegaShield)"/>
  <path d="M42 74 L74 42" stroke="{PALETTE['text']}" stroke-width="7" stroke-linecap="round"/>
  <path d="M74 42 L87 39 L90 52 Z" fill="{PALETTE['text']}"/>
  <text x="64" y="96" font-family="Segoe UI, Arial, sans-serif" font-size="24"
        font-weight="bold" fill="{PALETTE['text']}" text-anchor="middle">&#937;</text>
</svg>
"""


def save_logo_svg(destination: str | Path) -> Path | None:
    """
    Persiste el archivo SVG del logo tras validar permisos y seguridad.
    
    Previene path traversal y valida que la extensión sea .svg.
    
    Returns:
        Path del archivo guardado, o None si la operación es inválida.
    """
    if not destination:
        return None
    try:
        path = Path(destination).expanduser().resolve()
        
        if path.is_symlink() or not path.name.lower().endswith(".svg"):
            return None
        # Variante booleana: acá queremos devolver None, no propagar una
        # excepción. Con `ensure_safe_to_modify` este `if` no filtraba nada,
        # porque la función devuelve un Path (siempre verdadero) o lanza.
        if not is_safe_to_modify(path) or not is_safe_to_modify(path.parent):
            return None
            
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(logo_svg(), encoding="utf-8")
        return path
    except (OSError, PermissionError, TypeError, RuntimeError):
        return None


@lru_cache(maxsize=1)
def logo_ascii() -> str:
    """Retorna una representación en arte ASCII para logs o consola."""
    return r"""
   ___  __  __ ___ ___   _
  / _ \|  \/  | __/ __| /_\
 | (_) | |\/| | _|| (_ // _ \
  \___/|_|  |_|___\___/_/ \_\
      Limpieza Total Omega
"""


def draw_logo(canvas: Any, size: int = 56, x: int = 0, y: int = 0) -> None:
    """
    Renderiza el logo en un widget canvas de Tkinter.
    
    Args:
        canvas: Objeto con método `create_polygon`.
        size: Tamaño base del logo.
        x: Offset X en el canvas.
        y: Offset Y en el canvas.
    """
    if canvas is None or not hasattr(canvas, "create_polygon"):
        return

    # Normalización de tamaño para evitar desbordamiento gráfico
    if not isinstance(size, (int, float)) or size <= 0:
        size = 56
        
    s = size / 128
    def pts(*coords: float) -> list[float]:
        return [x + c * s for c in coords]

    try:
        canvas.create_polygon(
            pts(64, 20, 98, 32, 98, 66, 88, 88, 64, 108, 40, 88, 30, 66, 30, 32),
            fill=PALETTE["accent"], outline="",
        )
        canvas.create_line(
            *pts(42, 74, 74, 42), fill=PALETTE["text"],
            width=max(2, int(7 * s)), capstyle="round",
        )
        canvas.create_polygon(pts(74, 42, 87, 39, 90, 52), fill=PALETTE["text"], outline="")
        canvas.create_text(
            *pts(64, 94), text="\u03a9", fill=PALETTE["text"],
            font=("Segoe UI", max(8, int(21 * s)), "bold"),
        )
    except (ValueError, TypeError, AttributeError):
        pass
