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

# Mensaje orientativo para el usuario final sobre la gestión de persistencia.
HOW_TO_DISABLE: str = (
    "Para deshabilitar un programa de inicio, usá el Administrador de tareas "
    "de Windows (Ctrl+Shift+Esc) → pestaña 'Inicio'. Esta app no modifica el "
    "registro de arranque a propósito por seguridad."
)


@dataclass
class StartupEntry:
    """
    Representa una entrada de inicio (archivo o clave de registro).
    
    Attributes:
        name: Nombre descriptivo de la entrada.
        command: Línea de comando original que ejecuta el programa.
        source: Origen de la entrada (ej. "carpeta", "registro").
    """
    name: str
    command: str
    source: str
    
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    @property
    def is_valid(self) -> bool:
        """Verifica la integridad básica del comando, longitud y caracteres prohibidos."""
        if not isinstance(self.command, str):
            return False
        cmd_stripped: str = self.command.strip()
        if not cmd_stripped:
            return False
        if len(cmd_stripped) > MAX_PATH_LEN:
            return False
        if self._is_path_suspicious(cmd_stripped) or self._is_reserved_device_name(cmd_stripped):
            return False
        return True

    def _is_reserved_device_name(self, path_raw: str) -> bool:
        """Determina si la cadena de ruta hace referencia a dispositivos reservados de sistema."""
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
        """Detecta presencia de caracteres de inyección o rutas UNC de red (riesgosas)."""
        if not isinstance(path_raw, str) or "\0" in path_raw:
            return True
        return any(c in path_raw for c in SUSPICIOUS_CHARS) or path_raw.startswith(r"\\")

    def _is_valid_executable(self, path: Path) -> bool:
        """
        Valida que la ruta sea un ejecutable existente, no un symlink y legible.
        """
        try:
            p_str = str(path)
            if p_str in _RESOLVED_PATH_CACHE:
                return True
            
            if not path.exists() or path.is_symlink() or not os.access(path, os.R_OK):
                return False
                
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                return False
                
            if resolved.suffix.lower() not in EXECUTABLE_EXTS:
                return False
                
            _RESOLVED_PATH_CACHE[p_str] = str(resolved)
            return True
        except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_command: str) -> str:
        """Filtra caracteres de control y no imprimibles de la línea de comando."""
        if not isinstance(raw_command, str):
            return ""
        return "".join(c for c in raw_command.strip() if ord(c) >= MIN_ASCII_VAL)

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """
        Extrae una ruta de archivo encerrada entre comillas y valida su formato.
        
        Args:
            raw_cmd: Cadena de comando cruda (ej: '"C:\Apps\App.exe" /s').
        """
        if not isinstance(raw_cmd, str) or len(raw_cmd) < 3:
            return ""
        
        end_quote: int = raw_cmd.find('"', 1)
        if end_quote == -1:
            return ""
            
        path_str: str = raw_cmd[1:end_quote].strip()
        
        if not path_str or ".." in path_str or self._is_path_suspicious(path_str) or self._is_reserved_device_name(path_str) or len(path_str) > MAX_PATH_LEN:
            return ""
            
        try:
            p: Path = Path(path_str)
            if not p.parts or is_protected_path(p) or p.is_symlink():
                return ""
            return str(p)
        except (OSError, ValueError, RuntimeError, TypeError, PermissionError, AttributeError):
            return ""

    def _validate_file_access(self, p: Path) -> bool:
        """
        Verifica permisos de acceso, estado de protección y validez del ejecutable mediante caché.
        
        Args:
            p: Objeto Path del archivo a validar.
        """
        p_str: str = str(p)
        if p_str in _VALIDATION_CACHE:
            return _VALIDATION_CACHE[p_str]
            
        try:
            if is_protected_path(p) or not is_safe_to_modify(p) or p.is_symlink():
                _VALIDATION_CACHE[p_str] = False
                return False
            if not os.access(p, os.R_OK):
                _VALIDATION_CACHE[p_str] = False
                return False
            if not self._is_valid_executable(p):
                _VALIDATION_CACHE[p_str] = False
                return False
                
            _VALIDATION_CACHE[p_str] = True
            return True
        except (OSError, PermissionError, RuntimeError):
            _VALIDATION_CACHE[p_str] = False
            return False

    def _resolve_and_cache_path(self, path_raw: str) -> str:
        """
        Normaliza una ruta cruda, resuelve el path absoluto y valida su existencia.
        
        Args:
            path_raw: La cadena de texto de la ruta de archivo a procesar.
        """
        if not isinstance(path_raw, str) or not path_raw or "\0" in path_raw or path_raw.startswith(r"\\"):
            return ""
        
        if len(path_raw) > MAX_PATH_LEN or any(ord(c) < MIN_ASCII_VAL for c in path_raw):
            return ""
            
        try:
            norm: str = os.path.normpath(path_raw)
            if path_raw in _EXISTS_CACHE:
                return path_raw if _EXISTS_CACHE[path_raw] else path_raw
            
            p: Path = Path(norm)
            if not p.is_absolute() or ".." in p.parts or not p.anchor or p.is_symlink():
                _EXISTS_CACHE[path_raw] = False
                return ""
            
            if norm in _RESOLVED_PATH_CACHE:
                resolved = Path(_RESOLVED_PATH_CACHE[norm])
            else:
                resolved = p.resolve(strict=False)
                _RESOLVED_PATH_CACHE[norm] = str(resolved)
            
            if not resolved or len(resolved.parts) > MAX_PATH_PARTS or resolved.is_symlink():
                return ""

            if not self._validate_file_access(resolved):
                _EXISTS_CACHE[path_raw] = False
                return path_raw
            
            p_str: str = str(resolved)
            _EXISTS_CACHE[p_str] = True
            return p_str
        except (OSError, PermissionError, FileNotFoundError, RuntimeError, ValueError, TypeError, AttributeError):
            _EXISTS_CACHE[path_raw] = False
            return ""

    def _resolve_path_from_command(self, cmd_line: str) -> str:
        """Analiza la línea de comando para aislar y resolver la ruta del ejecutable."""
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
                if parts and isinstance(parts[0], str) and ( "\\" in parts[0] or "/" in parts[0] ):
                    result = self._resolve_and_cache_path(parts[0])
        except (AttributeError, ValueError, IndexError, OSError, TypeError):
            result = ""
        
        _COMMAND_CACHE[cmd_line] = result
        return result
        
    @property
    def executable(self) -> str:
        """Retorna la ruta absoluta del ejecutable si es válida y accesible."""
        if self._checked_exists:
            return self._exec_cache or ""
            
        self._checked_exists = True
        if not self.command or not isinstance(self.command, str):
            return ""

        cmd: str = self._sanitize_command(self.command)
        self._exec_cache = self._resolve_path_from_command(cmd) if cmd else ""
            
        return self._exec_cache or ""


