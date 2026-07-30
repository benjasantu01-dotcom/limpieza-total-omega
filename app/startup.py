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
from typing import Iterable, Optional, Iterator, List, Tuple, Dict, Sequence

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
REGISTRY_RUN_KEYS: Tuple[str, ...] = (
    r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
)

# Se le muestra al usuario en vez de ofrecer un botón que toque el registro.
HOW_TO_DISABLE: str = (
    "Para deshabilitar un programa de inicio, usá el Administrador de tareas "
    "de Windows (Ctrl+Shift+Esc) → pestaña 'Inicio'. Ahí Windows lleva "
    "registro del cambio y se puede revertir. Esta app no modifica el "
    "registro de arranque a propósito: un error ahí puede dejar el sistema "
    "sin programas esenciales."
)


@dataclass
class StartupEntry:
    """Representa una entrada de inicio (programa) detectada en el sistema."""
    name: str
    command: str
    source: str  # Indica si proviene de una carpeta o una ruta de registro específica

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """
        Analiza cadenas de comandos que envuelven rutas en comillas.
        
        El proceso busca la segunda comilla para aislar la ruta del resto de
        argumentos o parámetros, validando además la ausencia de caracteres 
        prohibidos en el sistema de archivos de Windows.
        
        Args:
            raw_cmd: Cadena de comando cruda (ej. '"C:\Ruta\App.exe" /param').
            
        Returns:
            Ruta absoluta del ejecutable si es válida, caso contrario string vacío.
        """
        end_quote: int = raw_cmd.find('"', 1)
        if end_quote == -1:
            return ""
        path = raw_cmd[1:end_quote]
        # Validación: evita rutas con caracteres reservados o vacías
        if not path or any(c in path for c in '<>|?*'):
            return ""
        # Criterio: se considera ejecutable si tiene extensión binaria o si existe físicamente
        if path.lower().endswith(('.exe', '.bat', '.cmd', '.scr')) or os.path.exists(path):
            return path
        return ""

    @property
    def executable(self) -> str:
        """
        Obtiene la ruta normalizada del ejecutable.
        
        Si el comando utiliza comillas (típico de rutas con espacios), delega
        en el extractor especializado; si no, asume el primer bloque de texto.
        """
        cmd: str = self.command.strip()
        if not cmd:
            return ""
        
        if cmd.startswith('"'):
            return self._extract_quoted_path(cmd)
        
        # Caso: ruta sin citar, tomamos el primer token como posible ejecutable
        parts: List[str] = cmd.split()
        return parts[0] if parts else ""


def startup_folders() -> List[Path]:
    """
    Retorna las rutas a las carpetas 'Inicio' (usuario y sistema) del sistema.
    """
    if os.name != "nt":
        return []
    candidates: List[Path] = []
    appdata: Optional[str] = os.environ.get("APPDATA")
    programdata: Optional[str] = os.environ.get("ProgramData")
    if appdata:
        candidates.append(Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup")
    if programdata:
        candidates.append(Path(programdata) / r"Microsoft\Windows\Start Menu\Programs\Startup")
    return [c for c in candidates if c.is_dir()]


def entries_from_folders(folders: Optional[Sequence[Path]] = None) -> List[StartupEntry]:
    """
    Escanea las carpetas de inicio en busca de ejecutables o accesos directos.

    Seguridad: Ignora 'desktop.ini', enlaces simbólicos y puntos de reparse.
    """
    if folders is None:
        folders = startup_folders()
    found_entries: List[StartupEntry] = []
    for folder in folders:
        try:
            base_path: Path = folder.resolve()
        except (ValueError, PermissionError, OSError):
            continue

        try:
            for item in base_path.iterdir():
                try:
                    if item.is_file() and not item.is_symlink():
                        resolved_item: Path = item.resolve()
                        # Verificación estricta de jerarquía para evitar escapes de carpeta
                        if item.name.lower() != "desktop.ini" and base_path == resolved_item.parent:
                            found_entries.append(StartupEntry(name=item.stem, command=str(item), source="carpeta"))
                except (OSError, PermissionError, RuntimeError):
                    continue
        except (OSError, PermissionError):
            continue
    return found_entries


def parse_registry_csv(text: str, source: str = "registro") -> List[StartupEntry]:
    """
    Convierte el CSV de PowerShell en objetos de dominio StartupEntry.
    """
    parsed_entries: List[StartupEntry] = []
    if not isinstance(text, str) or not text.strip():
        return parsed_entries
        
    for line in text.splitlines():
        clean_line: str = line.strip()
        if not clean_line or ',' not in clean_line:
            continue
            
        parts: List[str] = clean_line.split(",", 1)
        if len(parts) < 2:
            continue
            
        name_raw: str = parts[0].strip().strip('"\'')
        value_raw: str = parts[1].strip().strip('"\'')
        
        # Ignorar metadatos de PowerShell que no son entradas del sistema
        if not name_raw or name_raw.lower() in ("name", "pscustomobject") or name_raw.upper().startswith("PS"):
            continue
            
        parsed_entries.append(StartupEntry(name=name_raw, command=value_raw, source=source))
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """
    Consulta las claves del Registro de Windows vía PowerShell.
    
    Genera un script consolidado para cada clave (prefijado por 'SRCDATA') 
    para identificar la fuente de cada entrada tras el parseo del CSV resultante.
    """
    if os.name != "nt":
        return []
    
    query_parts: List[str] = []
    for key in keys:
        if isinstance(key, str):
            safe_key: str = subprocess.list2cmdline([key])
            # La consulta extrae solo los pares Name/Value del registro
            query_parts.append(f"Write-Host 'SRCDATA:{key}'; (Get-ItemProperty {safe_key}).psobject.properties | Select-Object Name, Value | ConvertTo-Csv -NoTypeInformation")
    
    ps_cmd: str = " ; ".join(query_parts)
    
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            entries: List[StartupEntry] = []
            current_source: str = "registro"
            for line in result.stdout.splitlines():
                if line.startswith("SRCDATA:"):
                    current_source = line[8:]
                    continue
                if ',' in line:
                    entries.extend(parse_registry_csv(line, source=current_source))
            return entries
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def list_startup_entries() -> List[StartupEntry]:
    """Consolida las entradas de carpetas y registro, eliminando duplicados por nombre."""
    seen_names: set[str] = set()
    unique_entries: List[StartupEntry] = []
    
    def _gen_entries() -> Iterator[StartupEntry]:
        yield from entries_from_folders()
        yield from entries_from_registry()

    for entry in _gen_entries():
        key: str = entry.name.lower()
        if key not in seen_names:
            seen_names.add(key)
            unique_entries.append(entry)
    return unique_entries


def estimate_impact(entries: Sequence[StartupEntry]) -> str:
    """Clasifica el impacto en el rendimiento basado en la cantidad de entradas."""
    count: int = len(entries)
    thresholds: List[Tuple[int, str]] = [(5, "ok"), (10, "info"), (18, "warning")]
    for limit, label in thresholds:
        if count <= limit:
            return label
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera un informe textual legible de los programas de inicio detectados."""
    entries_list: Sequence[StartupEntry] = entries if entries is not None else list_startup_entries()
    total_count: int = len(entries_list)
        
    lines: List[str] = [f"Programas que arrancan con el sistema: {total_count}"]
    impact_level: str = estimate_impact(entries_list)
    impact_messages: Dict[str, str] = {
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
    if total_count > 0:
        lines.extend(["", HOW_TO_DISABLE])
    return lines
