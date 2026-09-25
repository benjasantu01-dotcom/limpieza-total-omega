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
import os
from pathlib import Path
from typing import Any, Final, TypeAlias, Literal, Mapping, Tuple, List, Optional, Union, TypedDict, Protocol, NamedTuple
from types import MappingProxyType
from functools import lru_cache
from safety import ensure_safe_to_modify, is_safe_to_modify, is_protected_path
import math

# Definición de tipos para mejorar la semántica del código
ColorHex: TypeAlias = str  
SeverityLevel: TypeAlias = Literal["ok", "info", "warning", "danger"]
GradeKey: TypeAlias = Literal["A", "B", "C", "D", "F"]
SeverityStyle: TypeAlias = Tuple[ColorHex, str]  
RGBTuple: TypeAlias = Tuple[int, int, int]  

# Caché local para evitar recálculo de gradientes en cada frame.
_GRADIENT_CACHE: dict[Tuple[int, Tuple[ColorHex, ...]], Tuple[ColorHex, ...]] = {}

# Pre-generación de fragmento SVG estático para mejorar performance
_SVG_GRADIENT_STOPS: Final[str] = "\n".join([f'      <stop offset="{o}" stop-color="{c}"/>' 
                       for o, c in zip(["0%", "55%", "100%"], ["#00f0c0", "#7c5cff", "#ff2d78"])])

class CanvasElement(Protocol):
    """Protocolo Duck-typing que define la interfaz de dibujo para CTkCanvas."""
    def create_rectangle(self, x0: float, y0: float, x1: float, y1: float, **kwargs: Any) -> int: ...
    def create_polygon(self, *args: float, **kwargs: Any) -> int: ...
    def create_oval(self, x0: float, y0: float, x1: float, y1: float, **kwargs: Any) -> int: ...
    def create_line(self, x0: float, y0: float, x1: float, y1: float, **kwargs: Any) -> int: ...
    def create_text(self, x: float, y: float, **kwargs: Any) -> int: ...
    def create_arc(self, x0: float, y0: float, x1: float, y1: float, **kwargs: Any) -> int: ...

class ColorSegment(NamedTuple):
    """Representa un rango de pixeles con un mismo color para optimizar el dibujo en canvas."""
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

# Estilos tipográficos base para componentes de UI
UI_FONT_FAMILY: Final[str] = "Segoe UI"
UI_FONT_BOLD: Final[str] = "bold"
UI_FONT_HEADER_SIZE: Final[int] = 23
UI_FONT_BODY_SIZE: Final[int] = 12

# Mapeo maestro de colores y constantes individuales para acceso O(1) sin hashing
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

# Escala tipográfica fija: define jerarquía visual para mantener consistencia en toda la app.
FONT_SIZES: Final[Mapping[str, int]] = MappingProxyType({
    "display": 46, "title": 26, "subtitle": 13, "heading": 16,
    "body": UI_FONT_BODY_SIZE, "mono": 11, "caption": 10,
})

# Mapeo de severidad: traduce niveles de riesgo a combinaciones de color y texto descriptivo.
SEVERITY_STYLES: Final[Mapping[SeverityLevel, SeverityStyle]] = MappingProxyType({
    "ok": (C_SUCCESS, "Correcto"),
    "info": (C_INFO, "Informativo"),
    "warning": (C_WARNING, "Advertencia"),
    "danger": (C_DANGER, "Peligro"),
})

# Mapeo de grados: colores específicos para calificaciones de salud (escala académica A-F).
GRADE_COLORS: Final[Mapping[str, ColorHex]] = MappingProxyType({
    "A": C_SUCCESS, "B": C_INFO, "C": C_WARNING, "D": "#ff7b39", "F": C_DANGER,
})

# Glifos Unicode: iconos vectoriales de texto usados en navegación y estados de sistema.
ICONS: Final[Mapping[str, str]] = MappingProxyType({
    "Salud": "\u25c9", "Limpieza": "\u2726", "Seguridad": "\u26ca",
    "Cuarentena": "\u2297", "Memoria": "\u25a4", "Disco": "\u25f4",
    "Duplicados": "\u29c9", "Navegadores": "\u25d0", "Inicio": "\u23fb",
    "Informe": "\u2263", "Asistente": "\u273b", "Ajustes": "\u2699",
})

# Gradiente de marca: Verde (#00f0c0) -> Violeta (#7c5cff) -> Rosa (#ff2d78).
GRADIENT_STOPS: Final[Tuple[ColorHex, ...]] = (C_ACCENT, C_ACCENT2, C_ACCENT3)

