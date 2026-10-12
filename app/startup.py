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
    Iterable, Optional, Iterator, List, Tuple, Dict, Sequence, Set, Union, TypeAlias
)
from safety import is_protected_path, is_safe_to_modify

# Type aliases para mejorar la legibilidad de las firmas de funciones
StartupEntries: TypeAlias = List["StartupEntry"]
RegistryKeySet: TypeAlias = Iterable[str]

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

# Claves del registro origen para el inventario de inicio.
# Se limitan estrictamente a 'Run' para evitar listar tareas de servicio (autostart).
REGISTRY_RUN_KEYS: Tuple[str, ...] = (
    r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
)

# Extensiones estándar para filtrar ejecutables sin recurrir a heurísticas complejas.
EXECUTABLE_EXTS: Set[str] = {'.exe', '.bat', '.cmd', '.scr', '.lnk'}

# Caracteres que en rutas de Windows pueden ser interpretados por la shell como comandos (inyección).
SUSPICIOUS_CHARS: str = '<>|?*\0&;%^$'

# Lista de nombres reservados por el kernel que no deben ser tratados como archivos.
RESERVED_DEVICE_NAMES: Set[str] = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1", "COM2", "COM3", "COM4", "LPT2", "LPT3"}

# Caché global para persistir resultados de resolución de rutas en la misma ejecución.
_EXISTS_CACHE: Dict[str, bool] = {}
_COMMAND_CACHE: Dict[str, str] = {}
_FULL_SCAN_CACHE: Optional[StartupEntries] = None

# Mensaje para el usuario: la integridad del registro es responsabilidad de Windows.
HOW_TO_DISABLE: str = (
    "Para deshabilitar un programa de inicio, usá el Administrador de tareas "
    "de Windows (Ctrl+Shift+Esc) → pestaña 'Inicio'. Esta app no modifica el "
    "registro de arranque a propósito por seguridad."
)


