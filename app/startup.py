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

Convenciones de parámetros:
  - `path_str`: Cadena cruda proveniente del sistema o registro.
  - `path_obj`: Objeto de tipo pathlib.Path, ya normalizado o validado.
  - `raw_command`: Comando de shell sin procesar, susceptible de inyección.
"""

from __future__ import annotations
import os
import subprocess
import csv
import io
import re
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

# --- Cachés globales: Persisten durante la vida de la app para evitar I/O redundante ---
_EXISTS_CACHE: EntryCache = {}
_COMMAND_CACHE: Dict[str, str] = {}
_RESOLVED_PATH_CACHE: Dict[str, str] = {}
_FULL_SCAN_CACHE: Optional[StartupEntries] = None
_FOLDERS_CACHE: Optional[List[Path]] = None
_VALIDATION_CACHE: EntryCache = {}
_PROCESS_CACHE: EntryCache = {}
_REGISTRY_PATH_CACHE: EntryCache = {}
_EXECUTABLE_VALIDATION_CACHE: Dict[str, bool] = {}
_SUSPICIOUS_CACHE: EntryCache = {}
_EXCLUDED_PATH_CACHE: Set[str] = set()

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
    Incluye lógica de resolución para transformar comandos (a menudo con argumentos)
    en rutas de archivo absolutas y validadas mediante carga diferida.
    """
    __slots__ = ("name", "command", "source", "_exec_cache", "_initialized")
    
    name: str
    command: str
    source: str
    
    def __post_init__(self) -> None:
        self._exec_cache: str = ""
        self._initialized: bool = False

    def _is_path_safe(self, path_str: str) -> bool:
        """Verifica restricciones de longitud y ausencia de caracteres peligrosos."""
        if not path_str or len(path_str) > MAX_PATH_LEN:
            return False
        if any(c in path_str for c in SUSPICIOUS_CHARS):
            return False
        if path_str.upper() in RESERVED_DEVICE_NAMES:
            return False
        return True

    @property
    def is_valid(self) -> bool:
        """Determina si el comando base cumple con los criterios mínimos de seguridad."""
        if not isinstance(self.command, str):
            return False
        cmd_stripped: str = self.command.strip()
        return self._is_path_safe(cmd_stripped)

    def _is_reserved_device_name(self, path_raw: str) -> bool:
        """Detecta si la ruta hace referencia a dispositivos de hardware (ej. NUL, COM1)."""
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
        """Identifica rutas con salto de directorio (..) o rutas UNC potencialmente peligrosas."""
        if path_raw in _SUSPICIOUS_CACHE: return _SUSPICIOUS_CACHE[path_raw]
        
        if not isinstance(path_raw, str) or "\0" in path_raw:
            _SUSPICIOUS_CACHE[path_raw] = True
            return True
        if ".." in path_raw or path_raw.startswith(r"\\"):
            _SUSPICIOUS_CACHE[path_raw] = True
            return True
        result = any(c in path_raw for c in SUSPICIOUS_CHARS)
        _SUSPICIOUS_CACHE[path_raw] = result
        return result

    def _is_file_accessible(self, path: Path) -> bool:
        """Valida existencia, legibilidad y que no sea un enlace simbólico."""
        try:
            return path.exists() and path.is_file() and not path.is_symlink() and os.access(path, os.R_OK)
        except (OSError, PermissionError):
            return False

    def _is_valid_executable(self, path: Path) -> bool:
        """Validación profunda: acceso al archivo, no estar en zonas protegidas y tener extensión válida."""
        p_str: str = str(path)
        if p_str in _EXECUTABLE_VALIDATION_CACHE:
            return _EXECUTABLE_VALIDATION_CACHE[p_str]
        if p_str in _EXCLUDED_PATH_CACHE:
            return False

        try:
            if not self._is_file_accessible(path):
                _EXECUTABLE_VALIDATION_CACHE[p_str] = False
                return False
            
            if is_protected_path(path):
                _EXCLUDED_PATH_CACHE.add(p_str)
                _EXECUTABLE_VALIDATION_CACHE[p_str] = False
                return False
                
            resolved: Path = path.resolve(strict=False)
            if ".." in str(resolved) or not resolved.is_file() or resolved.suffix.lower() not in EXECUTABLE_EXTS:
                _EXECUTABLE_VALIDATION_CACHE[p_str] = False
                return False
            
            if resolved.parts and resolved.parts[0].startswith(r"\\"):
                _EXECUTABLE_VALIDATION_CACHE[p_str] = False
                return False
                
            _RESOLVED_PATH_CACHE[p_str] = str(resolved)
            _EXECUTABLE_VALIDATION_CACHE[p_str] = True
            return True
        except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
            _EXECUTABLE_VALIDATION_CACHE[p_str] = False
            return False

    def _sanitize_command(self, raw_command: str) -> str:
        """Filtra caracteres de control invisibles en la cadena de comando."""
        if not isinstance(raw_command, str):
            return ""
        return "".join(c for c in raw_command.strip() if ord(c) >= MIN_ASCII_VAL)

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """Extrae la ruta de un ejecutable delimitado por comillas evitando inyecciones."""
        if not isinstance(raw_cmd, str) or len(raw_cmd) < 3 or not raw_cmd.startswith('"'):
            return ""
        
        try:
            if any(ord(c) < MIN_ASCII_VAL for c in raw_cmd):
                return ""
                
            end_quote: int = raw_cmd.find('"', 1)
            if end_quote == -1:
                return ""
                
            path_str: str = raw_cmd[1:end_quote].strip()
            if not path_str or self._is_path_suspicious(path_str):
                return ""
            if self._is_reserved_device_name(path_str) or len(path_str) > MAX_PATH_LEN:
                return ""
                
            path_obj = Path(path_str)
            if not path_obj.name or path_obj.parent == path_obj:
                return ""
            
            if path_str in _EXCLUDED_PATH_CACHE or is_protected_path(path_obj):
                _EXCLUDED_PATH_CACHE.add(path_str)
                return ""

            if not path_obj.parts or path_obj.is_symlink():
                return ""
            return str(path_obj)
        except (OSError, ValueError, RuntimeError, TypeError, PermissionError, AttributeError):
            return ""

    def _validate_file_access(self, p: Path) -> bool:
        """Valida acceso al archivo verificando protección de sistema y caché de seguridad."""
        p_str: str = str(p)
        if p_str in _VALIDATION_CACHE:
            return _VALIDATION_CACHE[p_str]
        if p_str in _EXCLUDED_PATH_CACHE:
            return False
            
        try:
            if is_protected_path(p):
                _EXCLUDED_PATH_CACHE.add(p_str)
                return False
            is_valid = (
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

    def _check_structural_integrity(self, p: Path) -> bool:
        """Verifica que la estructura de la ruta no sea recursiva o exceda la jerarquía permitida."""
        try:
            p_str = str(p)
            return not (
                ".." in p_str or 
                len(p.parts) > MAX_PATH_PARTS or 
                p.is_symlink() or
                p_str.startswith(r"\\")
            )
        except (ValueError, AttributeError):
            return False

    def _resolve_and_cache_path(self, path_raw: str) -> str:
        """Normaliza y resuelve rutas absolutas, filtrando enlaces simbólicos o rutas inválidas."""
        if not isinstance(path_raw, str) or not path_raw or "\0" in path_raw or self._is_path_suspicious(path_raw):
            return ""
        
        if len(path_raw) > MAX_PATH_LEN or any(ord(c) < MIN_ASCII_VAL for c in path_raw):
            return ""
            
        try:
            norm: str = os.path.normpath(path_raw)
            if len(norm) > MAX_PATH_LEN: return ""
            p: Path = Path(norm)
            
            if not p.is_absolute() or not p.anchor or not self._check_structural_integrity(p):
                _EXISTS_CACHE[path_raw] = False
                return ""
            
            resolved: Path = p.resolve(strict=False)
            if not resolved or not self._check_structural_integrity(resolved):
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
        """Divide la línea de comando para identificar y resolver el ejecutable principal."""
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
        """Carga diferida del ejecutable resuelto para mejorar el rendimiento inicial."""
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
        """Retorna la ruta resuelta del ejecutable."""
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
    """Valida un archivo dentro de una carpeta de inicio y retorna la instancia si es seguro."""
    if entry is None or entry.path in _PROCESS_CACHE or entry.path in _EXCLUDED_PATH_CACHE: return None
        
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
            _EXCLUDED_PATH_CACHE.add(entry.path)
            _PROCESS_CACHE[entry.path] = False
            return None
            
        _PROCESS_CACHE[entry.path] = True
        return StartupEntry(name=path_obj.stem, command=entry.path, source="carpeta")
    except (OSError, PermissionError, ValueError, TypeError, AttributeError):
        _PROCESS_CACHE[entry.path] = False
        return None


def entries_from_folders(folders: Optional[Iterable[Path]] = None) -> StartupEntries:
    """Escanea carpetas de inicio detectando ejecutables bajo criterios de seguridad."""
    scan_folders = folders if folders is not None else startup_folders()
    processed_files = _EXCLUDED_PATH_CACHE.copy()
    results: StartupEntries = []
    for folder in scan_folders:
        if is_protected_path(folder): continue
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    path_key = entry.path.lower()
                    if path_key in processed_files: continue
                    try:
                        processed = _process_folder_entry(entry)
                        if processed:
                            processed_files.add(path_key)
                            results.append(processed)
                        else:
                            processed_files.add(path_key)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return results


def _is_path_syntax_safe(path_str: str) -> bool:
    """Valida sintaxis básica contra inyecciones de shell."""
    if not isinstance(path_str, str): return False
    clean_path: str = path_str.strip(' "')
    if not clean_path or "\0" in clean_path or ("\\" not in clean_path and "/" not in clean_path):
        return False
    if len(clean_path) > MAX_PATH_LEN or ".." in clean_path or any(c in clean_path for c in SUSPICIOUS_CHARS):
        return False
    return True


def _is_valid_registry_path(path_str: str) -> bool:
    """Valida integridad de una ruta de registro; asegura que el destino es ejecutable."""
    if not _is_path_syntax_safe(path_str): return False
    if path_str in _REGISTRY_PATH_CACHE: return _REGISTRY_PATH_CACHE[path_str]
    if path_str in _EXCLUDED_PATH_CACHE: return False
        
    try:
        clean_str: str = path_str.strip(' "')
        if clean_str.startswith(r"\\"):
            _REGISTRY_PATH_CACHE[path_str] = False
            return False
            
        target: Path = Path(clean_str).expanduser()
        if target.stem.upper() in RESERVED_DEVICE_NAMES:
            _REGISTRY_PATH_CACHE[path_str] = False
            return False
        
        target_res: Path = target.resolve(strict=False)
        if is_protected_path(target_res) or target_res.is_symlink():
            _EXCLUDED_PATH_CACHE.add(path_str)
            _REGISTRY_PATH_CACHE[path_str] = False
            return False
        
        _REGISTRY_PATH_CACHE[path_str] = True
        return True
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError, PermissionError):
        _REGISTRY_PATH_CACHE[path_str] = False
        return False


