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

# Límite máximo estándar para rutas en el sistema de archivos de Windows.
MAX_PATH_LEN: int = 260

# Cachés globales: evitan consultas repetitivas al sistema de archivos o al registro.
_EXISTS_CACHE: Dict[str, bool] = {}
_COMMAND_CACHE: Dict[str, str] = {}
_FULL_SCAN_CACHE: Optional[StartupEntries] = None
_FOLDERS_CACHE: Optional[List[Path]] = None
_VALIDATION_CACHE: Dict[str, bool] = {}
_PROCESS_CACHE: Dict[str, bool] = {}

# Mensaje orientativo para el usuario final sobre la gestión de persistencia.
HOW_TO_DISABLE: str = (
    "Para deshabilitar un programa de inicio, usá el Administrador de tareas "
    "de Windows (Ctrl+Shift+Esc) → pestaña 'Inicio'. Esta app no modifica el "
    "registro de arranque a propósito por seguridad."
)


@dataclass
class StartupEntry:
    """
    Representa una entrada de inicio detectada. 

    Gestiona el ciclo de vida de una entrada, desde la recepción de la 
    línea de comando cruda hasta la validación y resolución absoluta de la ruta.
    """
    name: str
    command: str
    source: str
    
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    @property
    def is_valid(self) -> bool:
        """
        Valida que la estructura del comando cumpla con las restricciones de 
        seguridad básicas antes de intentar resolver el ejecutable.
        """
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
        """
        Verifica si la ruta apunta a un alias de dispositivo de Windows (ej. NUL, COM1).
        Hacer operaciones de archivo sobre estos nombres puede bloquear el hilo principal.
        """
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
        """
        Bloquea caracteres de shell (inyección de comandos) y rutas UNC de red.
        Las rutas UNC son excluidas porque los tiempos de respuesta del recurso remoto 
        pueden degradar la experiencia de usuario de la app.
        """
        if not isinstance(path_raw, str):
            return True
        return any(c in path_raw for c in SUSPICIOUS_CHARS) or path_raw.startswith(r"\\")

    def _is_valid_executable(self, path: Path) -> bool:
        """
        Filtra archivos por extensión permitida. Excluye enlaces simbólicos (`is_symlink`)
        como política preventiva contra el seguimiento de punteros no controlados.
        """
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_command: str) -> str:
        """Elimina caracteres de control ASCII (código < 32) para limpiar la cadena de comando."""
        if not isinstance(raw_command, str):
            return ""
        return "".join(c for c in raw_command.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """
        Extrae la ruta contenida entre comillas. Es crítico para separar el ejecutable 
        de sus argumentos en líneas de comandos de registro del tipo `"C:\App\app.exe" /flag`.
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
            if not p.parts or is_protected_path(p):
                return ""
            return str(p)
        except (OSError, ValueError, RuntimeError, TypeError, PermissionError, AttributeError):
            return ""

    def _validate_file_access(self, p: Path) -> bool:
        """
        Valida que el archivo exista y sea seguro. Usa `is_safe_to_modify` para cumplir
        con la política de seguridad del proyecto que prohíbe tocar rutas críticas.
        """
        p_str: str = str(p)
        if p_str in _VALIDATION_CACHE:
            return _VALIDATION_CACHE[p_str]
            
        try:
            if is_protected_path(p) or not is_safe_to_modify(p):
                _VALIDATION_CACHE[p_str] = False
                return False
            if not p.is_file() or p.is_symlink():
                _VALIDATION_CACHE[p_str] = False
                return False
            if not os.access(p, os.R_OK):
                _VALIDATION_CACHE[p_str] = False
                return False
                
            _VALIDATION_CACHE[p_str] = True
            return True
        except (OSError, PermissionError, RuntimeError):
            _VALIDATION_CACHE[p_str] = False
            return False

    def _resolve_and_cache_path(self, path_raw: str) -> str:
        """
        Normaliza una ruta a formato absoluto y verifica su existencia física.
        """
        if not isinstance(path_raw, str) or not path_raw or "\0" in path_raw:
            return ""
        
        if len(path_raw) > MAX_PATH_LEN or any(ord(c) < 32 for c in path_raw):
            return ""
            
        try:
            norm: str = os.path.normpath(path_raw)
            if path_raw in _EXISTS_CACHE:
                return path_raw if _EXISTS_CACHE[path_raw] else path_raw
            
            p: Path = Path(norm)
            if not p.is_absolute() or ".." in p.parts:
                _EXISTS_CACHE[path_raw] = False
                return ""
            
            resolved: Path = p.resolve(strict=False)
            if len(resolved.parts) > 64:
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
        """
        Decodifica la línea de comando para extraer el ejecutable. Diferencia entre
        rutas entrecomilladas (más precisas) y rutas simples.
        """
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
        """
        Retorna la ruta absoluta del ejecutable mediante resolución diferida. 
        """
        if self._checked_exists:
            return self._exec_cache or ""
            
        self._checked_exists = True
        if not self.command:
            return ""

        cmd: str = self._sanitize_command(self.command)
        self._exec_cache = self._resolve_path_from_command(cmd) if cmd else ""
            
        return self._exec_cache or ""


def startup_folders() -> List[Path]:
    """
    Identifica las carpetas de inicio del sistema y del usuario actual.
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
    Valida un objeto de archivo encontrado en una carpeta de inicio.
    """
    if not hasattr(entry, "path") or not hasattr(entry, "name"):
        return None
        
    if entry.path in _PROCESS_CACHE:
        return None
        
    try:
        if not entry.is_file(follow_symlinks=False) or entry.is_symlink():
            _PROCESS_CACHE[entry.path] = False
            return None
        
        _, ext = os.path.splitext(entry.name)
        if not ext or ext.lower() not in EXECUTABLE_EXTS:
            _PROCESS_CACHE[entry.path] = False
            return None
            
        if len(entry.path) > MAX_PATH_LEN:
            _PROCESS_CACHE[entry.path] = False
            return None
            
        path_obj: Path = Path(entry.path)
        if is_protected_path(path_obj) or not is_safe_to_modify(path_obj):
            _PROCESS_CACHE[entry.path] = False
            return None
            
        name: str = Path(entry.name).stem
        _PROCESS_CACHE[entry.path] = True
        return StartupEntry(name=name, command=entry.path, source="carpeta")
    except (OSError, PermissionError, ValueError, TypeError, AttributeError):
        _PROCESS_CACHE[entry.path] = False
        return None


def entries_from_folders(folders: Optional[Iterable[Path]] = None) -> StartupEntries:
    """
    Escanea las carpetas de inicio autorizadas.
    """
    found_entries: StartupEntries = []
    scan_folders: Iterable[Path] = folders if folders is not None else startup_folders()
    processed_paths: Set[str] = set()
    
    for folder in scan_folders:
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    if entry.path in processed_paths:
                        continue
                    processed: Optional[StartupEntry] = _process_folder_entry(entry)
                    if processed:
                        found_entries.append(processed)
                        processed_paths.add(entry.path)
        except (OSError, PermissionError):
            continue
    return found_entries


def _is_valid_registry_path(path_str: str) -> bool:
    """
    Valida sintácticamente una ruta de archivo obtenida del registro.
    """
    if not isinstance(path_str, str):
        return False
        
    try:
        clean_path: str = path_str.strip(' "')
        if not clean_path or "\0" in clean_path or ("\\" not in clean_path and "/" not in clean_path):
            return False
        
        target: Path = Path(clean_path)
        if target.is_symlink(): return False
        
        if is_protected_path(target): return False
        
        target_res: Path = target.resolve(strict=False)
        if is_protected_path(target_res): return False
        
        if any(c in str(target) for c in SUSPICIOUS_CHARS): return False
        
        return True
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        return False


def _is_registry_entry_safe(name: str, command: str, seen: Set[str]) -> bool:
    """Filtra entradas del registro, descartando duplicados y rutas sospechosas."""
    if not isinstance(name, str) or not isinstance(command, str): return False
    if len(command) > MAX_PATH_LEN or any(ord(c) < 32 for c in command) or "\0" in command: return False
        
    clean_name, clean_cmd = name.strip(), command.strip()
    if not clean_name or not clean_cmd or clean_cmd.startswith(r"\\") or clean_cmd in seen: return False
    
    return _is_valid_registry_path(clean_cmd)


def parse_registry_csv(csv_text: str, source: str = "registro") -> StartupEntries:
    """
    Procesa la salida CSV cruda de PowerShell hacia una lista de `StartupEntry`.
    """
    if not isinstance(csv_text, str) or not csv_text.strip():
        return []
        
    parsed_entries: StartupEntries = []
    seen_commands: Set[str] = set()
    
    try:
        f: io.StringIO = io.StringIO(csv_text.strip())
        reader: csv.DictReader = csv.DictReader(f)
        
        if not reader.fieldnames or "Name" not in reader.fieldnames or "Value" not in reader.fieldnames:
            return []
        
        for row in reader:
            if not isinstance(row, dict):
                continue
            
            raw_name: Optional[str] = row.get("Name")
            raw_cmd: Optional[str] = row.get("Value")
            
            if not isinstance(raw_name, str) or not isinstance(raw_cmd, str):
                continue
            
            n, c = raw_name.strip(), raw_cmd.strip()
            if not n or not c:
                continue
            
            if _is_registry_entry_safe(n, c, seen_commands):
                seen_commands.add(c)
                parsed_entries.append(StartupEntry(name=n, command=c, source=source))
            
    except (csv.Error, OSError, ValueError, TypeError, AttributeError):
        return parsed_entries
    return parsed_entries


def entries_from_registry(keys: RegistryKeySet = REGISTRY_RUN_KEYS) -> StartupEntries:
    """
    Invoca PowerShell para extraer valores de registro.
    """
    if os.name != "nt":
        return []
    
    keys_formatted: str = ", ".join([f"'{k}'" for k in keys])
    ps_script: str = f"""
    $ErrorActionPreference = 'SilentlyContinue'
    foreach ($path in @({keys_formatted})) {{
        $item = Get-ItemProperty -LiteralPath $path -ErrorAction SilentlyContinue
        if ($item) {{
            $item.PSObject.Properties | Where-Object {{ $_.Name -ne 'PSPath' -and $_.Name -ne 'PSParentPath' -and $_.Name -ne 'PSChildName' -and $_.Name -ne 'PSDrive' -and $_.Name -ne 'PSProvider' }} | 
            Select-Object @{{Name='Name';Expression={{$_.Name}}}}, @{{Name='Value';Expression={{$_.Value}}}}
        }}
    }} | ConvertTo-Csv -NoTypeInformation
    """
    
    try:
        process: subprocess.CompletedProcess = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=10, check=False
        )
        if process.returncode == 0 and process.stdout and process.stdout.strip():
            return parse_registry_csv(process.stdout)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return []
    return []


def list_startup_entries() -> StartupEntries:
    """
    Consolida todas las fuentes de inicio en un inventario único y cacheado.
    """
    global _FULL_SCAN_CACHE
    if _FULL_SCAN_CACHE is not None:
        return _FULL_SCAN_CACHE

    seen_commands: Set[str] = set()
    unique_entries: StartupEntries = []
    
    for entry in entries_from_folders():
        if entry.command not in seen_commands:
            seen_commands.add(entry.command)
            unique_entries.append(entry)
            
    for entry in entries_from_registry():
        if entry.command not in seen_commands:
            seen_commands.add(entry.command)
            unique_entries.append(entry)
            
    _FULL_SCAN_CACHE = unique_entries
    return unique_entries


def estimate_impact(entries: Iterable[StartupEntry]) -> str:
    """
    Estima el impacto en el rendimiento basado en la cantidad de entradas detectadas.
    """
    count: int = len(entries) if isinstance(entries, list) else sum(1 for _ in entries)
    if count <= 5: return "ok"
    if count <= 10: return "info"
    if count <= 18: return "warning"
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """
    Genera el reporte final formateado para la UI.
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
