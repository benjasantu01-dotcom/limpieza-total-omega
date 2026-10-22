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
    Iterable, Optional, Iterator, List, Tuple, Dict, Set, TypeAlias
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
    
    Implementa resolución de rutas diferida (lazy loading) para minimizar llamadas 
    al sistema de archivos. La clase actúa como un validador que encapsula tanto 
    la sintaxis de la ruta (protección contra inyecciones) como su estado actual 
    en el sistema operativo.
    """
    name: str
    command: str
    source: str
    
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    @property
    def is_valid(self) -> bool:
        """
        Valida que el comando de inicio no sea inherentemente malicioso o sintácticamente 
        inválido, evitando el procesamiento de rutas con nombres de dispositivos reservados.
        """
        if not self.command or self._is_path_suspicious(self.command):
            return False
        if self._is_reserved_device_name(self.command):
            return False
        return True

    def _is_reserved_device_name(self, path_str: str) -> bool:
        """Determina si la ruta hace referencia a un dispositivo de sistema (ej: NUL, CON)."""
        try:
            if not isinstance(path_str, str) or "\0" in path_str:
                return True
            if path_str.upper().startswith(("\\\\.\\", "\\\\?\\")):
                return True
            return Path(path_str).stem.upper() in RESERVED_DEVICE_NAMES
        except (ValueError, TypeError, AttributeError):
            return True

    def _is_path_suspicious(self, path_string: str) -> bool:
        """Verifica la presencia de caracteres de inyección o rutas de red UNC desaconsejadas."""
        if not isinstance(path_string, str):
            return True
        return any(c in path_string for c in SUSPICIOUS_CHARS) or path_string.startswith(r"\\")

    def _is_valid_executable(self, path: Path) -> bool:
        """Valida que la extensión del archivo corresponda a un ejecutable permitido y no sea un enlace simbólico."""
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_command: str) -> str:
        """Filtra caracteres de control no imprimibles que podrían romper el parser de comandos."""
        if not isinstance(raw_command, str):
            return ""
        return "".join(c for c in raw_command.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_command: str) -> str:
        """
        Extrae y valida una ruta delimitada por comillas dentro de una línea de comando.
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
        except (OSError, ValueError, RuntimeError, TypeError, PermissionError, AttributeError):
            return ""

    def _validate_file_access(self, p: Path) -> bool:
        """
        Confirma si el archivo existe físicamente y si es seguro según `safety.py`.
        """
        try:
            if is_protected_path(p) or p.is_symlink() or not is_safe_to_modify(p):
                return False
            return p.stat().is_file()
        except (OSError, PermissionError, FileNotFoundError, AttributeError):
            return False

    def _resolve_and_cache_path(self, path_string: str) -> str:
        """
        Resuelve rutas relativas a absolutas usando resolución canónica.
        """
        if not path_string or not isinstance(path_string, str) or not self.is_valid:
            return ""
        
        # Validación defensiva contra rutas excesivamente largas o UNC antes de normalizar
        if len(path_string) > 260 or path_string.startswith(r"\\"):
            return ""
            
        try:
            norm: str = os.path.normpath(path_string)
        except (ValueError, TypeError):
            return ""
        
        if path_string in _EXISTS_CACHE:
            return path_string if _EXISTS_CACHE[path_string] else path_string
        
        try:
            p: Path = Path(norm)
            if not p.is_absolute() or ".." in p.parts:
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
        """Analiza una cadena de comandos cruda para extraer únicamente la ruta ejecutable."""
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
                    if "\\" in parts[0] or "/" in parts[0]:
                        result = self._resolve_and_cache_path(parts[0])
        except (AttributeError, ValueError, IndexError, OSError, TypeError):
            result = ""
        
        _COMMAND_CACHE[command_line] = result
        return result
        
    @property
    def executable(self) -> str:
        """Retorna la ruta absoluta validada del ejecutable. Implementa resolución bajo demanda."""
        if self._checked_exists:
            return self._exec_cache or ""
            
        self._checked_exists = True
        if not self.command:
            return ""

        cmd: str = self._sanitize_command(self.command)
        self._exec_cache = self._resolve_path_from_command(cmd) if cmd else ""
            
        return self._exec_cache or ""


def startup_folders() -> List[Path]:
    """Identifica rutas de inicio de sistema y usuario, filtrando rutas protegidas."""
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
    """Valida un archivo individual en las carpetas de inicio contra extensiones y seguridad."""
    try:
        if not entry.is_file(follow_symlinks=False) or entry.is_symlink():
            return None
        
        name_ext = os.path.splitext(entry.name)
        if name_ext[1].lower() not in EXECUTABLE_EXTS:
            return None
            
        entry_path = entry.path
        if not isinstance(entry_path, str):
            return None
            
        p = Path(entry_path)
        if is_protected_path(p):
            return None
            
        return StartupEntry(name=name_ext[0], command=entry_path, source="carpeta")
    except (OSError, PermissionError, ValueError, TypeError):
        return None


