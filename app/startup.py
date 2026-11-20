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
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Iterable, Optional, Iterator, List, Tuple, Dict, Set, TypeAlias, Sequence
)
from safety import is_protected_path, is_safe_to_modify

# Type aliases para mejorar la legibilidad de las firmas de funciones
StartupEntries: TypeAlias = List["StartupEntry"]
RegistryKeySet: TypeAlias = Iterable[str]
EntryCache: TypeAlias = Dict[str, bool]

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
REGISTRY_RUN_KEYS: Tuple[str, ...] = (
    r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
)

# Extensiones de archivo que representan candidatos probables de ejecución automática.
EXECUTABLE_EXTS: Set[str] = {'.exe', '.bat', '.cmd', '.scr', '.lnk'}

# Caracteres que en rutas de Windows pueden ser interpretados por la shell como comandos (inyección).
SUSPICIOUS_CHARS: str = '<>|?*\0&;%^$'

# Nombres reservados por el kernel que deben ser excluidos para evitar errores de acceso a dispositivos.
RESERVED_DEVICE_NAMES: Set[str] = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1", "COM2", "COM3", "COM4", "LPT2", "LPT3"}

# Límites operativos para la validación de rutas y estructuras.
MAX_PATH_LEN: int = 260
MAX_PATH_PARTS: int = 64
MIN_ASCII_VAL: int = 32

# --- Cachés globales de estado para optimizar operaciones de I/O ---
_EXISTS_CACHE: EntryCache = {}
_COMMAND_CACHE: Dict[str, str] = {}
_RESOLVED_PATH_CACHE: Dict[str, str] = {}
_FULL_SCAN_CACHE: Optional[StartupEntries] = None
_FOLDERS_CACHE: Optional[List[Path]] = None
_VALIDATION_CACHE: EntryCache = {}
_PROCESS_CACHE: EntryCache = {}
_REGISTRY_PATH_CACHE: EntryCache = {}
_EXECUTABLE_VALIDATION_CACHE: Dict[str, bool] = {}

# Mensaje orientativo para el usuario final sobre la gestión de persistencia.
HOW_TO_DISABLE: str = (
    "Para deshabilitar un programa de inicio, usá el Administrador de tareas "
    "de Windows (Ctrl+Shift+Esc) → pestaña 'Inicio'. Esta app no modifica el "
    "registro de arranque a propósito por seguridad."
)