def _is_registry_entry_safe(name: str, command: str, seen: Set[str]) -> bool:
    """Verifica unicidad y validez de un par nombre/comando del registro."""
    if not isinstance(name, str) or not isinstance(command, str): return False
    if len(command) > MAX_PATH_LEN or any(ord(c) < MIN_ASCII_VAL for c in command) or "\0" in command: return False
        
    clean_name, clean_cmd = name.strip(), command.strip()
    if not clean_name or not clean_cmd or clean_cmd in seen: return False
    
    return _is_valid_registry_path(clean_cmd)


def parse_registry_csv(csv_text: str, source: str = "registro") -> StartupEntries:
    """Convierte salida CSV de PowerShell en objetos `StartupEntry`."""
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
    """Lee claves de inicio del registro mediante PowerShell encapsulado."""
    if os.name != "nt": return []
    
    registry_pattern = re.compile(r"^[a-zA-Z0-9:\\_]+$")
    valid_keys = [k for k in keys if registry_pattern.match(k)]
    
    if not valid_keys: return []
    
    keys_formatted: str = ", ".join([f"'{k}'" for k in valid_keys])
    ps_script: str = f"""
    $ErrorActionPreference = 'Stop'
    try {{
        foreach ($path in @({keys_formatted})) {{
            $item = Get-ItemProperty -LiteralPath $path -ErrorAction SilentlyContinue
            if ($item) {{
                $item.PSObject.Properties | Where-Object {{ 
                    $_.MemberType -eq 'NoteProperty' -and 
                    $_.Name -notmatch '^PS' -and 
                    $null -ne $_.Value 
                }} | Select-Object @{{Name='Name';Expression={{$_.Name}}}}, @{{Name='Value';Expression={{[string]$_.Value}}}}
            }}
        }} | ConvertTo-Csv -NoTypeInformation
    }} catch {{ exit 1 }}
    """
    
    try:
        process: subprocess.CompletedProcess = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=10, check=False
        )
        if process.returncode == 0 and process.stdout and len(process.stdout.strip()) > 0:
            return parse_registry_csv(process.stdout)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError, ValueError, UnicodeDecodeError):
        return []
    return []


def list_startup_entries() -> StartupEntries:
    """Retorna una lista consolidada y sin duplicados de todas las entradas de inicio."""
    global _FULL_SCAN_CACHE
    if _FULL_SCAN_CACHE is not None: return _FULL_SCAN_CACHE

    results = entries_from_folders() + entries_from_registry()
    unique_map: Dict[str, StartupEntry] = {e.command: e for e in results}
    
    _FULL_SCAN_CACHE = list(unique_map.values())
    return _FULL_SCAN_CACHE


def estimate_impact(entries: Iterable[StartupEntry]) -> str:
    """Clasifica el impacto en el rendimiento basado en la cantidad de programas."""
    count: int = len(entries) if isinstance(entries, list) else sum(1 for _ in entries)
    if count <= 5: return "ok"
    if count <= 10: return "info"
    if count <= 18: return "warning"
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera un informe textual legible detallando los programas encontrados."""
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