# Umbrales de salud: define qué color corresponde a cada rango porcentual del puntaje.
SCORE_THRESHOLDS: Final[Tuple[Tuple[float, ColorHex], ...]] = (
    (90.0, C_SUCCESS), (80.0, C_INFO), (65.0, C_WARNING), (50.0, "#ff7b39")
)

# Mapeo de iconos de severidad: glifos para representar estados en interfaces compactas.
SEVERITY_MAP: Final[Mapping[str, str]] = MappingProxyType({"ok": "\u2713", "info": "\u2139", "warning": "\u26a0", "danger": "\u2716"})

def app_title() -> str:
    """Retorna el título completo de la aplicación incluyendo la versión actual."""
    return f"{APP_NAME} v{APP_VERSION}"

def color(name: str) -> ColorHex:
    """Busca un color en la paleta global usando su clave identificadora."""
    return PALETTE.get(name, "#808080")

@lru_cache(maxsize=16)
def font_size(name: str) -> int:
    """Retorna el tamaño de fuente configurado para un identificador dado."""
    return FONT_SIZES.get(name, UI_FONT_BODY_SIZE)

@lru_cache(maxsize=32)
def icon(section: Optional[str]) -> str:
    """Retorna el glifo Unicode asociado a una sección de la interfaz."""
    return ICONS.get(section.strip(), "\u2022") if isinstance(section, str) else "\u2022"

@lru_cache(maxsize=32)
def tab_label(section: Optional[str]) -> str:
    """Genera una etiqueta para pestañas combinando icono y texto."""
    if not isinstance(section, str): return f"\u2022  Desconocido"
    return f"{icon(section)}  {section}"

@lru_cache(maxsize=16)
def _get_severity_style(severity: Optional[str]) -> Tuple[ColorHex, str]:
    """Helper interno para recuperar la tupla (color, label) basada en nivel de severidad."""
    if isinstance(severity, str) and (style := SEVERITY_STYLES.get(severity.lower())):
        return style
    return (C_TEXT_MUTED, "Desconocido")

def severity_color(severity: Optional[str]) -> ColorHex:
    """Retorna el código de color hexadecimal asociado a una severidad dada."""
    return _get_severity_style(severity)[0]

def severity_label(severity: Optional[str]) -> str:
    """Retorna la etiqueta descriptiva legible para una severidad dada."""
    if isinstance(severity, str):
        style = SEVERITY_STYLES.get(severity.lower())
        return style[1] if style else severity.capitalize()
    return "Desconocido"

def severity_icon(severity: Optional[str]) -> str:
    """Retorna el glifo unicode representativo para una severidad dada."""
    return SEVERITY_MAP.get(severity.lower(), "\u2022") if isinstance(severity, str) else "\u2022"

def grade_color(grade: Optional[str]) -> ColorHex:
    """Resuelve el color del grado de calificación de salud (A-F)."""
    if not isinstance(grade, str) or not grade.strip():
        return C_TEXT_MUTED
    return GRADE_COLORS.get(grade.strip().upper()[0], C_TEXT_MUTED)

