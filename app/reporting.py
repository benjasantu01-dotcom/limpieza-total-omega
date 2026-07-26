"""
reporting.py — reporte unificado de todos los módulos.

Junta la salida de limpieza, seguridad, memoria, disco, duplicados,
arranque y cuarentena en un solo informe exportable a texto o Markdown.
Sirve para dos cosas: que el usuario tenga un archivo para guardar, y para
mostrar en una demo el estado completo del sistema de un vistazo.

Todas las funciones son puras: reciben los datos ya recolectados y solo
arman texto. No tocan el disco salvo `save_report`, que escribe donde el
usuario pida.
"""

from __future__ import annotations
from datetime import datetime
from pathlib import Path

import branding

__all__ = [
    "REPORT_SECTIONS",
    "section",
    "build_report",
    "build_markdown",
    "save_report",
    "quick_summary",
]

# Orden en que aparecen las secciones del informe.
REPORT_SECTIONS = (
    "salud",
    "seguridad",
    "cuarentena",
    "limpieza",
    "duplicados",
    "memoria",
    "disco",
    "navegadores",
    "arranque",
)

SECTION_TITLES = {
    "salud": "Salud general del sistema",
    "seguridad": "Seguridad (escaneo heurístico)",
    "cuarentena": "Cuarentena",
    "limpieza": "Archivos temporales y basura",
    "duplicados": "Archivos duplicados",
    "memoria": "Memoria RAM",
    "disco": "Uso de disco",
    "navegadores": "Caché de navegadores",
    "arranque": "Programas de inicio",
}


def section(title: str, lines, underline: str = "-") -> list[str]:
    """Arma una sección con título subrayado y su contenido."""
    content = [str(line) for line in (lines or ["(sin datos)"])]
    return [title, underline * len(title), *content, ""]


def _header() -> list[str]:
    """Encabezado del informe con marca y fecha."""
    return [
        branding.APP_NAME,
        branding.APP_TAGLINE,
        f"Versión {branding.APP_VERSION}",
        f"Informe generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
        "",
    ]


def build_report(data: dict) -> str:
    """Informe en texto plano.

    `data` es un diccionario {sección: lista de líneas}. Las secciones que
    falten simplemente no aparecen, así se puede generar un informe parcial
    con solo los análisis que el usuario ya corrió.
    """
    lines = _header()
    for key in REPORT_SECTIONS:
        if key in data:
            lines.extend(section(SECTION_TITLES.get(key, key.capitalize()), data[key]))
    if len(lines) == len(_header()):
        lines.append("Todavía no se ejecutó ningún análisis.")
    return "\n".join(lines)


def build_markdown(data: dict) -> str:
    """Informe en Markdown, para pegar en un documento o en el repo."""
    lines = [
        f"# {branding.APP_NAME}",
        "",
        f"*{branding.APP_TAGLINE}*",
        "",
        f"- Versión: `{branding.APP_VERSION}`",
        f"- Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for key in REPORT_SECTIONS:
        if key not in data:
            continue
        lines.append(f"## {SECTION_TITLES.get(key, key.capitalize())}")
        lines.append("")
        lines.append("```")
        lines.extend(str(line) for line in (data[key] or ["(sin datos)"]))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def save_report(data: dict, destination: str | Path, as_markdown: bool = False) -> Path:
    """Guarda el informe en disco y devuelve la ruta escrita."""
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = build_markdown(data) if as_markdown else build_report(data)
    path.write_text(content, encoding="utf-8")
    return path


def quick_summary(data: dict) -> str:
    """Una línea con qué análisis se corrieron y cuántos hallazgos hubo."""
    if not data:
        return "Sin análisis ejecutados todavía."
    partes = []
    for key in REPORT_SECTIONS:
        if key in data:
            cantidad = len(data[key] or [])
            partes.append(f"{key}: {cantidad} línea(s)")
    return " | ".join(partes)