def startup_folders() -> List[Path]:
    """
    Retorna una lista de objetos Path correspondientes a las carpetas de inicio.
    
    Verifica la existencia, accesibilidad y si la ruta es un símbolo de enlace
    (prohibido por seguridad) antes de incluirlas en la lista.
    """
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
    """
    Evalúa un archivo dentro de una carpeta de inicio para crear un objeto StartupEntry.
    
    Args:
        entry: Objeto DirEntry que representa el archivo del sistema a evaluar.
        
    Returns:
        Un objeto StartupEntry si es un ejecutable válido, o None en caso contrario.
    """
    if not hasattr(entry, 'path'): return None
    if entry.path in _PROCESS_CACHE:
        return None
        
    try:
        # Usamos try-except interno para saltar bloqueos de acceso durante la introspección
        if not entry.is_file() or entry.is_symlink() or "\0" in entry.path:
            return None

        _, ext = os.path.splitext(entry.name)
        if not ext or ext.lower() not in EXECUTABLE_EXTS or len(entry.path) > MAX_PATH_LEN:
            return None
            
        path_obj: Path = Path(entry.path)
        if is_protected_path(path_obj) or not is_safe_to_modify(path_obj):
            _PROCESS_CACHE[entry.path] = False
            return None
            
        _PROCESS_CACHE[entry.path] = True
        return StartupEntry(name=path_obj.stem, command=entry.path, source="carpeta")
    except (OSError, PermissionError, ValueError, TypeError, AttributeError):
        _PROCESS_CACHE[entry.path] = False
        return None


def entries_from_folders(folders: Optional[Iterable[Path]] = None) -> StartupEntries:
    """
    Escanea las carpetas de inicio proporcionadas y retorna una lista de StartupEntry detectados.
    
    Args:
        folders: Iterable de objetos Path a escanear. Si es None, usa startup_folders().
    """
    found_entries: StartupEntries = []
    scan_folders: Iterable[Path] = folders if folders is not None else startup_folders()
    processed_paths: Set[str] = set()
    
    for folder in scan_folders:
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        norm_path = os.path.normpath(entry.path)
                        if norm_path in processed_paths:
                            continue
                        processed: Optional[StartupEntry] = _process_folder_entry(entry)
                        if processed:
                            found_entries.append(processed)
                            processed_paths.add(norm_path)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return found_entries


def _is_path_syntax_safe(path_str: str) -> bool:
    """Valida que la cadena de ruta no contenga inyecciones antes de procesarla."""
    if not isinstance(path_str, str): return False
    clean_path: str = path_str.strip(' "')
    if not clean_path or "\0" in clean_path or ("\\" not in clean_path and "/" not in clean_path):
        return False
    if clean_path.startswith(r"\\") or any(c in clean_path for c in SUSPICIOUS_CHARS):
        return False
    return True