@lru_cache(maxsize=128)
def score_color(score: Union[float, int, None]) -> ColorHex:
    """Calcula el color asociado a un puntaje de salud (0-100) basándose en umbrales."""
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
    """Genera una representación visual de texto de una barra de progreso."""
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
    """Convierte un string hex '#RRGGBB' a su tupla RGB (R, G, B) de 8 bits."""
    if isinstance(value, str) and len(value) == 7 and value[0] == '#':
        try:
            val = int(value[1:], 16)
            return ((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF)
        except ValueError:
            pass
    return (0, 0, 0)

@lru_cache(maxsize=256)
def _rgb_to_hex(rgb: RGBTuple) -> ColorHex:
    """Convierte una tupla (R, G, B) a su formato string hex '#RRGGBB'."""
    def _clamp(c: int) -> int: return max(0, min(255, c))
    return "#{:02x}{:02x}{:02x}".format(_clamp(rgb[0]), _clamp(rgb[1]), _clamp(rgb[2]))

@lru_cache(maxsize=128)
def blend(start: ColorHex, end: ColorHex, ratio: float) -> ColorHex:
    """Mezcla linealmente dos colores RGB según un ratio (0.0 a 1.0)."""
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

def _interpolate_rgb(s1: RGBTuple, s2: RGBTuple, delta: float) -> RGBTuple:
    """Calcula un punto de interpolación (delta) entre dos colores RGB base."""
    return (
        int(s1[0] + (s2[0] - s1[0]) * delta),
        int(s1[1] + (s2[1] - s1[1]) * delta),
        int(s1[2] + (s2[2] - s1[2]) * delta)
    )

@lru_cache(maxsize=32)
def gradient_colors(steps: int, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> Tuple[ColorHex, ...]:
    """Genera una secuencia de colores intermedios interpolando entre los puntos definidos en stops."""
    n = max(1, int(steps))
    if not stops or len(stops) < 2: 
        return (stops[0] if stops else C_TEXT_MUTED,) * n
    
    rgb_stops = [_hex_to_rgb(s) for s in stops]
    tramos = len(stops) - 1
    paso = float(n - 1) if n > 1 else 1.0
    
    res = [None] * n
    for i in range(n):
        pos = (i / paso) * tramos
        idx = min(int(pos), tramos - 1)
        res[i] = _rgb_to_hex(_interpolate_rgb(rgb_stops[idx], rgb_stops[idx + 1], pos - idx))
    return tuple(res) # type: ignore

@lru_cache(maxsize=64)
def _get_grouped_segments(colors: Tuple[ColorHex, ...]) -> Tuple[ColorSegment, ...]:
    """Agrupa colores consecutivos idénticos para reducir llamadas al motor de dibujo."""
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
SHIELD_BASE_COORDS: Final[Tuple[float, ...]] = (64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31)

@lru_cache(maxsize=128)
def _get_scaled_poly(scale: float, canvas_x: float, canvas_y: float) -> Tuple[float, ...]:
    """Escala las coordenadas base del escudo aplicando un factor y un offset."""
    return tuple(canvas_x + (c * scale) if i % 2 == 0 else canvas_y + (c * scale) 
                 for i, c in enumerate(SHIELD_BASE_COORDS))

@lru_cache(maxsize=8)
def logo_svg(size: int = 128) -> str:
    """Genera el código fuente XML de un archivo SVG del logo principal para exportación."""
    s = max(1, min(4096, int(size)))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="omegaShield" x1="0" y1="0" x2="1" y2="1">{_SVG_GRADIENT_STOPS}    </linearGradient>
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

def save_logo_svg(destination: Union[str, Path, None], size: int = 128) -> Optional[Path]:
    """Guarda el logo SVG tras validar la seguridad de la ruta destino y los límites de tamaño."""
    if not isinstance(destination, (str, Path)):
        return None
    try:
        path = Path(destination).resolve()
        # Defensa: validación jerárquica, asegurar directorio y evitar rutas protegidas
        if is_protected_path(path) or not is_safe_to_modify(path):
            return None
            
        safe_size = max(16, min(1024, int(size)))
        ensure_safe_to_modify(path)
        
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(logo_svg(safe_size), encoding="utf-8")
        return path if path.is_file() else None
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError, AttributeError):
        return None

def logo_ascii() -> str:
    """Retorna una representación ASCII del logo para logs o consola."""
    return "\n   ___  __  __ ___ ___   _\n  / _ \\|  \\/  | __/ __| /_\\\n | (_) | |\\/| | _|| (_ // _ \\\n  \\___/|_|  |_|___\\___/_/ \\_\\\n      Limpieza Total Omega\n"

@lru_cache(maxsize=16)
def _get_stripe_params(scale: float, franjas_count: int) -> Tuple[Tuple[float, float, float], ...]:
    """Pre-calcula parámetros de franjas para evitar cálculos repetitivos en el loop de dibujo."""
    return tuple((36.0 * scale * (1.0 if (i / (franjas_count - 1)) < 0.55 else 1.0 - (((i / (franjas_count - 1)) - 0.55) * 1.9)),
                  i * (92.0 * scale / franjas_count),
                  (i + 1) * (92.0 * scale / franjas_count)) for i in range(franjas_count))

def _draw_shield_stripes(canvas: CanvasElement, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Renderiza franjas decorativas graduadas en el interior del escudo."""
    try:
        if not math.isfinite(scale) or scale <= 0: return
        franjas_count = max(6, int(28 * scale))
        base_y = canvas_y + 18 * scale
        center_x = canvas_x + 64 * scale
        params = _get_stripe_params(scale, franjas_count)
        
        for seg in _get_grouped_segments(gradient_colors(franjas_count)):
            w, y_start, y_end = params[seg.start_index]
            canvas.create_rectangle(center_x - w, base_y + y_start, 
                                    center_x + w, base_y + y_end, 
                                    fill=seg.hex_color, outline="")
    except (TypeError, ValueError, ZeroDivisionError, IndexError): pass

def _draw_shield_icon_decorations(canvas: CanvasElement, canvas_x: float, canvas_y: float, scale: float) -> None:
    """Renderiza los símbolos iconográficos sobre el escudo (corte y letra Omega)."""
    try:
        if not math.isfinite(scale) or scale <= 0: return
        canvas.create_line(canvas_x + 41 * scale, canvas_y + 75 * scale, 
                           canvas_x + 75 * scale, canvas_y + 41 * scale, 
                           fill=C_BACKGROUND, width=max(2, int(8 * scale)), capstyle="round")
        canvas.create_polygon(canvas_x + 75 * scale, canvas_y + 41 * scale, 
                              canvas_x + 89 * scale, canvas_y + 38 * scale, 
                              canvas_x + 92 * scale, canvas_y + 52 * scale, 
                              fill=C_BACKGROUND, outline="")
        canvas.create_text(canvas_x + 64 * scale, canvas_y + 96 * scale, text="\u03a9", 
                           fill=C_BACKGROUND, font=(UI_FONT_FAMILY, max(8, int(UI_FONT_HEADER_SIZE * scale)), UI_FONT_BOLD))
    except (TypeError, ValueError, AttributeError): pass

def draw_logo(canvas: CanvasElement, size: float = 56.0, canvas_x: float = 0.0, canvas_y: float = 0.0) -> None:
    """Renderiza el escudo corporativo escalado a partir de la coordenada (canvas_x, canvas_y)."""
    try:
        s = float(size)
        if not math.isfinite(s) or s <= 0: return
        scale = max(0.1, min(10.0, s / 128.0))
        
        canvas.create_oval(
            canvas_x + (64 * scale) - 75 * scale, canvas_y + (58 * scale) - 75 * scale, 
            canvas_x + (64 * scale) + 75 * scale, canvas_y + (58 * scale) + 75 * scale, 
            fill=blend(C_SURFACE, C_GLOW, 0.15), outline=""
        )
        canvas.create_polygon(*_get_scaled_poly(scale, canvas_x, canvas_y), fill=GRADIENT_STOPS[1], outline="")
        _draw_shield_stripes(canvas, canvas_x, canvas_y, scale)
        _draw_shield_icon_decorations(canvas, canvas_x, canvas_y, scale)
    except (TypeError, ValueError, AttributeError): pass

def draw_gradient_bar(canvas: CanvasElement, width: int, height: int = 3, canvas_x: float = 0.0, canvas_y: float = 0.0, stops: Tuple[ColorHex, ...] = GRADIENT_STOPS) -> None:
    """Dibuja una barra horizontal decorativa con gradiente lineal en las coordenadas especificadas."""
    try:
        w_val = max(1, int(width))
        h_val = max(1, int(height))
        for seg in _get_grouped_segments(gradient_colors(w_val, stops)):
            canvas.create_line(canvas_x + seg.start_index, canvas_y, canvas_x + seg.end_index, canvas_y, fill=seg.hex_color, width=h_val)
    except (TypeError, ValueError, AttributeError): pass

def draw_ring(canvas: CanvasElement, percent: Union[float, int, None], size: int = 150, 
              canvas_x: float = 0.0, canvas_y: float = 0.0, thickness: int = 14, 
              track: Optional[ColorHex] = None, fill: Optional[ColorHex] = None) -> None:
    """Renderiza un gráfico circular de progreso (anillo) en las coordenadas (canvas_x, canvas_y)."""
    if percent is None or not isinstance(percent, (int, float)): return
    try:
        val = float(percent)
        if not math.isfinite(val): val = 0.0
        val = max(0.0, min(100.0, val))
        diam = max(20, int(size))
        thick = max(2, min(int(thickness), (diam // 2) - 1))
        borde: float = float(thick) / 2.0
        caja = (canvas_x + borde, canvas_y + borde, canvas_x + diam - borde, canvas_y + diam - borde)
        
        canvas.create_arc(*caja, start=0, extent=359.9, style="arc", outline=track or C_SURFACE_ALT, width=thick)
        if val > 0: 
            fill_color = fill or score_color(val)
            canvas.create_arc(*caja, start=90, extent=-(val / 100 * 359.9), style="arc", outline=fill_color, width=thick)
    except (ValueError, TypeError, AttributeError): return
