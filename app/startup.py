"""
startup.py — inventario de programas que arrancan con Windows.

SOLO LECTURA: lista lo que arranca con el sistema y estima su impacto, pero
**no deshabilita ni borra nada**. Tocar las claves de arranque del registro
de forma automática es una de las maneras más rápidas de dejar una PC en
mal estado, así que acá se reporta y se explica; deshabilitar queda para el
Administrador de tareas de Windows, que además guarda respaldo.

Los datos salen de dos lugares:
  1. Las carpetas "Inicio" (del usuario y del sistema).
  2. Las claves Run del registro, leídas vía PowerShell.

El parseo está separado de la lectura (`parse_registry_csv`) para poder
testearlo en CI sobre Linux, sin registro de Windows.
"""

from __future__ import annotations
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

__all__ = [
    "StartupEntry",
    "REGISTRY_RUN_KEYS",
    "startup_folders",
    "entries_from_folders",
    "parse_registry_csv",
    "entries_from_registry",
    "list_startup_entries",
    "estimate_impact",
    "summarize",
    "HOW_TO_DISABLE",
]

# Claves del registro donde los programas se anotan para arrancar solos.
REGISTRY_RUN_KEYS = (
    r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
)

# Se le muestra al usuario en vez de ofrecer un botón que toque el registro.
HOW_TO_DISABLE = (
    "Para deshabilitar un programa de inicio, usá el Administrador de tareas "
    "de Windows (Ctrl+Shift+Esc) → pestaña 'Inicio'. Ahí Windows lleva "
    "registro del cambio y se puede revertir. Esta app no modifica el "
    "registro de arranque a propósito: un error ahí puede dejar el sistema "
    "sin programas esenciales."
)


@dataclass
class StartupEntry:
    """Un programa configurado para arrancar con el sistema."""
    name: str
    command: str
    source: str  # "carpeta" o el nombre de la clave del registro

    @property
    def executable(self) -> str:
        """Intenta extraer solo el ejecutable del comando completo.

        Los comandos de arranque suelen venir con argumentos y comillas;
        esto se queda con la primera parte para poder mostrarla corta.
        """
        if not self.command:
            return ""
        cmd = self.command.strip()
        if cmd.startswith('"'):
            end = cmd.find('"', 1)
            if end > 0:
                return cmd[1:end]
        return cmd.split(" ")[0] if cmd else ""


def startup_folders() -> list[Path]:
    """Carpetas 'Inicio' existentes: la del usuario y la de todos los usuarios."""
    if os.name != "nt":
        return []
    candidates = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("ProgramData")
    if appdata:
        candidates.append(Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup")
    if programdata:
        candidates.append(Path(programdata) / r"Microsoft\Windows\Start Menu\Programs\Startup")
    return [c for c in candidates if c.is_dir()]


def entries_from_folders(folders: Optional[Iterable[Path]] = None) -> list[StartupEntry]:
    """Escanea las carpetas de inicio en busca de accesos directos (.lnk).

    Args:
        folders: Lista opcional de rutas Path para inyectar directorios
            personalizados en entornos de prueba o entornos controlados.
            Si es None, usa las rutas por defecto del sistema operativo.
    """
    if folders is None:
        folders = startup_folders()
    entries: list[StartupEntry] = []
    for folder in folders:
        base = Path(folder)
        if not base.is_dir():
            continue
        try:
            children = sorted(base.iterdir())
        except (OSError, PermissionError):
            continue
        for item in children:
            if item.is_file() and item.name.lower() != "desktop.ini":
                entries.append(StartupEntry(name=item.stem, command=str(item), source="carpeta"))
    return entries


def parse_registry_csv(text: str, source: str = "registro") -> list[StartupEntry]:
    """Interpreta la salida CSV de PowerShell con las claves Run.

    Formato esperado: Name,Value (con encabezado). Se ignoran las entradas
    internas de PowerShell (PS*) porque no son programas de arranque.
    """
    entries: list[StartupEntry] = []
    if not text:
        return entries
        
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split solo en la primera coma para separar Name y Value
        parts = [p.strip().strip('"') for p in line.split(",", 1)]
        if len(parts) < 2 or not parts[0]:
            continue
        name, value = parts[0], parts[1]
        if name.lower() == "name" or name.startswith("PS"):
            continue
        entries.append(StartupEntry(name=name, command=value, source=source))
    return entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> list[StartupEntry]:
    """Lee las claves Run del registro. Solo lectura, vía PowerShell."""
    if os.name != "nt":
        return []
    entries: list[StartupEntry] = []
    for key in keys:
        command = (
            f"if (Test-Path '{key}') {{ (Get-Item '{key}').Property | ForEach-Object "
            f"{{ [PSCustomObject]@{{ Name = $_; Value = (Get-ItemProperty '{key}').$_ }} }} | "
            "ConvertTo-Csv -NoTypeInformation }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                entries.extend(parse_registry_csv(result.stdout, source=key))
        except (OSError, subprocess.SubprocessError):
            continue
    return entries


def list_startup_entries() -> list[StartupEntry]:
    """Todos los programas de arranque detectados, sin duplicados por nombre."""
    entries = entries_from_folders() + entries_from_registry()
    vistos: set[str] = set()
    unicos: list[StartupEntry] = []
    for entry in entries:
        clave = entry.name.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append(entry)
    return unicos


def estimate_impact(entries: Iterable[StartupEntry]) -> str:
    """Estima el impacto del arranque según la cantidad de programas.

    Es una heurística por cantidad, no una medición real de tiempo: se
    aclara así para no dar una cifra falsa de "segundos de arranque".
    """
    if entries is None:
        return "ok"
    
    # Aseguramos iterable y calculamos total
    try:
        total = len(list(entries))
    except (TypeError, ValueError):
        return "ok"

    if total == 0:
        return "ok"
    if total <= 5:
        return "ok"
    if total <= 10:
        return "info"
    if total <= 18:
        return "warning"
    return "danger"


def summarize(entries: Optional[Iterable[StartupEntry]] = None) -> list[str]:
    """Resumen legible del arranque, con la advertencia de cómo desactivar."""
    if entries is None:
        entries = list_startup_entries()
    entries_list = list(entries)
    lines = [f"Programas que arrancan con el sistema: {len(entries_list)}"]
    nivel = estimate_impact(entries_list)
    mensajes = {
        "ok": "Arranque liviano: no hay mucho para ganar acá.",
        "info": "Cantidad normal de programas al inicio.",
        "warning": "Bastantes programas al inicio; revisá si los usás todos.",
        "danger": "Muchos programas al inicio: es probable que el arranque sea lento.",
    }
    lines.append(mensajes.get(nivel, ""))
    lines.append("")
    for entry in entries_list:
        lines.append(f"  {entry.name:<28} [{entry.source}]")
        if entry.executable:
            lines.append(f"      {entry.executable}")
    if entries_list:
        lines.extend(["", HOW_TO_DISABLE])
    return lines