@dataclass
class StartupEntry:
    """
    Representa una entrada de inicio recuperada del sistema o registro.

    Attributes:
        name: Nombre descriptivo de la aplicación o tarea.
        command: Línea de comando original tal cual fue leída.
        source: Origen del dato ('carpeta' o 'registro').
    """
    name: str
    command: str
    source: str
    
    _exec_cache: str = field(default="", init=False)
    _initialized: bool = field(default=False, init=False)

    def _is_path_safe(self, path_str: str) -> bool:
        """Valida que la cadena de ruta cumpla límites básicos de longitud y seguridad."""
        if not path_str or len(path_str) > MAX_PATH_LEN:
            return False
        if any(c in path_str for c in SUSPICIOUS_CHARS):
            return False
        if path_str.upper() in RESERVED_DEVICE_NAMES:
            return False
        return True

    @property
    def is_valid(self) -> bool:
        """Valida si la estructura del comando es segura y no excede límites de longitud."""
        if not isinstance(self.command, str):
            return False
        cmd_stripped: str = self.command.strip()
        return self._is_path_safe(cmd_stripped)

    def _is_reserved_device_name(self, path_raw: str) -> bool:
        """Verifica si la cadena refiere a un nombre de dispositivo reservado del kernel."""
        if not isinstance(path_raw, str) or "\0" in path_raw:
            return True
        try:
            path_upper: str = path_raw.upper()
            if path_upper.startswith(("\\\\.\\", "\\\\?\\")):
                return True
            p: Path = Path(path_raw)
            return p.stem.upper() in RESERVED_DEVICE_NAMES
        except (ValueError, TypeError, AttributeError, OSError):
            return True

    def _is_path_suspicious(self, path_raw: str) -> bool:
        """Detecta patrones de inyección o rutas que escapan del contexto local (.. o UNC)."""
        if not isinstance(path_raw, str) or "\0" in path_raw:
            return True
        if ".." in path_raw or path_raw.startswith(r"\\"):
            return True
        return any(c in path_raw for c in SUSPICIOUS_CHARS)

    def _is_file_accessible(self, path: Path) -> bool:
        """Comprueba si el archivo existe y es legible mediante permisos estándar del SO."""
        try:
            return path.exists() and path.is_file() and not path.is_symlink() and os.access(path, os.R_OK)
        except (OSError, PermissionError):
            return False

    def _is_valid_executable(self, path: Path) -> bool:
        """
        Realiza una validación exhaustiva de un ejecutable: comprueba existencia,
        accesibilidad, protección del sistema y extensión válida.
        """
        p_str: str = str(path)
        if p_str in _EXECUTABLE_VALIDATION_CACHE:
            return _EXECUTABLE_VALIDATION_CACHE[p_str]

        try:
            if not self._is_file_accessible(path):
                _EXECUTABLE_VALIDATION_CACHE[p_str] = False
                return False
            
            if is_protected_path(path):
                _EXECUTABLE_VALIDATION_CACHE[p_str] = False
                return False
                
            resolved: Path = path.resolve(strict=False)
            if ".." in str(resolved) or not resolved.is_file() or resolved.suffix.lower() not in EXECUTABLE_EXTS:
                _EXECUTABLE_VALIDATION_CACHE[p_str] = False
                return False
                
            _RESOLVED_PATH_CACHE[p_str] = str(resolved)
            _EXECUTABLE_VALIDATION_CACHE[p_str] = True
            return True
        except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
            _EXECUTABLE_VALIDATION_CACHE[p_str] = False
            return False

    def _sanitize_command(self, raw_command: str) -> str:
        """Limpia caracteres de control no imprimibles (ASCII < 32) de la línea de comando."""
        if not isinstance(raw_command, str):
            return ""
        return "".join(c for c in raw_command.strip() if ord(c) >= MIN_ASCII_VAL)

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """Extrae la ruta contenida entre comillas dobles y valida que sea segura."""
        if not isinstance(raw_cmd, str) or len(raw_cmd) < 3:
            return ""
        
        end_quote: int = raw_cmd.find('"', 1)
        if end_quote == -1:
            return ""
            
        path_str: str = raw_cmd[1:end_quote].strip()
        if not path_str or self._is_path_suspicious(path_str):
            return ""
        if self._is_reserved_device_name(path_str) or len(path_str) > MAX_PATH_LEN:
            return ""
            
        if is_protected_path(Path(path_str)):
            return ""

        try:
            # Validación de integridad antes de instanciar Path
            if any(ord(c) < MIN_ASCII_VAL for c in path_str):
                return ""
            p: Path = Path(path_str)
            if not p.parts or p.is_symlink():
                return ""
            return str(p)
        except (OSError, ValueError, RuntimeError, TypeError, PermissionError, AttributeError):
            return ""

    def _validate_file_access(self, p: Path) -> bool:
        """Verifica el acceso al archivo mediante `safety.py` y caché local."""
        p_str: str = str(p)
        if p_str in _VALIDATION_CACHE:
            return _VALIDATION_CACHE[p_str]
            
        try:
            is_valid = (
                not is_protected_path(p) and
                is_safe_to_modify(p) and
                not p.is_symlink() and
                os.access(p, os.R_OK) and
                self._is_valid_executable(p)
            )
            _VALIDATION_CACHE[p_str] = is_valid
            return is_valid
        except (OSError, PermissionError, RuntimeError):
            _VALIDATION_CACHE[p_str] = False
            return False

    def _resolve_and_cache_path(self, path_raw: str) -> str:
        """Normaliza, resuelve enlaces y valida rutas contra la política de seguridad."""
        if not isinstance(path_raw, str) or not path_raw or "\0" in path_raw or self._is_path_suspicious(path_raw):
            return ""
        
        if len(path_raw) > MAX_PATH_LEN or any(ord(c) < MIN_ASCII_VAL for c in path_raw):
            return ""
            
        try:
            norm: str = os.path.normpath(path_raw)
            p: Path = Path(norm)
            
            if not p.is_absolute() or ".." in p.parts or not p.anchor or p.is_symlink():
                _EXISTS_CACHE[path_raw] = False
                return ""
            
            resolved: Path = p.resolve(strict=False)
            if not resolved or ".." in str(resolved) or len(resolved.parts) > MAX_PATH_PARTS or resolved.is_symlink():
                return ""

            if not self._validate_file_access(resolved):
                _EXISTS_CACHE[path_raw] = False
                return path_raw
            
            _RESOLVED_PATH_CACHE[norm] = str(resolved)
            _EXISTS_CACHE[str(resolved)] = True
            return str(resolved)
        except (OSError, PermissionError, FileNotFoundError, RuntimeError, ValueError, TypeError, AttributeError):
            _EXISTS_CACHE[path_raw] = False
            return ""

    def _resolve_path_from_command(self, cmd_line: str) -> str:
        """Aísla y resuelve el ejecutable de una línea de comando compleja."""
        if not isinstance(cmd_line, str) or not cmd_line:
            return ""
        
        if cmd_line in _COMMAND_CACHE:
            return _COMMAND_CACHE[cmd_line]
        
        result: str = ""
        try:
            if cmd_line.startswith('"'):
                result = self._extract_quoted_path(cmd_line)
            else:
                parts: List[str] = cmd_line.split()
                if parts and ("\\" in parts[0] or "/" in parts[0]):
                    result = self._resolve_and_cache_path(parts[0])
        except (AttributeError, ValueError, IndexError, OSError, TypeError):
            result = ""
        
        _COMMAND_CACHE[cmd_line] = result
        return result
    
    def _initialize_executable(self) -> None:
        """Ejecuta la resolución diferida del ejecutable manejando fallos de acceso."""
        try:
            if isinstance(self.command, str):
                cmd: str = self._sanitize_command(self.command)
                self._exec_cache = self._resolve_path_from_command(cmd) if cmd else ""
            else:
                self._exec_cache = ""
        except (OSError, PermissionError, RuntimeError):
            self._exec_cache = ""
        finally:
            self._initialized = True
        
    @property
    def executable(self) -> str:
        """Retorna la ruta absoluta del ejecutable si es válida y accesible."""
        if not self._initialized:
            self._initialize_executable()
        return self._exec_cache


