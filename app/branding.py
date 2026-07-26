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

APP_NAME = "Limpieza Total Omega"
APP_SHORT_NAME = "Omega"
APP_TAGLINE = "Limpieza y seguridad, en un solo lugar"
APP_VERSION = "2.0.0"

# Paleta oscura con acento cian. Las claves describen el USO, no el color,
# así se puede cambiar la paleta entera sin renombrar nada en la interfaz.
PALETTE = {
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
}

FONT_SIZES = {
    "title": 24,
    "subtitle": 13,
    "heading": 15,
    "body": 12,
    "mono": 11,
    "caption": 10,
}

# Severidad -> (color, etiqueta legible). Lo usan todos los módulos que
# reportan hallazgos, para que la interfaz los pinte de forma uniforme.
SEVERITY_STYLES = {
    "ok": ("#00d4aa", "Correcto"),
    "info": ("#58a6ff", "Informativo"),
    "warning": ("#f5a623", "Advertencia"),
    "danger": ("#e5484d", "Peligro"),
}

# Grado de salud -> color, para el panel que combina todos los módulos.
GRADE_COLORS = {
    "A": "#00d4aa",
    "B": "#58a6ff",
    "C": "#f5a623",
    "D": "#ff7b39",
    "F": "#e5484d",
}


def app_title() -> str:
    """Título con versión, para la barra de la ventana."""
    return f"{APP_NAME} v{APP_VERSION}"


def color(name: str) -> str:
    """Color de la paleta, con gris neutro de respaldo.

    Devuelve un respaldo en vez de lanzar excepción para que un nombre mal
    escrito en la interfaz no tire abajo la app entera.
    """
    return PALETTE.get(name, "#808080")


def font_size(name: str) -> int:
    """Tamaño de la escala tipográfica, con respaldo al tamaño de cuerpo."""
    return FONT_SIZES.get(name, FONT_SIZES["body"])


def severity_color(severity: str) -> str:
    """Color asociado a una severidad de hallazgo."""
    style = SEVERITY_STYLES.get(str(severity).lower())
    return style[0] if style else color("text_muted")


def severity_label(severity: str) -> str:
    """Etiqueta legible para una severidad."""
    style = SEVERITY_STYLES.get(str(severity).lower())
    return style[1] if style else str(severity).upper()


def grade_color(grade: str) -> str:
    """Color para una nota de salud del sistema (A a F)."""
    return GRADE_COLORS.get(str(grade).upper()[:1], color("text_muted"))


def logo_svg(size: int = 128) -> str:
    """Logo como SVG en texto plano, sin dependencias.

    Escala con `size` sobre un viewBox de 128x128, así el mismo trazado
    sirve para un ícono chico o para una portada grande.
    """
    accent = PALETTE["accent"]
    surface = PALETTE["surface"]
    text = PALETTE["text"]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="omegaShield" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{accent}"/>
      <stop offset="100%" stop-color="#0891b2"/>
    </linearGradient>
  </defs>
  <rect width="128" height="128" rx="28" fill="{surface}"/>
  <path d="M64 20 L98 32 V66 C98 88 82 102 64 108 C46 102 30 88 30 66 V32 Z"
        fill="url(#omegaShield)"/>
  <path d="M42 74 L74 42" stroke="{text}" stroke-width="7" stroke-linecap="round"/>
  <path d="M74 42 L87 39 L90 52 Z" fill="{text}"/>
  <text x="64" y="96" font-family="Segoe UI, Arial, sans-serif" font-size="24"
        font-weight="bold" fill="{text}" text-anchor="middle">&#937;</text>
</svg>
"""


def save_logo_svg(destination: str | Path) -> Path:
    """Guarda el logo SVG en disco y devuelve la ruta escrita."""
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(logo_svg(), encoding="utf-8")
    return path


def logo_ascii() -> str:
    """Logo en ASCII, para la consola o el README."""
    return r"""
   ___  __  __ ___ ___   _
  / _ \|  \/  | __/ __| /_\
 | (_) | |\/| | _|| (_ // _ \
  \___/|_|  |_|___\___/_/ \_\
      Limpieza Total Omega
"""


def draw_logo(canvas, size: int = 56, x: int = 0, y: int = 0) -> None:
    """Dibuja el logo sobre un canvas de Tkinter ya existente.

    Se dibuja con primitivas en vez de cargar una imagen para no depender
    de Pillow ni de archivos externos. El canvas llega por parámetro (no se
    crea acá) para que este módulo se pueda importar en entornos sin
    pantalla, como el runner de CI.
    """
    s = size / 128  # escala respecto del viewBox del SVG

    def pts(*coords: float) -> list[float]:
        return [x + c * s if i % 2 == 0 else y + c * s for i, c in enumerate(coords)]

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