@dataclass
class StartupEntry:
    """
    Representa una entrada de inicio detectada. 
    
    Implementa validación de rutas 'lazy' para evitar saturar el I/O en la 
    instanciación, verificando la integridad contra `safety.py`.
    """
    name: str
    command: str
    source: str
    
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    @property
    def is_valid(self) -> bool:
        """Valida que la entrada sea estructuralmente coherente y segura."""
        if not self.command or self._is_path_suspicious(self.command):
            return False
        if self._is_reserved_device_name(self.command):
            return False
        return True

    def _is_reserved_device_name(self, path_str: str) -> bool:
        """Detecta si una ruta apunta a un dispositivo lógico de sistema."""
        try:
            if "\0" in path_str:
                return True
            if path_str.upper().startswith(("\\\\.\\", "\\\\?\\")):
                return True
            return Path(path_str).stem.upper() in RESERVED_DEVICE_NAMES
        except (ValueError, TypeError):
            return True

    def _is_path_suspicious(self, path_string: str) -> bool:
        """Detecta caracteres de inyección o rutas UNC de red no permitidas."""
        return any(c in path_string for c in SUSPICIOUS_CHARS) or path_string.startswith(r"\\")

    def _is_valid_executable(self, path: Path) -> bool:
        """Confirma extensión y verifica ausencia de hijacking mediante symlinks."""
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_command: str) -> str:
        """Limpia caracteres de control (ASCII < 32) de la cadena de comando."""
        if not isinstance(raw_command, str):
            return ""
        return "".join(c for c in raw_command.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_command: str) -> str:
        """
        Extrae rutas encerradas entre comillas aplicando límites de seguridad (MAX_PATH).
        """
        if not isinstance(raw_command, str) or len(raw_command) < 3:
            return ""
        
        end_quote: int = raw_command.find('"', 1)
        if end_quote == -1:
            return ""
            
        path_str: str = raw_command[1:end_quote].strip()
        
        if not path_str or ".." in path_str or self._is_path_suspicious(path_str) or self._is_reserved_device_name(path_str):
            return ""
            
        try:
            if len(path_str) > 260:
                return ""
            p: Path = Path(path_str)
            if not p.parts or is_protected_path(p):
                return ""
            return str(p)
        except (OSError, ValueError, RuntimeError, TypeError, PermissionError):
            return ""

    def _validate_file_access(self, p: Path) -> bool:
        """Verifica la existencia y accesibilidad del ejecutable."""
        try:
            if is_protected_path(p) or p.is_symlink() or (p.exists() and p.is_mount()):
                return False
            return p.stat().is_file()
        except (OSError, PermissionError, FileNotFoundError, AttributeError):
            return False

    def _resolve_and_cache_path(self, path_string: str) -> str:
        """Resuelve la ruta absoluta usando caché para evitar redundancia de disco."""
        if not path_string or not isinstance(path_string, str) or not self.is_valid:
            return ""
        
        try:
            if len(path_string) > 260 or path_string.startswith(r"\\"):
                return ""
            norm: str = os.path.normpath(path_string)
        except (ValueError, TypeError):
            return ""
        
        if path_string in _EXISTS_CACHE:
            return path_string if _EXISTS_CACHE[path_string] else path_string
        
        try:
            p: Path = Path(norm)
            if not p.is_absolute():
                _EXISTS_CACHE[path_string] = False
                return ""
            
            p = p.resolve(strict=False)
            if not self._validate_file_access(p):
                _EXISTS_CACHE[path_string] = False
                return path_string
            
            p_str: str = str(p)
            _EXISTS_CACHE[p_str] = True
            return p_str
        except (OSError, PermissionError, FileNotFoundError, RuntimeError, ValueError, TypeError):
            _EXISTS_CACHE[path_string] = False
            return ""

    def _resolve_path_from_command(self, command_line: str) -> str:
        """Determina la ruta del archivo ejecutable dentro de una línea de comandos."""
        if not command_line or not isinstance(command_line, str):
            return ""
        
        if command_line in _COMMAND_CACHE:
            return _COMMAND_CACHE[command_line]
        
        result: str = ""
        try:
            if command_line.startswith('"'):
                result = self._extract_quoted_path(command_line)
            else:
                parts: List[str] = command_line.split()
                if parts and parts[0]:
                    result = self._resolve_and_cache_path(parts[0])
        except (AttributeError, ValueError, IndexError, OSError, TypeError):
            result = ""
        
        _COMMAND_CACHE[command_line] = result
        return result
        
    @property
    def executable(self) -> str:
        """Devuelve la ruta absoluta del ejecutable tras procesar y validar."""
        if self._checked_exists:
            return self._exec_cache or ""
            
        self._checked_exists = True
        if not self.command:
            return ""

        cmd: str = self._sanitize_command(self.command)
        self._exec_cache = self._resolve_path_from_command(cmd) if cmd else ""
            
        return self._exec_cache or ""


def startup_folders() -> List[Path]:
    """Retorna rutas de carpetas 'Startup' de Windows válidas."""
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
    
    valid_folders: List[Path] = []
    seen_paths: Set[str] = set()
    for c in candidates:
        try:
            if c and c.is_dir():
                resolved_str = str(c.resolve(strict=False))
                if resolved_str not in seen_paths and not c.is_symlink() and not is_protected_path(c):
                    seen_paths.add(resolved_str)
                    valid_folders.append(c)
        except (OSError, RuntimeError, PermissionError):
            continue
    return valid_folders


def _process_folder_entry(entry: os.DirEntry) -> Optional[StartupEntry]:
    """Valida un archivo en carpeta Startup como posible ejecutable."""
    try:
        if not entry.is_file(follow_symlinks=False) or entry.is_symlink():
            return None
        
        _, ext = os.path.splitext(entry.name)
        if ext.lower() not in EXECUTABLE_EXTS:
            return None
            
        if is_protected_path(Path(entry.path)):
            return None
            
        name = os.path.splitext(entry.name)[0]
        return StartupEntry(name=name, command=entry.path, source="carpeta")
    except (OSError, PermissionError, ValueError):
        return None


def entries_from_folders(folders: Optional[Sequence[Path]] = None) -> StartupEntries:
    """Explora carpetas de inicio en busca de ejecutables."""
    found_entries: StartupEntries = []
    scan_folders = folders if folders is not None else startup_folders()
    
    for folder in scan_folders:
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    processed = _process_folder_entry(entry)
                    if processed:
                        found_entries.append(processed)
        except (OSError, PermissionError):
            continue
    return found_entries