def startup_folders() -> List[Path]:
    """Retorna una lista de rutas canónicas de carpetas 'Inicio' validadas."""
    global _FOLDERS_CACHE
    if _FOLDERS_CACHE is not None:
        return _FOLDERS_CACHE

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
            if c and c.is_dir() and not c.is_symlink():
                resolved_str: str = os.path.normpath(str(c))
                if resolved_str not in seen_paths and not is_protected_path(c):
                    seen_paths.add(resolved_str)
                    valid_folders.append(c)
        except (OSError, RuntimeError, PermissionError):
            continue
    _FOLDERS_CACHE = valid_folders
    return valid_folders


def _process_folder_entry(entry: os.DirEntry) -> Optional[StartupEntry]:
    """Valida un archivo dentro de una carpeta de inicio y retorna la instancia."""
    if entry is None or entry.path in _PROCESS_CACHE: return None
        
    try:
        if not entry.is_file() or entry.is_symlink() or "\0" in entry.path:
            _PROCESS_CACHE[entry.path] = False
            return None

        _, ext = os.path.splitext(entry.name)
        if not ext or ext.lower() not in EXECUTABLE_EXTS:
            _PROCESS_CACHE[entry.path] = False
            return None
            
        path_obj: Path = Path(entry.path)
        if is_protected_path(path_obj) or not is_safe_to_modify(path_obj):
            _PROCESS_CACHE[entry.path] = False
            return None
            
        _PROCESS_CACHE[entry.path] = True
        return StartupEntry(name=path_obj.stem, command=entry.path, source="carpeta")
    except (OSError, PermissionError, ValueError, TypeError, AttributeError):
        return None


def entries_from_folders(folders: Optional[Iterable[Path]] = None) -> StartupEntries:
    """Escanea carpetas de inicio detectando ejecutables bajo criterios de seguridad."""
    scan_folders = folders if folders is not None else startup_folders()
    processed_files: Set[str] = set()
    results = []
    for folder in scan_folders:
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        path_key = entry.path.lower()
                        if path_key in processed_files: continue
                        
                        processed = _process_folder_entry(entry)
                        if processed:
                            processed_files.add(path_key)
                            results.append(processed)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return results


def _is_path_syntax_safe(path_str: str) -> bool:
    """Valida sintaxis de ruta contra inyecciones de shell."""
    if not isinstance(path_str, str): return False
    clean_path: str = path_str.strip(' "')
    if not clean_path or "\0" in clean_path or ("\\" not in clean_path and "/" not in clean_path):
        return False
    if ".." in clean_path or clean_path.startswith(r"\\") or any(c in clean_path for c in SUSPICIOUS_CHARS):
        return False
    return True


def _is_valid_registry_path(path_str: str) -> bool:
    """Valida la integridad de una ruta de registro; asegura destino ejecutable seguro."""
    if not _is_path_syntax_safe(path_str): return False
    if path_str in _REGISTRY_PATH_CACHE: return _REGISTRY_PATH_CACHE[path_str]
        
    try:
        clean_str: str = path_str.strip(' "')
        if len(clean_str) > MAX_PATH_LEN or "\0" in clean_str:
            _REGISTRY_PATH_CACHE[path_str] = False
            return False
            
        target: Path = Path(clean_str).expanduser()
        if target.stem.upper() in RESERVED_DEVICE_NAMES or target.is_symlink() or is_protected_path(target):
            _REGISTRY_PATH_CACHE[path_str] = False
            return False
        
        try:
            target_res: Path = target.resolve(strict=False)
        except (OSError, RuntimeError):
            target_res = target

        if not target_res or ".." in str(target_res) or target_res.is_symlink() or is_protected_path(target_res):
            _REGISTRY_PATH_CACHE[path_str] = False
            return False
        
        _REGISTRY_PATH_CACHE[path_str] = True
        return True
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError, PermissionError):
        _REGISTRY_PATH_CACHE[path_str] = False
        return False