def _is_valid_registry_path(path_str: str) -> bool:
    """
    Verifica la integridad de una cadena de registro resolviendo la existencia y seguridad de la ruta.
    
    Returns:
        True si la ruta es segura, absoluta y no protegida.
    """
    if not _is_path_syntax_safe(path_str):
        return False
    
    if path_str in _REGISTRY_PATH_CACHE:
        return _REGISTRY_PATH_CACHE[path_str]
        
    try:
        clean_str = path_str.strip(' "')
        if len(clean_str) > MAX_PATH_LEN:
            _REGISTRY_PATH_CACHE[path_str] = False
            return False
            
        target: Path = Path(clean_str)
        if target.is_symlink() or is_protected_path(target):
            _REGISTRY_PATH_CACHE[path_str] = False
            return False
        
        target_res: Path = target.resolve(strict=False)
        if not target_res or target_res.is_symlink() or is_protected_path(target_res):
            _REGISTRY_PATH_CACHE[path_str] = False
            return False
        
        _REGISTRY_PATH_CACHE[path_str] = True
        return True
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError, PermissionError):
        _REGISTRY_PATH_CACHE[path_str] = False
        return False


def _is_registry_entry_safe(name: str, command: str, seen: Set[str]) -> bool:
    """
    Verifica si un par clave/valor de registro es apto para ser inventariado.
    
    Args:
        name: Nombre de la clave del registro.
        command: Valor de la clave (comando de ejecución).
        seen: Conjunto de comandos ya procesados para evitar duplicados.
    """
    if not isinstance(name, str) or not isinstance(command, str): 
        return False
    
    # Validación reforzada de tipos y límites
    if len(command) > MAX_PATH_LEN or any(ord(c) < MIN_ASCII_VAL for c in command) or "\0" in command: 
        return False
        
    clean_name, clean_cmd = name.strip(), command.strip()
    if not clean_name or not clean_cmd or clean_cmd in seen: 
        return False
    
    return _is_valid_registry_path(clean_cmd)


def parse_registry_csv(csv_text: str, source: str = "registro") -> StartupEntries:
    """
    Parsea la salida cruda en formato CSV de PowerShell a una lista de objetos StartupEntry.
    
    Args:
        csv_text: Salida cruda capturada de la ejecución de PowerShell.
        source: Identificador del origen (default "registro").
    """
    if not isinstance(csv_text, str) or not csv_text.strip():
        return []
        
    parsed_entries: StartupEntries = []
    seen_commands: Set[str] = set()
    
    try:
        f: io.StringIO = io.StringIO(csv_text.strip())
        reader: csv.DictReader = csv.DictReader(f)
        
        if not reader.fieldnames or not {"Name", "Value"}.issubset(set(reader.fieldnames)):
            return []
        
        for row in reader:
            if not isinstance(row, dict):
                continue
            
            raw_name = row.get("Name")
            raw_cmd = row.get("Value")
            
            # Validación robusta de tipos antes de llamar a lógica de filtrado
            if not isinstance(raw_name, str) or not isinstance(raw_cmd, str):
                continue
            
            if _is_registry_entry_safe(raw_name, raw_cmd, seen_commands):
                seen_commands.add(raw_cmd.strip())
                parsed_entries.append(StartupEntry(name=raw_name.strip(), command=raw_cmd.strip(), source=source))
            
    except (csv.Error, ValueError, TypeError, AttributeError):
        return []
    return parsed_entries


def entries_from_registry(keys: RegistryKeySet = REGISTRY_RUN_KEYS) -> StartupEntries:
    """
    Invoca PowerShell para extraer programas de inicio definidos en las claves del Registro.
    
    Retorna:
        Lista de objetos StartupEntry parseados desde el Registro.
    """
    if os.name != "nt":
        return []
    
    keys_formatted: str = ", ".join([f"'{k}'" for k in keys])
    ps_script: str = f"""
    $ErrorActionPreference = 'SilentlyContinue'
    foreach ($path in @({keys_formatted})) {{
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
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError, ValueError):
        return []
    return []


def list_startup_entries() -> StartupEntries:
    """Consolida la lista total de programas de inicio mediante caché global."""
    global _FULL_SCAN_CACHE
    if _FULL_SCAN_CACHE is not None:
        return _FULL_SCAN_CACHE

    seen_cmds: Set[str] = set()
    results: List[StartupEntry] = []
    
    def generator() -> Iterator[StartupEntry]:
        for entry in itertools.chain(entries_from_folders(), entries_from_registry()):
            if entry.command not in seen_cmds:
                seen_cmds.add(entry.command)
                yield entry
    
    results = list(generator())
    _FULL_SCAN_CACHE = results
    return results


def estimate_impact(entries: Iterable[StartupEntry]) -> str:
    """
    Clasifica el impacto en el rendimiento según volumen de entradas detectadas.
    
    Args:
        entries: Iterable de StartupEntry a evaluar.
    """
    count: int = len(entries) if isinstance(entries, list) else sum(1 for _ in entries)
    if count <= 5: return "ok"
    if count <= 10: return "info"
    if count <= 18: return "warning"
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """
    Genera un informe unificado con los programas de inicio detectados.
    
    Args:
        entries: Lista opcional de entradas. Si no se provee, se usa list_startup_entries().
        
    Returns:
        Una lista de strings representando el informe para ser mostrado en la UI.
    """
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
