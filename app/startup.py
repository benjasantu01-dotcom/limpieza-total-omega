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

# Mensaje estandarizado para deshabilitar programas sin tocar el registro.
HOW_TO_DISABLE: str = (
    "Para deshabilitar un programa de inicio, usá el Administrador de tareas "
    "de Windows (Ctrl+Shift+Esc) → pestaña 'Inicio'. Esta app no modifica el "
    "registro de arranque a propósito por seguridad."
)


@dataclass
class StartupEntry:
    """
    Representa una entrada de inicio (archivo en carpeta o clave de registro).

    Atributos:
        name: Identificador legible de la entrada.
        command: String original obtenido del sistema (ruta o línea de ejecución).
        source: Origen del dato ('registro' o 'carpeta').
    """
    name: str
    command: str
    source: str
    
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    def _is_reserved_device_name(self, path_str: str) -> bool:
        """Verifica si el nombre del archivo es un dispositivo reservado (ej. CON, NUL)."""
        reserved: Set[str] = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1", "COM2", "COM3", "COM4", "LPT2", "LPT3"}
        try:
            return Path(path_str).stem.upper() in reserved
        except (ValueError, TypeError):
            return True

    def _is_path_suspicious(self, path_string: str) -> bool:
        """Determina si una cadena de ruta contiene caracteres maliciosos o bloqueados."""
        suspicious_chars = '<>|?*\0&;%'
        return any(c in path_string for c in suspicious_chars) or path_string.startswith(r"\\")

    def _is_valid_executable(self, path: Path) -> bool:
        """Valida si la extensión es ejecutable y descarta symlinks para evitar recursión/redirección."""
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_command: str) -> str:
        """Filtra caracteres de control y asegura una cadena de comando limpia."""
        if not isinstance(raw_command, str):
            return ""
        return "".join(c for c in raw_command.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_command: str) -> str:
        """
        Extrae una ruta de una cadena de comandos entrecomillada.
        Valida que la ruta no contenga caracteres inválidos para el sistema de archivos.
        """
        if not isinstance(raw_command, str) or len(raw_command) < 3:
            return ""
        
        end_quote: int = raw_command.find('"', 1)
        if end_quote == -1:
            return ""
            
        path_str: str = raw_command[1:end_quote].strip()
        
        if not path_str or any(c in path_str for c in '<>|?*'):
            return ""
        
        try:
            p: Path = Path(path_str)
            if not p.parts or is_protected_path(p):
                return ""
            return str(p)
        except (OSError, ValueError, RuntimeError, TypeError):
            return ""

    def _validate_file_access(self, p: Path) -> bool:
        """Verifica existencia física y descarta rutas con atributos de reparseo (junctions)."""
        try:
            if not os.path.lexists(p) or p.is_dir():
                return False
            stats = p.lstat()
            # 0x400 es el bitmask para FILE_ATTRIBUTE_REPARSE_POINT
            return not p.is_symlink() and not (stats.st_file_attributes & 0x00000400)
        except (OSError, PermissionError):
            return False

    def _resolve_and_cache_path(self, path_string: str) -> str:
        """
        Normaliza, valida contra `is_protected_path` y resuelve la ruta absoluta.
        Usa una caché local para evitar múltiples llamadas al sistema operativo.
        """
        if not path_string or self._is_path_suspicious(path_string):
            return ""
        
        try:
            norm: str = os.path.normpath(path_string)
            if len(norm) > 260 or self._is_reserved_device_name(norm):
                return ""
        except (ValueError, TypeError):
            return ""
        
        if path_string in _EXISTS_CACHE:
            return path_string if _EXISTS_CACHE[path_string] else path_string
        
        try:
            abs_path: str = os.path.abspath(norm)
            p: Path = Path(abs_path)
            
            if not self._validate_file_access(p) or not p.is_absolute() or is_protected_path(p):
                _EXISTS_CACHE[path_string] = False
                return path_string
            
            try:
                real_path_str: str = os.path.realpath(abs_path)
            except (OSError, PermissionError):
                real_path_str = abs_path

            real_path: Path = Path(real_path_str)
            if not real_path.exists() or real_path.is_dir() or is_protected_path(real_path):
                _EXISTS_CACHE[path_string] = False
                return ""
                
            _EXISTS_CACHE[real_path_str] = True
            return real_path_str
        except (OSError, ValueError, RuntimeError, TypeError, PermissionError):
            _EXISTS_CACHE[path_string] = False
            return path_string

    def _resolve_path_from_command(self, command_line: str) -> str:
        """Analiza la línea de comandos para aislar el ejecutable base."""
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
        """Ruta absoluta del ejecutable. Utiliza resolución diferida para optimizar el escaneo."""
        if self._checked_exists:
            return self._exec_cache or ""
            
        self._checked_exists = True
        if not self.command:
            return ""

        cmd: str = self._sanitize_command(self.command)
        self._exec_cache = self._resolve_path_from_command(cmd) if cmd else ""
            
        return self._exec_cache or ""