def entries_from_folders(folders: Optional[Iterable[Path]] = None) -> StartupEntries:
    """Escanea las carpetas de inicio y retorna la lista consolidada de StartupEntry."""
    found_entries: StartupEntries = []
    scan_folders = folders if folders is not None else startup_folders()
    processed_paths: Set[str] = set()
    
    for folder in scan_folders:
        try:
            if not folder.exists():
                continue
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.path in processed_paths:
                            continue
                        processed = _process_folder_entry(entry)
                        if processed:
                            found_entries.append(processed)
                            processed_paths.add(entry.path)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return found_entries


def _is_registry_entry_safe(name: str, command: str, seen: Set[str]) -> bool:
    """
    Realiza una validación de seguridad sobre los valores crudos obtenidos del registro
    para prevenir inyecciones y accesos a rutas protegidas antes de instanciar StartupEntry.
    """
    if not isinstance(name, str) or not isinstance(command, str):
        return False
    
    if any(ord(c) < 32 for c in command) or "\0" in command:
        return False
        
    clean_name = name.strip()
    clean_cmd = command.strip()
    
    # Bloqueo defensivo: rutas UNC no deben estar en el registro de inicio
    if not clean_name or not clean_cmd or clean_cmd.startswith(r"\\") or clean_cmd in seen:
        return False
    
    try:
        target_path = Path(clean_cmd.strip('"'))
        if not target_path or ("\\" not in str(target_path) and "/" not in str(target_path)):
            return False
        if any(c in str(target_path) for c in SUSPICIOUS_CHARS):
            return False
        if is_protected_path(target_path) or ".." in str(target_path):
            return False
        return True
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


def parse_registry_csv(csv_text: str, source: str = "registro") -> StartupEntries:
    """
    Procesa el output CSV de PowerShell y lo convierte en objetos StartupEntry,
    aplicando filtros de seguridad preventivos a cada fila.
    """
    if not isinstance(csv_text, str) or not csv_text.strip():
        return []
        
    parsed_entries: StartupEntries = []
    seen_commands: Set[str] = set()
    
    try:
        f: io.StringIO = io.StringIO(csv_text.strip())
        reader = csv.DictReader(f)
        
        if not reader.fieldnames or "Name" not in reader.fieldnames or "Value" not in reader.fieldnames:
            return []
        
        for row in reader:
            try:
                if not isinstance(row, dict): continue
                
                raw_name = row.get("Name")
                raw_cmd = row.get("Value")
                
                if not (isinstance(raw_name, str) and raw_name.strip()) or \
                   not (isinstance(raw_cmd, str) and raw_cmd.strip()):
                    continue
                
                if _is_registry_entry_safe(raw_name, raw_cmd, seen_commands):
                    seen_commands.add(raw_cmd.strip())
                    parsed_entries.append(StartupEntry(name=raw_name.strip(), command=raw_cmd.strip(), source=source))
            except Exception:
                continue
            
    except (csv.Error, OSError, ValueError, TypeError):
        return parsed_entries
    return parsed_entries


def entries_from_registry(keys: RegistryKeySet = REGISTRY_RUN_KEYS) -> StartupEntries:
    """Extrae programas de inicio del Registro mediante un subproceso de PowerShell."""
    if os.name != "nt":
        return []
    
    keys_formatted = ", ".join([f"'{k}'" for k in keys])
    ps_script = f"""
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
        if process.stdout and process.stdout.strip():
            return parse_registry_csv(process.stdout)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return []
    return []


def list_startup_entries() -> StartupEntries:
    """Consolida las fuentes (Registro + Carpetas) eliminando duplicados."""
    global _FULL_SCAN_CACHE
    if _FULL_SCAN_CACHE is not None:
        return _FULL_SCAN_CACHE

    seen_items: Set[Tuple[str, str]] = set()
    unique_entries: StartupEntries = []
    
    def _iterator() -> Iterator[StartupEntry]:
        yield from entries_from_folders()
        yield from entries_from_registry()

    for entry in _iterator():
        key = (entry.name.lower(), entry.command.lower())
        if key not in seen_items:
            seen_items.add(key)
            unique_entries.append(entry)
            
    _FULL_SCAN_CACHE = unique_entries
    return unique_entries


def estimate_impact(entries: Iterable[StartupEntry]) -> str:
    """Calcula la severidad del impacto del inicio basado en la cantidad de entradas."""
    count = len(entries) if isinstance(entries, list) else sum(1 for _ in entries)
    if count <= 5: return "ok"
    if count <= 10: return "info"
    if count <= 18: return "warning"
    return "danger"


def summarize(entries: Optional[Iterable[StartupEntry]] = None) -> List[str]:
    """Genera el reporte de texto final con las entradas detectadas."""
    entries_list = entries if entries is not None else list_startup_entries()
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