def _is_registry_entry_safe(name: str, command: str, seen: Set[str]) -> bool:
    """Verifica unicidad y validez de un par nombre/comando de registro."""
    if not isinstance(name, str) or not isinstance(command, str): return False
    if len(command) > MAX_PATH_LEN or any(ord(c) < MIN_ASCII_VAL for c in command) or "\0" in command: return False
        
    clean_name, clean_cmd = name.strip(), command.strip()
    if not clean_name or not clean_cmd or clean_cmd in seen: return False
    
    return _is_valid_registry_path(clean_cmd)


def parse_registry_csv(csv_text: str, source: str = "registro") -> StartupEntries:
    """Transforma la salida CSV de PowerShell en una lista de objetos `StartupEntry`."""
    if not isinstance(csv_text, str) or not csv_text.strip(): return []
        
    parsed_entries: StartupEntries = []
    seen_commands: Set[str] = set()
    
    try:
        f: io.StringIO = io.StringIO(csv_text.strip())
        reader: csv.DictReader = csv.DictReader(f)
        
        if not reader.fieldnames or not {"Name", "Value"}.issubset(set(reader.fieldnames)): return []
        
        for row in reader:
            if not isinstance(row, dict): continue
            
            try:
                raw_name = row.get("Name")
                raw_cmd = row.get("Value")
                
                if not isinstance(raw_name, str) or not isinstance(raw_cmd, str):
                    continue
                
                clean_name = raw_name.strip()
                clean_cmd = raw_cmd.strip()
                
                if not clean_name or not clean_cmd:
                    continue
                
                if _is_registry_entry_safe(clean_name, clean_cmd, seen_commands):
                    seen_commands.add(clean_cmd)
                    parsed_entries.append(StartupEntry(name=clean_name, command=clean_cmd, source=source))
            except (ValueError, TypeError, AttributeError):
                continue
            
    except (csv.Error, ValueError, TypeError, AttributeError, OSError):
        return []
    return parsed_entries


def entries_from_registry(keys: RegistryKeySet = REGISTRY_RUN_KEYS) -> StartupEntries:
    """Lee claves de inicio del registro mediante un script seguro de PowerShell."""
    if os.name != "nt": return []
    
    keys_formatted: str = ", ".join([f"'{k}'" for k in keys])
    ps_script: str = f"""
    $ErrorActionPreference = 'SilentlyContinue'
    foreach ($path in @({keys_formatted})) {{
        if (-not ($path -match '^[a-zA-Z0-9:\\\\_]+$')) {{ continue }}
        $item = Get-ItemProperty -LiteralPath $path -ErrorAction SilentlyContinue
        if ($item) {{
            $item.PSObject.Properties | Where-Object {{ $_.MemberType -eq 'NoteProperty' -and $_.Name -notmatch '^PS' }} | 
            Select-Object @{{Name='Name';Expression={{$_.Name}}}}, @{{Name='Value';Expression={{$_.Value}}}}
        }}
    }} | ConvertTo-Csv -NoTypeInformation
    """
    
    try:
        process: subprocess.CompletedProcess = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=10, check=False
        )
        if process.returncode == 0 and process.stdout:
            return parse_registry_csv(process.stdout)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError, ValueError, UnicodeDecodeError):
        return []
    return []


def list_startup_entries() -> StartupEntries:
    """Consolida entradas de carpetas y registro con validación de unicidad."""
    global _FULL_SCAN_CACHE
    if _FULL_SCAN_CACHE is not None: return _FULL_SCAN_CACHE

    results: StartupEntries = entries_from_folders() + entries_from_registry()
    unique_entries: StartupEntries = []
    seen_commands: Set[str] = set()

    for entry in results:
        if entry.command not in seen_commands:
            seen_commands.add(entry.command)
            unique_entries.append(entry)
    
    _FULL_SCAN_CACHE = unique_entries
    return _FULL_SCAN_CACHE


def estimate_impact(entries: Iterable[StartupEntry]) -> str:
    """Estima degradación del rendimiento de arranque según cantidad de entradas."""
    count: int = len(entries) if isinstance(entries, list) else sum(1 for _ in entries)
    if count <= 5: return "ok"
    if count <= 10: return "info"
    if count <= 18: return "warning"
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera un reporte legible sobre los programas configurados al inicio."""
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
