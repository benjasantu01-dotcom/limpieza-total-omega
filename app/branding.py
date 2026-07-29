"""
branding.py — identidad visual de Limpieza Total Omega.

Centraliza nombre, versión, paleta, tipografía, iconografía y logo en un solo
lugar, para que la interfaz sea consistente.

Referencia de funciones gráficas para el layout:
  - `draw_logo(canvas, ...)`: Renderiza el escudo/omega en widgets Tkinter.
  - `draw_ring(canvas, percent, ...)`: Medidor circular de estado (HealthScore).
  - `draw_gradient_bar(canvas, ...)`: Franja decorativa de alta fidelidad.
  - `bar(percent, ...)`: Generador de texto para paneles de consola/reporte.
  - `logo_svg(...)`: Serializador de identidad para exportación.

El motivo es un escudo (seguridad) cruzado por un trazo de limpieza.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Final, TypeAlias, Literal, Mapping, Tuple, List
from types import MappingProxyType
from functools import lru_cache
from safety import is_safe_to_modify, ensure_safe_to_modify

# Type Aliases para mejorar la legibilidad de la semántica de datos
HexColor: TypeAlias = str
SeverityKey: TypeAlias = Literal["ok", "info", "warning", "danger"]
GradeKey: TypeAlias = Literal["A", "B", "C", "D", "F"]
SeverityStyle: TypeAlias = Tuple[HexColor, str]

APP_NAME: Final = "Limpieza Total Omega"
APP_SHORT_NAME: Final = "Omega"
APP_TAGLINE: Final = "Limpieza y seguridad, en un solo lugar"
APP_VERSION: Final = "2.1.0"

# Paleta oscura de alto contraste con tres acentos vivos.
PALETTE: Final = MappingProxyType({
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

FONT_SIZES: Final = MappingProxyType({
    "display": 46,
    "title": 26,
    "subtitle": 13,
    "heading": 16,
    "body": 12,
    "mono": 11,
    "caption": 10,
})

SEVERITY_STYLES: Final[Mapping[str, SeverityStyle]] = MappingProxyType({
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

GRADIENT_STOPS: Final = ("#00f0c0", "#7c5cff", "#ff2d78")


def app_title() -> str:
    """Retorna el nombre completo de la aplicación y su versión actual."""
    return f"{APP_NAME} v{APP_VERSION}"


@lru_cache(maxsize=32)
def color(name: str) -> HexColor:
    """Obtiene un código hexadecimal de la paleta."""
    return PALETTE.get(name, "#808080")


@lru_cache(maxsize=16)
def font_size(name: str) -> int:
    """Obtiene el tamaño tipográfico por nombre."""
    return FONT_SIZES.get(name, FONT_SIZES["body"])


def icon(section: str | None) -> str:
    """Glifo de una sección, o una viñeta neutra si no tiene uno asignado."""
    if isinstance(section, str) and (glifo := ICONS.get(section.strip())):
        return glifo
    return "\u2022"


def tab_label(section: str) -> str:
    """Nombre de pestaña con su ícono adelante."""
    return f"{icon(section)}  {section}"


def severity_color(severity: str | None) -> HexColor:
    """Mapea un nivel de severidad al color hexadecimal correspondiente."""
    if isinstance(severity, str) and (style := SEVERITY_STYLES.get(severity.lower())):
        return style[0]
    return PALETTE["text_muted"]


def severity_label(severity: str | None) -> str:
    """Obtiene la etiqueta legible para un nivel de severidad determinado."""
    if isinstance(severity, str) and severity.strip():
        if style := SEVERITY_STYLES.get(severity.lower()):
            return style[1]
        return severity.upper()
    return "Desconocido"


def severity_icon(severity: str | None) -> str:
    """Símbolo para marcar una severidad dentro de un panel de texto."""
    simbolos = {"ok": "\u2713", "info": "\u2139", "warning": "\u26a0", "danger": "\u2716"}
    if isinstance(severity, str):
        return simbolos.get(severity.lower(), "\u2022")
    return "\u2022"


def grade_color(grade: str | None) -> HexColor:
    """Retorna el color asignado a una letra de calificación (A, B, C, D, F)."""
    if isinstance(grade, str) and grade.strip():
        return GRADE_COLORS.get(grade.upper()[0], PALETTE["text_muted"])
    return PALETTE["text_muted"]


def score_color(score: float | int | None) -> HexColor:
    """Color según un puntaje 0-100, para medidores y barras."""
    try:
        valor = float(score)
    except (TypeError, ValueError):
        return PALETTE["text_muted"]
    if valor >= 90: return PALETTE["success"]
    if valor >= 80: return PALETTE["info"]
    if valor >= 65: return PALETTE["warning"]
    if valor >= 50: return "#ff7b39"
    return PALETTE["danger"]


def bar(percent: float | int | None, width: int = 24,
        filled: str = "\u2588", empty: str = "\u2591") -> str:
    """Barra de progreso en texto, para paneles de resultados."""
    try:
        valor = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        valor = 0.0
    ancho = max(1, int(width))
    llenos = int(round(valor / 100 * ancho))
    return filled * llenos + empty * (ancho - llenos)


@lru_cache(maxsize=128)
def _hex_to_rgb(value: HexColor) -> tuple[int, int, int]:
    """Convierte '#rrggbb' a una tupla RGB. Devuelve negro si es inválido."""
    try:
        limpio = value.lstrip("#")
        if len(limpio) != 6: raise ValueError("Invalid length")
        return (int(limpio[0:2], 16), int(limpio[2:4], 16), int(limpio[4:6], 16))
    except (AttributeError, ValueError, IndexError, TypeError):
        return (0, 0, 0)


@lru_cache(maxsize=64)
def blend(start: HexColor, end: HexColor, ratio: float) -> HexColor:
    """Mezcla dos colores mediante interpolación lineal."""
    proporcion = max(0.0, min(1.0, float(ratio)))
    r1, g1, b1 = _hex_to_rgb(start)
    r2, g2, b2 = _hex_to_rgb(end)
    return "#{:02x}{:02x}{:02x}".format(
        int(r1 + (r2 - r1) * proporcion),
        int(g1 + (g2 - g1) * proporcion),
        int(b1 + (b2 - b1) * proporcion),
    )


@lru_cache(maxsize=16)
def gradient_colors(steps: int, stops: tuple[HexColor, ...] = GRADIENT_STOPS) -> List[HexColor]:
    """Genera una lista interpolada de colores recorriendo las paradas."""
    cantidad = max(1, int(steps))
    if not stops: return [PALETTE["accent"]] * cantidad
    if len(stops) < 2: return [stops[0]] * cantidad
    
    tramos = len(stops) - 1
    salida: List[HexColor] = []
    for i in range(cantidad):
        posicion = i / max(1, cantidad - 1) * tramos
        indice = min(tramos - 1, int(posicion))
        salida.append(blend(stops[indice], stops[indice + 1], posicion - indice))
    return salida


@lru_cache(maxsize=4)
def logo_svg(size: int = 128) -> str:
    """Genera el logo de la aplicación en formato SVG."""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 128 128">
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


def save_logo_svg(destination: str | Path | None) -> Path | None:
    """Persiste el logo en disco tras validar seguridad."""
    if not destination: return None
    try:
        path = Path(destination).expanduser().resolve()
        
        # Validar directorio padre
        parent = path.parent
        if not parent.exists():
            if not is_safe_to_modify(parent):
                return None
            parent.mkdir(parents=True, exist_ok=True)
            
        # Validación final de seguridad antes de escritura
        if not is_safe_to_modify(path):
            return None
            
        ensure_safe_to_modify(path)
        path.write_text(logo_svg(), encoding="utf-8")
        return path
    except (OSError, PermissionError, RuntimeError, ValueError):
        return None


@lru_cache(maxsize=1)
def logo_ascii() -> str:
    """Retorna una representación en arte ASCII para logs."""
    return r"""
   ___  __  __ ___ ___   _
  / _ \|  \/  | __/ __| /_\
 | (_) | |\/| | _|| (_ // _ \
  \___/|_|  |_|___\___/_/ \_\
      Limpieza Total Omega
"""


def draw_logo(canvas: Any, size: int = 56, x: int = 0, y: int = 0) -> None:
    """Renderiza el logo (escudo Omega) en un widget canvas de Tkinter."""
    if canvas is None or not hasattr(canvas, "create_polygon"): return
    try:
        s = max(0.1, float(size) / 128)
        x_val, y_val = float(x), float(y)
    except (TypeError, ValueError): return

    # Resplandor radial pre-calculado
    for paso in range(4, 0, -1):
        radio = 56 * s * (0.6 + paso * 0.12)
        canvas.create_oval(
            x_val + 64 * s - radio, y_val + 58 * s - radio,
            x_val + 64 * s + radio, y_val + 58 * s + radio,
            fill=blend(PALETTE["surface"], PALETTE["glow"], 0.04 * paso),
            outline="",
        )

    # Polígono base
    c = [64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31]
    contorno = [x_val + v * s if i % 2 == 0 else y_val + v * s for i, v in enumerate(c)]
    canvas.create_polygon(contorno, fill=GRADIENT_STOPS[1], outline="")
    
    # Detalle interno
    franjas = max(6, int(28 * s))
    alto = 92 * s / franjas
    colores_grad = gradient_colors(franjas)
    for i, tono in enumerate(colores_grad):
        arriba = y_val + 18 * s + i * alto
        avance = i / max(1, franjas - 1)
        w = 36 * s * (1.0 if avance < 0.55 else 1.0 - (avance - 0.55) * 1.9)
        canvas.create_rectangle(x_val + 64 * s - w, arriba, x_val + 64 * s + w, arriba + alto + 1, fill=tono, outline="")

    # Trazos finales
    canvas.create_line(x_val + 41 * s, y_val + 75 * s, x_val + 75 * s, y_val + 41 * s, fill=PALETTE["background"], width=max(2, int(8 * s)), capstyle="round")
    canvas.create_polygon(x_val + 75 * s, y_val + 41 * s, x_val + 89 * s, y_val + 38 * s, x_val + 92 * s, y_val + 52 * s, fill=PALETTE["background"], outline="")
    canvas.create_text(x_val + 64 * s, y_val + 96 * s, text="\u03a9", fill=PALETTE["background"], font=("Segoe UI", max(8, int(23 * s)), "bold"))


def draw_gradient_bar(canvas: Any, width: int, height: int = 3,
                      x: int = 0, y: int = 0,
                      stops: tuple[HexColor, ...] = GRADIENT_STOPS) -> None:
    """Pinta una franja de degradado, línea por línea."""
    if canvas is None or not hasattr(canvas, "create_line"): return
    try:
        ancho, alto = max(1, int(width)), max(1, int(height))
        colores = gradient_colors(ancho, stops)
        for i, tono in enumerate(colores):
            canvas.create_line(x + i, y, x + i, y + alto, fill=tono)
    except (ValueError, TypeError, AttributeError): pass


def draw_ring(canvas: Any, percent: float | int, size: int = 150,
              x: int = 0, y: int = 0, thickness: int = 14,
              track: HexColor | None = None,
              fill: HexColor | None = None) -> None:
    """Dibuja un medidor circular (HealthScore) usando arcos de Tkinter."""
    if canvas is None or not hasattr(canvas, "create_arc"): return
    try:
        valor = max(0.0, min(100.0, float(percent)))
        diametro = max(20, int(size))
        grosor = max(2, min(int(thickness), diametro // 2 - 1))
    except (TypeError, ValueError): return
    
    color_fondo, color_avance = track or PALETTE["surface_alt"], fill or score_color(valor)
    borde = grosor / 2
    caja = (x + borde, y + borde, x + diametro - borde, y + diametro - borde)
    
    canvas.create_arc(*caja, start=0, extent=359.9, style="arc", outline=color_fondo, width=grosor)
    if valor > 0:
        canvas.create_arc(*caja, start=90, extent=-(valor / 100 * 359.9),
                          style="arc", outline=color_avance, width=grosor)
