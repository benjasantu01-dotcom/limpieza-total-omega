"""
branding.py — identidad visual de Limpieza Total Omega.

Centraliza nombre, versión, paleta, tipografía, iconografía y logo en un solo
lugar, para que la interfaz sea consistente y para que el bucle autónomo pueda
mejorar el diseño sin tocar la lógica de la app.

Todo lo visual se genera por código, sin archivos de imagen ni dependencias:
  - `logo_svg()` devuelve un SVG (texto plano) para el README o un ícono.
  - `draw_logo()` lo dibuja sobre un canvas de Tkinter para la ventana.
  - `draw_ring()` dibuja el medidor circular del puntaje de salud.
  - `draw_gradient_bar()` pinta la franja de degradado del encabezado.
  - `bar()` arma barras de progreso en texto, para los paneles de resultados.

El motivo es un escudo (seguridad) cruzado por un trazo de limpieza, con
la letra omega abajo: las dos mitades del producto en una sola marca.

CRITERIO DE COLOR: fondo azul muy oscuro para que los acentos resalten, y
tres acentos vivos (menta, violeta, magenta) en vez de uno solo. Un panel
lleno de gris se lee como apagado incluso cuando la información es buena; el
color acá no es decoración, es lo que hace que el estado se entienda de un
vistazo.
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

# Diccionarios de mapeo directo para evitar lógica repetitiva y búsquedas extra
SEVERITY_STYLES: Final[Mapping[str, tuple[HexColor, str]]] = MappingProxyType({
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

# Íconos por sección. Se usan glifos Unicode a propósito: no hacen falta
# archivos de imagen ni una librería de íconos, y Segoe UI Symbol (presente en
# todo Windows 10/11) los tiene todos.
ICONS: Final[Mapping[str, str]] = MappingProxyType({
    "Salud": "\u25c9",        # ◉ diana
    "Limpieza": "\u2726",     # ✦ destello
    "Seguridad": "\u26ca",    # ⛊ escudo
    "Cuarentena": "\u2297",   # ⊗ aislado
    "Memoria": "\u25a4",      # ▤ módulo
    "Disco": "\u25f4",        # ◴ disco
    "Duplicados": "\u29c9",   # ⧉ dos cuadros
    "Navegadores": "\u25d0",  # ◐ globo
    "Inicio": "\u23fb",       # ⏻ encendido
    "Informe": "\u2263",      # ≣ líneas
})

# Paradas del degradado del encabezado: menta -> violeta -> magenta.
GRADIENT_STOPS: Final = ("#00f0c0", "#7c5cff", "#ff2d78")


def app_title() -> str:
    """Retorna el nombre completo de la aplicación y su versión actual."""
    return f"{APP_NAME} v{APP_VERSION}"


@lru_cache(maxsize=32)
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


def icon(section: str | None) -> str:
    """Glifo de una sección, o una viñeta neutra si no tiene uno asignado."""
    if isinstance(section, str) and (glifo := ICONS.get(section.strip())):
        return glifo
    return "\u2022"


def tab_label(section: str) -> str:
    """Nombre de pestaña con su ícono adelante."""
    return f"{icon(section)}  {section}"


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


def severity_icon(severity: str | None) -> str:
    """Símbolo para marcar una severidad dentro de un panel de texto."""
    simbolos = {"ok": "\u2713", "info": "\u2139", "warning": "\u26a0", "danger": "\u2716"}
    if isinstance(severity, str):
        return simbolos.get(severity.lower(), "\u2022")
    return "\u2022"


def grade_color(grade: str | None) -> HexColor:
    """
    Retorna el color asignado a una letra de calificación (A, B, C, D, F).
    """
    if isinstance(grade, str) and grade.strip():
        return GRADE_COLORS.get(grade.upper()[0], PALETTE["text_muted"])
    return PALETTE["text_muted"]


def score_color(score: float | int | None) -> HexColor:
    """Color según un puntaje 0-100, para medidores y barras."""
    try:
        valor = float(score)
    except (TypeError, ValueError):
        return PALETTE["text_muted"]
    if valor >= 90:
        return PALETTE["success"]
    if valor >= 80:
        return PALETTE["info"]
    if valor >= 65:
        return PALETTE["warning"]
    if valor >= 50:
        return "#ff7b39"
    return PALETTE["danger"]


def bar(percent: float | int | None, width: int = 24,
        filled: str = "\u2588", empty: str = "\u2591") -> str:
    """Barra de progreso en texto, para los paneles de resultados.

    Un panel de números sueltos cuesta leer; la misma información con una
    barra al lado se entiende de un vistazo, y no requiere ningún widget.

    Args:
        percent: Valor 0-100. Se recorta al rango, tolera None y basura.
        width: Cantidad de caracteres de la barra.
    """
    try:
        valor = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        valor = 0.0
    ancho = max(1, int(width))
    llenos = int(round(valor / 100 * ancho))
    return filled * llenos + empty * (ancho - llenos)


def _hex_to_rgb(value: HexColor) -> tuple[int, int, int]:
    """Convierte '#rrggbb' a una tupla RGB. Devuelve negro si es inválido."""
    try:
        limpio = value.lstrip("#")
        return (int(limpio[0:2], 16), int(limpio[2:4], 16), int(limpio[4:6], 16))
    except (AttributeError, ValueError, IndexError):
        return (0, 0, 0)


def blend(start: HexColor, end: HexColor, ratio: float) -> HexColor:
    """Mezcla dos colores. `ratio` 0.0 devuelve `start`, 1.0 devuelve `end`.

    Es la base del degradado: Tkinter no tiene degradados nativos, así que se
    dibujan como una serie de líneas de un pixel con el color interpolado.
    """
    try:
        proporcion = max(0.0, min(1.0, float(ratio)))
    except (TypeError, ValueError):
        proporcion = 0.0
    r1, g1, b1 = _hex_to_rgb(start)
    r2, g2, b2 = _hex_to_rgb(end)
    return "#{:02x}{:02x}{:02x}".format(
        int(r1 + (r2 - r1) * proporcion),
        int(g1 + (g2 - g1) * proporcion),
        int(b1 + (b2 - b1) * proporcion),
    )


def gradient_colors(steps: int, stops: tuple[HexColor, ...] = GRADIENT_STOPS) -> list[HexColor]:
    """Lista de colores que recorre las paradas del degradado.

    Args:
        steps: Cuántos colores generar (uno por pixel de ancho, típicamente).
    """
    cantidad = max(1, int(steps))
    if len(stops) < 2:
        return [stops[0] if stops else PALETTE["accent"]] * cantidad
    tramos = len(stops) - 1
    salida: list[HexColor] = []
    for i in range(cantidad):
        posicion = i / max(1, cantidad - 1) * tramos
        indice = min(tramos - 1, int(posicion))
        salida.append(blend(stops[indice], stops[indice + 1], posicion - indice))
    return salida


@lru_cache(maxsize=4)
def logo_svg(size: int = 128) -> str:
    """
    Genera el logo de la aplicación en formato SVG (XML plano).

    Args:
        size: Tamaño en píxeles del lado del contenedor cuadrado.
    """
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

    El escudo se pinta en franjas horizontales de color interpolado, que es la
    forma de conseguir un degradado en Tkinter, y lleva un halo detrás para que
    no se vea plano.

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
        return [x + c * s if i % 2 == 0 else y + c * s for i, c in enumerate(coords)]

    try:
        # Halo: círculos concéntricos que se van acercando al fondo.
        if hasattr(canvas, "create_oval"):
            for paso in range(4, 0, -1):
                radio = 56 * s * (0.6 + paso * 0.12)
                canvas.create_oval(
                    x + 64 * s - radio, y + 58 * s - radio,
                    x + 64 * s + radio, y + 58 * s + radio,
                    fill=blend(PALETTE["surface"], PALETTE["glow"], 0.04 * paso),
                    outline="",
                )

        contorno = pts(64, 18, 100, 31, 100, 67, 90, 90, 64, 110, 38, 90, 28, 67, 28, 31)
        canvas.create_polygon(contorno, fill=GRADIENT_STOPS[1], outline="")

        # Degradado: se recorta el escudo en franjas y cada una se pinta con su
        # color. Tkinter no tiene degradados, así que se simulan así.
        franjas = max(6, int(28 * s))
        colores = gradient_colors(franjas)
        alto = 92 * s / franjas
        for i, tono in enumerate(colores):
            arriba = y + 18 * s + i * alto
            # Ancho del escudo en esa altura: arriba es recto, abajo se afina.
            avance = i / max(1, franjas - 1)
            medio_ancho = 36 * s * (1.0 if avance < 0.55 else 1.0 - (avance - 0.55) * 1.9)
            if medio_ancho <= 0:
                continue
            canvas.create_rectangle(
                x + 64 * s - medio_ancho, arriba,
                x + 64 * s + medio_ancho, arriba + alto + 1,
                fill=tono, outline="",
            ) if hasattr(canvas, "create_rectangle") else None

        canvas.create_line(
            *pts(41, 75, 75, 41), fill=PALETTE["background"],
            width=max(2, int(8 * s)), capstyle="round",
        )
        canvas.create_polygon(pts(75, 41, 89, 38, 92, 52),
                              fill=PALETTE["background"], outline="")
        canvas.create_text(
            *pts(64, 96), text="\u03a9", fill=PALETTE["background"],
            font=("Segoe UI", max(8, int(23 * s)), "bold"),
        )
    except (ValueError, TypeError, AttributeError):
        pass


def draw_gradient_bar(canvas: Any, width: int, height: int = 3,
                      x: int = 0, y: int = 0,
                      stops: tuple[HexColor, ...] = GRADIENT_STOPS) -> None:
    """Pinta una franja de degradado, línea por línea.

    Se usa como separador del encabezado. Es puro color y cuesta nada, pero es
    lo que saca a la ventana del gris uniforme.
    """
    if canvas is None or not hasattr(canvas, "create_line"):
        return
    try:
        ancho = max(1, int(width))
        alto = max(1, int(height))
    except (TypeError, ValueError):
        return
    try:
        for i, tono in enumerate(gradient_colors(ancho, stops)):
            canvas.create_line(x + i, y, x + i, y + alto, fill=tono)
    except (ValueError, TypeError, AttributeError):
        pass


def draw_ring(canvas: Any, percent: float | int, size: int = 150,
              x: int = 0, y: int = 0, thickness: int = 14,
              track: HexColor | None = None,
              fill: HexColor | None = None) -> None:
    """Dibuja un medidor circular de progreso (dona) para el puntaje de salud.

    Un número grande solo dice cuánto; el anillo dice además cuánto falta, y
    eso es lo que hace que se entienda sin leer.

    Args:
        percent: Valor 0-100 que se completa en sentido horario.
        size: Diámetro exterior en píxeles.
        thickness: Grosor del anillo.
        track: Color del fondo del anillo. Por defecto, un tono de superficie.
        fill: Color del avance. Por defecto, según el puntaje.
    """
    if canvas is None or not hasattr(canvas, "create_arc"):
        return
    try:
        valor = max(0.0, min(100.0, float(percent)))
        diametro = max(20, int(size))
        grosor = max(2, min(int(thickness), diametro // 2 - 1))
    except (TypeError, ValueError):
        return

    color_fondo = track or PALETTE["surface_alt"]
    color_avance = fill or score_color(valor)

    try:
        borde = grosor / 2
        caja = (x + borde, y + borde, x + diametro - borde, y + diametro - borde)
        # Pista completa.
        canvas.create_arc(*caja, start=0, extent=359.9, style="arc",
                          outline=color_fondo, width=grosor)
        # Avance, arrancando arriba y girando en sentido horario.
        if valor > 0:
            canvas.create_arc(*caja, start=90, extent=-(valor / 100 * 359.9),
                              style="arc", outline=color_avance, width=grosor)
    except (ValueError, TypeError, AttributeError):
        pass