def startup_folders() -> List[Path]:
    """Retorna las rutas locales de las carpetas de inicio del sistema (NT solamente)."""
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
    """Escanea carpetas de inicio buscando archivos ejecutables."""
    found_entries: List[StartupEntry] = []
    scan_folders = folders if folders is not None else startup_folders()
    
    for folder in scan_folders:
        try:
            if not folder.exists() or not folder.is_dir():
                continue
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            _, ext = os.path.splitext(entry.name)
                            if ext.lower() in EXECUTABLE_EXTS:
                                p_entry = Path(entry.path)
                                if not is_protected_path(p_entry):
                                    found_entries.append(StartupEntry(
                                        name="".join(c for c in os.path.splitext(entry.name)[0] if ord(c) >= 32),
                                        command=entry.path,
                                        source="carpeta"
                                    ))
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError, ValueError):
            continue
    return found_entries


def parse_registry_csv(csv_text: str, source: str = "registro") -> List[StartupEntry]:
    """Parsea la salida CSV de PowerShell para convertirla en objetos StartupEntry."""
    if not isinstance(csv_text, str) or not csv_text.strip():
        return []
        
    parsed_entries: List[StartupEntry] = []
    seen_commands: Set[str] = set()
    
    try:
        f = io.StringIO(csv_text.strip())
        reader = csv.DictReader(f)
        
        if not reader.fieldnames or len(reader.fieldnames) < 2:
            return []
            
        f_name, f_cmd = reader.fieldnames[0], reader.fieldnames[1]
            
        for row in reader:
            try:
                if not isinstance(row, dict):
                    continue
                
                raw_n = row.get(f_name)
                raw_c = row.get(f_cmd)
                
                if not isinstance(raw_n, str) or not isinstance(raw_c, str):
                    continue
                
                name = "".join(c for c in raw_n if ord(c) >= 32).strip()
                cmd = "".join(c for c in raw_c if ord(c) >= 32).strip()
                
                if not name or not cmd or cmd.startswith(r"\\") or cmd in seen_commands:
                    continue
                if name.upper().startswith("PS") or any(c in cmd for c in '<>|?*&;'):
                    continue
                
                p_cmd = Path(cmd)
                if is_protected_path(p_cmd):
                    continue
                    
                seen_commands.add(cmd)
                parsed_entries.append(StartupEntry(name=name, command=cmd, source=source))
            except (KeyError, ValueError, TypeError):
                continue
    except (csv.Error, OSError, ValueError, TypeError):
        return []
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """Ejecuta consulta PowerShell para extraer entradas desde el Registro de Windows."""
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
            clean_out: str = "".join(c for c in result.stdout if ord(c) >= 32 or c in "\r\n")
            return parse_registry_csv(clean_out)
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def list_startup_entries() -> List[StartupEntry]:
    """Consolida las entradas de inicio del sistema usando caché de sesión."""
    global _FULL_SCAN_CACHE
    if _FULL_SCAN_CACHE is not None:
        return _FULL_SCAN_CACHE

    seen_items: Set[Tuple[str, str]] = set()
    unique_entries: List[StartupEntry] = []
    
    for entry in itertools.chain(entries_from_folders(), entries_from_registry()):
        key = (entry.name.lower(), entry.command.lower())
        if key not in seen_items:
            seen_items.add(key)
            unique_entries.append(entry)
            
    _FULL_SCAN_CACHE = unique_entries
    return unique_entries


def estimate_impact(entries: Sequence[StartupEntry]) -> str:
    """Clasifica el impacto en rendimiento según la cantidad de entradas."""
    count: int = len(entries)
    thresholds: List[Tuple[int, str]] = [(5, "ok"), (10, "info"), (18, "warning")]
    for limit, label in thresholds:
        if count <= limit:
            return label
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera un reporte legible de los programas de inicio y su impacto estimado."""
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
