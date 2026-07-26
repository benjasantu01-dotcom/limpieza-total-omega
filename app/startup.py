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
from typing import Iterable, Optional, Iterator, List
from app.safety import ensure_safe_to_modify

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
            # Si no hay comilla de cierre, se asume toda la cadena como ejecutable
            return cmd[1:end] if end > 0 else cmd[1:]
        return cmd.split(" ")[0] if cmd else ""


def startup_folders() -> List[Path]:
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


def entries_from_folders(folders: Optional[Iterable[Path]] = None) -> List[StartupEntry]:
    """Escanea las carpetas de inicio en busca de accesos directos (.lnk).

    Args:
        folders: Lista opcional de rutas Path para inyectar directorios
            personalizados en entornos de prueba o entornos controlados.
            Si es None, usa las rutas por defecto del sistema operativo.
    """
    if folders is None:
        folders = startup_folders()
    found_entries: List[StartupEntry] = []
    for folder in folders:
        # Validación de seguridad: verificamos que la carpeta de inicio sea segura
        try:
            ensure_safe_to_modify(folder)
        except (ValueError, PermissionError):
            continue

        base_path = Path(folder).resolve()
        if not base_path.is_dir():
            continue
        try:
            items = sorted(base_path.iterdir())
        except (OSError, PermissionError):
            continue
        for item in items:
            if item.is_file() and item.name.lower() != "desktop.ini":
                # Validación de seguridad: el ítem DEBE residir físicamente en 'base'
                if base_path in item.resolve().parents:
                    found_entries.append(StartupEntry(name=item.stem, command=str(item), source="carpeta"))
    return found_entries


def parse_registry_csv(text: str, source: str = "registro") -> List[StartupEntry]:
    """Interpreta la salida CSV de PowerShell con las claves Run.

    Args:
        text: Salida cruda del comando 'ConvertTo-Csv' de PowerShell.
        source: Identificador del origen (usualmente la ruta de la clave).

    Formato esperado: Name,Value. Se ignoran entradas internas de PowerShell (PS*).
    """
    parsed_entries: List[StartupEntry] = []
    if not text:
        return parsed_entries
        
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split conservador: el registro puede contener comas en los paths
        parts = line.split(",", 1)
        if len(parts) < 2:
            continue
            
        # Limpieza robusta de las columnas CSV generadas por PowerShell
        name = parts[0].strip().strip('"').strip("'")
        value = parts[1].strip().strip('"').strip("'")
        
        if not name or name.lower() == "name" or name.startswith("PS"):
            continue
        parsed_entries.append(StartupEntry(name=name, command=value, source=source))
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """Lee las claves Run del registro. Solo lectura, vía PowerShell."""
    if os.name != "nt":
        return []
    all_entries: List[StartupEntry] = []
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
                all_entries.extend(parse_registry_csv(result.stdout, source=key))
        except (OSError, subprocess.SubprocessError):
            continue
    return all_entries


def list_startup_entries() -> List[StartupEntry]:
    """Todos los programas de arranque detectados, sin duplicados por nombre."""
    seen_names: set[str] = set()
    unique_entries: List[StartupEntry] = []
    
    def _gen_entries() -> Iterator[StartupEntry]:
        yield from entries_from_folders()
        yield from entries_from_registry()

    for entry in _gen_entries():
        key = entry.name.lower()
        if key not in seen_names:
            seen_names.add(key)
            unique_entries.append(entry)
    return unique_entries


def estimate_impact(entries: Iterable[StartupEntry]) -> str:
    """Estima el impacto del arranque según la cantidad de programas."""
    total_count = sum(1 for _ in entries)
    if total_count <= 5:
        return "ok"
    if total_count <= 10:
        return "info"
    if total_count <= 18:
        return "warning"
    return "danger"


def summarize(entries: Optional[Iterable[StartupEntry]] = None) -> List[str]:
    """Genera un reporte legible de los programas de inicio y su impacto."""
    entries_list = list(entries) if entries is not None else list_startup_entries()
        
    lines = [f"Programas que arrancan con el sistema: {len(entries_list)}"]
    impact_level = estimate_impact(entries_list)
    impact_messages = {
        "ok": "Arranque liviano: no hay mucho para ganar acá.",
        "info": "Cantidad normal de programas al inicio.",
        "warning": "Bastantes programas al inicio; revisá si los usás todos.",
        "danger": "Muchos programas al inicio: es probable que el arranque sea lento.",
    }
    lines.append(impact_messages.get(impact_level, ""))
    lines.append("")
    for entry in entries_list:
        lines.append(f"  {entry.name:<28} [{entry.source}]")
        if entry.executable:
            lines.append(f"      {entry.executable}")
    if entries_list:
        lines.extend(["", HOW_TO_DISABLE])
    return lines