def _is_valid_registry_entry(name: str, cmd: str, seen: Set[str]) -> bool:
    """Valida integridad de datos leídos del registro."""
    if not isinstance(name, str) or not isinstance(cmd, str):
        return False
    if not name.strip() or not cmd.strip() or cmd.startswith(r"\\") or cmd in seen:
        return False
    if name.upper().startswith("PS"):
        return False
    
    try:
        clean_path = cmd.strip('"')
        if not clean_path or "\\" not in clean_path and "/" not in clean_path:
            return False
        if any(c in clean_path for c in SUSPICIOUS_CHARS):
            return False
        
        target_path = Path(clean_path)
        if is_protected_path(target_path) or ".." in str(target_path):
            return False
        return True
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


def parse_registry_csv(csv_text: str, source: str = "registro") -> StartupEntries:
    """Convierte salida CSV de PowerShell en objetos StartupEntry."""
    if not isinstance(csv_text, str) or not csv_text.strip():
        return []
        
    parsed_entries: StartupEntries = []
    seen_commands: Set[str] = set()
    
    try:
        f: io.StringIO = io.StringIO(csv_text.strip())
        reader: csv.DictReader = csv.DictReader(f)
        
        if not reader or not reader.fieldnames or len(reader.fieldnames) < 2:
            return []
        
        header_name: str = reader.fieldnames[0]
        header_cmd: str = reader.fieldnames[1]
            
        for row in reader:
            if not isinstance(row, dict):
                continue
            
            raw_val_name = row.get(header_name)
            raw_val_cmd = row.get(header_cmd)
            
            if not isinstance(raw_val_name, str) or not isinstance(raw_val_cmd, str):
                continue
            
            clean_name: str = "".join(c for c in raw_val_name if ord(c) >= 32).strip()
            clean_cmd: str = "".join(c for c in raw_val_cmd if ord(c) >= 32).strip()
            
            if _is_valid_registry_entry(clean_name, clean_cmd, seen_commands):
                seen_commands.add(clean_cmd)
                parsed_entries.append(StartupEntry(name=clean_name, command=clean_cmd, source=source))
            
    except (csv.Error, OSError, ValueError, TypeError, IndexError):
        return []
    return parsed_entries


def entries_from_registry(keys: RegistryKeySet = REGISTRY_RUN_KEYS) -> StartupEntries:
    """Consulta las claves 'Run' mediante una llamada aislada de PowerShell."""
    if os.name != "nt":
        return []
    
    allowed_set = set(REGISTRY_RUN_KEYS)
    safe_keys: List[str] = [k for k in keys if k in allowed_set]
            
    if not safe_keys:
        return []
        
    ps_cmd: str = (
        "Get-ItemProperty -LiteralPath " + ",".join([f"'{k}'" for k in safe_keys]) + 
        " -ErrorAction SilentlyContinue | Select-Object * -ExcludeProperty PS* | ConvertTo-Csv -NoTypeInformation"
    )
    
    try:
        process: subprocess.CompletedProcess = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30, check=False
        )
        if process.returncode == 0 and process.stdout:
            clean_output: str = "".join(c for c in process.stdout if ord(c) >= 32 or c in "\r\n")
            return parse_registry_csv(clean_output)
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def list_startup_entries() -> StartupEntries:
    """Obtiene el inventario consolidado usando caché de sesión."""
    global _FULL_SCAN_CACHE
    if _FULL_SCAN_CACHE is not None:
        return _FULL_SCAN_CACHE

    seen_items: Set[Tuple[str, str]] = set()
    unique_entries: StartupEntries = []
    
    for entry in itertools.chain(entries_from_folders(), entries_from_registry()):
        key = (entry.name.lower(), entry.command.lower())
        if key not in seen_items:
            seen_items.add(key)
            unique_entries.append(entry)
            
    _FULL_SCAN_CACHE = unique_entries
    return unique_entries


def estimate_impact(entries: Sequence[StartupEntry]) -> str:
    """Categoriza el rendimiento esperado según el número de entradas."""
    count: int = len(entries)
    thresholds: List[Tuple[int, str]] = [(5, "ok"), (10, "info"), (18, "warning")]
    for limit, label in thresholds:
        if count <= limit:
            return label
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera la vista de reporte para el usuario."""
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
