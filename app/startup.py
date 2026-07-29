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
        Extrae la ruta absoluta de una cadena delimitada por comillas dobles.
        
        Args:
            raw_cmd: Cadena de comando cruda que comienza con comillas.
            
        Returns:
            La ruta extraída si es válida y existe, o una cadena vacía en caso contrario.
        """
        end_quote: int = raw_cmd.find('"', 1)
        if end_quote == -1:
            return ""
        path = raw_cmd[1:end_quote]
        # Validación de integridad de ruta ante caracteres ilegales o rutas vacías
        if not path or any(c in path for c in '<>|?*'):
            return ""
        # Valida que sea ejecutable real o exista antes de retornarlo
        if path.lower().endswith(('.exe', '.bat', '.cmd', '.scr')) or os.path.exists(path):
            return path
        return ""

    @property
    def executable(self) -> str:
        """
        Extrae y normaliza la ruta del archivo ejecutable de la línea de comando.
        
        Si la cadena comienza con comillas, utiliza el extractor de rutas citado; 
        en caso contrario, asume que el primer token es el binario.
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
    
    Verifica la existencia del directorio antes de incluirlo en la lista.
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
                    # Chequeo defensivo: no seguir symlinks ni puntos de reparse (junctions)
                    if item.is_file() and not item.is_symlink():
                        resolved_item: Path = item.resolve()
                        # Verificar que el ítem resuelto esté efectivamente bajo la carpeta base
                        if item.name.lower() != "desktop.ini" and base_path == resolved_item.parent:
                            found_entries.append(StartupEntry(name=item.stem, command=str(item), source="carpeta"))
                except (OSError, PermissionError, RuntimeError):
                    continue
        except (OSError, PermissionError):
            continue
    return found_entries


def parse_registry_csv(text: str, source: str = "registro") -> List[StartupEntry]:
    """
    Transforma el volcado CSV de PowerShell en objetos StartupEntry.
    
    Args:
        text: Salida cruda de PowerShell en formato CSV.
        source: Identificador de la fuente (usualmente la clave de registro).
    """
    parsed_entries: List[StartupEntry] = []
    if not isinstance(text, str) or not text.strip():
        return parsed_entries
        
    for line in text.splitlines():
        clean_line: str = line.strip()
        if not clean_line:
            continue
        csv_row_parts: List[str] = clean_line.split(",", 1)
        if len(csv_row_parts) < 2:
            continue
            
        name_raw: str = csv_row_parts[0].strip().strip('"').strip("'")
        value_raw: str = csv_row_parts[1].strip().strip('"').strip("'")
        
        if not name_raw or not value_raw:
            continue
            
        # Filtra metadatos de PowerShell
        if name_raw.lower() in ("name", "pscustomobject") or name_raw.upper().startswith("PS"):
            continue
            
        parsed_entries.append(StartupEntry(name=name_raw, command=value_raw, source=source))
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """
    Obtiene los programas de inicio consultando las claves del Registro vía PowerShell.
    
    Genera un único script consolidado para minimizar el costo de invocación 
    de procesos externos.
    """
    if os.name != "nt":
        return []
    all_entries: List[StartupEntry] = []
    
    query_parts = []
    for key in keys:
        if isinstance(key, str):
            safe_key = subprocess.list2cmdline([key])
            query_parts.append(f"Write-Host '{key}'; (Get-ItemProperty {safe_key}).psobject.properties | Select-Object Name, Value | ConvertTo-Csv -NoTypeInformation")
    
    ps_cmd = " ; ".join(query_parts)
    
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            current_source = "registro"
            for line in result.stdout.splitlines():
                if line.startswith("'HK"):
                    current_source = line.strip("'")
                    continue
                if '","' in line or (line.startswith('"') and line.endswith('"')):
                    all_entries.extend(parse_registry_csv(line, source=current_source))
    except (OSError, subprocess.SubprocessError):
        pass
    return all_entries


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
    count = len(entries)
    thresholds = [(5, "ok"), (10, "info"), (18, "warning")]
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
