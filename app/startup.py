"""
startup.py — inventario de programas que arrancan con Windows.

SOLO LECTURA: lista lo que arranca con el sistema y estima su impacto, pero
**no deshabilita ni borra nada**. Tocar las claves de arranque del registro
de forma administrativa es una de las maneras más rápidas de dejar una PC en
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
import csv
import io
import itertools
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Iterable, Optional, Iterator, List, Tuple, Dict, Sequence, Set, Union
)
from safety import is_protected_path

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

# Claves del registro donde los programas se registran para inicio automático:
# HKCU (Current User): Programas específicos del usuario logueado.
# HKLM (Local Machine): Programas que arrancan para todos los usuarios.
# WOW6432Node: Claves para aplicaciones de 32 bits en sistemas de 64 bits.
REGISTRY_RUN_KEYS: Tuple[str, ...] = (
    r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
)

# Extensiones consideradas ejecutables para el escaneo de carpetas.
EXECUTABLE_EXTS: Set[str] = {'.exe', '.bat', '.cmd', '.scr', '.lnk'}

# Caché global para evitar operaciones de I/O redundantes durante la sesión.
_EXISTS_CACHE: Dict[str, bool] = {}
_FULL_SCAN_CACHE: Optional[List[StartupEntry]] = None
_REGISTRY_CACHE: Optional[List[StartupEntry]] = None

# Mensaje estandarizado para deshabilitar programas sin tocar el registro.
HOW_TO_DISABLE: str = (
    "Para deshabilitar un programa de inicio, usá el Administrador de tareas "
    "de Windows (Ctrl+Shift+Esc) → pestaña 'Inicio'. Ahí Windows lleva "
    "registro del cambio y se puede revertir. Esta app no modifica el "
    "registro de arranque a propósito: un error ahí puede dejar el sistema "
    "sin programas esenciales."
)


@dataclass
class StartupEntry:
    """
    Representa una entrada de inicio detectada, sea de carpetas o registro.
    
    Estrategia 'lazy': la resolución real de la ruta en disco (validación de 
    existencia, reparse points y normalización) solo ocurre al acceder a 
    la propiedad .executable, ahorrando ciclos de I/O durante el escaneo inicial.
    """
    name: str
    command: str
    source: str
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    def _is_valid_executable(self, path: Path) -> bool:
        """Verifica si la extensión es válida y si la ruta no es un enlace simbólico."""
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_command: str) -> str:
        """Elimina caracteres de control y espacios en blanco de una línea de comandos."""
        if not isinstance(raw_command, str):
            return ""
        return "".join(c for c in raw_command.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_command: str) -> str:
        """Aísla y valida una ruta de archivo encerrada entre comillas dobles."""
        if not isinstance(raw_command, str) or len(raw_command) < 2:
            return ""
        end_quote: int = raw_command.find('"', 1)
        if end_quote == -1:
            return ""
        path_str: str = raw_command[1:end_quote].strip()
        
        if not path_str or any(c in path_str for c in '<>|?*'):
            return ""
        
        try:
            p: Path = Path(path_str)
            if is_protected_path(p):
                return ""
            return str(p)
        except (OSError, ValueError, RuntimeError, TypeError):
            return ""

    def _resolve_and_cache_path(self, path_string: str) -> str:
        """Realiza la resolución profunda de la ruta: normaliza, verifica existencia y reparse points."""
        if not isinstance(path_string, str) or not path_string:
            return ""
        # Filtrado preventivo de caracteres ilegales y rutas UNC inseguras
        if any(c in path_string for c in '<>|?*\0&;') or path_string.startswith(r"\\"):
            return ""
        
        try:
            # Normalización inicial para evitar ataques de salto de directorio
            norm = os.path.normpath(path_string)
            # Limite de longitud Windows MAX_PATH (260) para prevenir desbordamientos en APIs de sistema
            if len(norm) > 260:
                return ""
            # Bloqueo de nombres de dispositivos reservados en Windows
            stem = Path(norm).stem.upper()
            if stem in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1", "COM2", "COM3", "COM4", "LPT2", "LPT3"}:
                return ""
        except (ValueError, TypeError):
            return ""
        
        if path_string in _EXISTS_CACHE:
            return path_string if _EXISTS_CACHE[path_string] else path_string
        
        try:
            abs_path = os.path.abspath(norm)
            p: Path = Path(abs_path)
            
            # Verificación de existencia segura
            if p.exists():
                stat_info = p.lstat()
                # Verifica el bit de Reparse Point (FILE_ATTRIBUTE_REPARSE_POINT)
                if (stat_info.st_file_attributes & 0x00000400) != 0:
                    _EXISTS_CACHE[path_string] = False
                    return ""

            if not p.is_absolute() or is_protected_path(p) or p.is_symlink():
                _EXISTS_CACHE[path_string] = False
                return path_string
            
            try:
                real_path_str: str = os.path.realpath(abs_path)
            except (OSError, PermissionError):
                _EXISTS_CACHE[path_string] = False
                return ""

            # Validación final de integridad de ruta tras resolución
            if not real_path_str.startswith(os.path.splitdrive(abs_path)[0]):
                _EXISTS_CACHE[path_string] = False
                return ""

            real_path: Path = Path(real_path_str)
            
            if not real_path_str or not real_path.exists() or is_protected_path(real_path):
                _EXISTS_CACHE[path_string] = False
                return ""
                
            _EXISTS_CACHE[real_path_str] = True
            return real_path_str
        except (OSError, ValueError, RuntimeError, TypeError, PermissionError):
            _EXISTS_CACHE[path_string] = False
            return path_string

    def _resolve_path_from_command(self, command_line: str) -> str:
        """
        Tokeniza una línea de comandos completa para extraer y resolver el ejecutable.
        
        Args:
            command_line: Línea de comandos cruda extraída del registro o acceso directo.
        Returns:
            Ruta absoluta normalizada si es un ejecutable válido, de lo contrario cadena vacía.
        """
        if not command_line or not isinstance(command_line, str):
            return ""
        if any(char in command_line for char in ('&', '|', ';', '>', '<', '$', '`', '(', ')')):
            return ""

        if command_line.startswith('"'):
            return self._extract_quoted_path(command_line)
            
        try:
            parts: List[str] = command_line.split()
            if not parts:
                return ""
            return self._resolve_and_cache_path(parts[0])
        except (AttributeError, ValueError):
            return ""
        
    @property
    def executable(self) -> str:
        """Retorna la ruta absoluta validada del ejecutable, resolviéndola bajo demanda (lazy)."""
        if self._checked_exists:
            return self._exec_cache or ""
            
        self._checked_exists = True
        if not self.command:
            return ""

        cmd: str = self._sanitize_command(self.command)
        self._exec_cache = self._resolve_path_from_command(cmd) if cmd else ""
            
        return self._exec_cache or ""


def startup_folders() -> List[Path]:
    """Retorna las rutas base donde Windows almacena los accesos directos de inicio."""
    if os.name != "nt":
        return []
    candidates: List[Path] = []
    appdata: Optional[str] = os.environ.get("APPDATA")
    programdata: Optional[str] = os.environ.get("ProgramData")
    try:
        if appdata:
            candidates.append(Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup")
        if programdata:
            candidates.append(Path(programdata) / r"Microsoft\Windows\Start Menu\Programs\Startup")
    except (ValueError, TypeError, OSError):
        pass
    return [c for c in candidates if c and c.is_dir() and not is_protected_path(c)]


def entries_from_folders(folders: Optional[Sequence[Path]] = None) -> List[StartupEntry]:
    """Escanea el sistema de archivos buscando ejecutables en las carpetas de inicio."""
    found_entries: List[StartupEntry] = []
    scan_folders = folders if folders is not None else startup_folders()
    
    for folder in scan_folders:
        if not isinstance(folder, Path) or is_protected_path(folder):
            continue
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    name, ext = os.path.splitext(entry.name)
                    if ext.lower() not in EXECUTABLE_EXTS:
                        continue
                    
                    p_entry = Path(entry.path)
                    if entry.is_file(follow_symlinks=False) and not p_entry.is_symlink():
                        if not is_protected_path(p_entry):
                            found_entries.append(StartupEntry(
                                name=name,
                                command=entry.path,
                                source="carpeta"
                            ))
        except (OSError, PermissionError):
            continue
    return found_entries


def parse_registry_csv(csv_text: str, source: str = "registro") -> List[StartupEntry]:
    """
    Convierte el CSV generado por PowerShell en una lista de objetos StartupEntry.
    
    Args:
        csv_text: Salida cruda de 'ConvertTo-Csv' capturada de PowerShell.
        source: Identificador del origen para el reporte de usuario.
    Returns:
        Lista de entradas validadas y sanitizadas para su ejecución segura.
    """
    if not isinstance(csv_text, str) or not csv_text.strip():
        return []
        
    parsed_entries: List[StartupEntry] = []
    try:
        f = io.StringIO(csv_text.strip())
        reader: csv.DictReader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        # Verificación estricta: requiere al menos 2 columnas para ser válida
        if not fieldnames or len(fieldnames) < 2:
            return []
            
        for row in reader:
            if not isinstance(row, dict) or len(row) < 2:
                continue
            try:
                # Usar los dos primeros campos detectados en el CSV del registro
                name_raw = row.get(fieldnames[0], "")
                cmd_raw = row.get(fieldnames[1], "")
                
                if name_raw is None or cmd_raw is None:
                    continue
                
                name: str = "".join(c for c in name_raw if ord(c) >= 32).strip()
                cmd: str = "".join(c for c in cmd_raw if ord(c) >= 32).strip()
                
                # Ignorar campos PS (PowerShell metadata) o entradas vacías
                if not name or not cmd or name.upper().startswith("PS"):
                    continue
                if any(c in cmd for c in '<>|?*'):
                    continue
                
                try:
                    if is_protected_path(Path(cmd)):
                        continue
                except (ValueError, TypeError, OSError):
                    continue
                
                parsed_entries.append(StartupEntry(name=name, command=cmd, source=source))
            except (KeyError, ValueError, TypeError, AttributeError, OSError):
                continue
    except (csv.Error, OSError, ValueError, TypeError):
        return []
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """Ejecuta consulta a PowerShell para extraer programas de las claves Run del Registro."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE

    if os.name != "nt":
        return []
    
    targets: str = ", ".join(f"'{k}'" for k in keys)
    ps_cmd: str = f"Get-ItemProperty {targets} -ErrorAction SilentlyContinue | Select-Object * -ExcludeProperty PS* | ConvertTo-Csv -NoTypeInformation"
    
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            # Filtro defensivo: asegurar que solo contenga caracteres imprimibles
            clean_out = "".join(c for c in result.stdout if ord(c) >= 32 or c in "\r\n")
            _REGISTRY_CACHE = parse_registry_csv(clean_out)
            return _REGISTRY_CACHE
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def list_startup_entries() -> List[StartupEntry]:
    """Agrega todas las entradas de inicio detectadas, eliminando duplicados por nombre."""
    global _FULL_SCAN_CACHE
    if _FULL_SCAN_CACHE is not None:
        return _FULL_SCAN_CACHE

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_folders = executor.submit(entries_from_folders)
        f_registry = executor.submit(entries_from_registry)
        
        seen_names: Set[str] = set()
        unique_entries: List[StartupEntry] = []
        
        for entry in itertools.chain(f_folders.result(), f_registry.result()):
            name_n: str = entry.name.lower()
            if name_n not in seen_names:
                seen_names.add(name_n)
                unique_entries.append(entry)
            
    _FULL_SCAN_CACHE = unique_entries
    return unique_entries


def estimate_impact(entries: Sequence[StartupEntry]) -> str:
    """Calcula un nivel de impacto basado en la cantidad de elementos en la lista."""
    count: int = len(entries)
    thresholds: List[Tuple[int, str]] = [(5, "ok"), (10, "info"), (18, "warning")]
    for limit, label in thresholds:
        if count <= limit:
            return label
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera un reporte textual formateado sobre los programas de inicio detectados."""
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
