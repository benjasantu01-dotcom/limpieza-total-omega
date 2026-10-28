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
    
    Encapsula la lógica de resolución, limpieza y validación de rutas de archivos
    ejecutables provenientes de distintas fuentes del sistema (Registro o carpetas).
    """
    name: str
    command: str
    source: str
    
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    @property
    def is_valid(self) -> bool:
        """
        Valida que la cadena del comando cumpla con las restricciones básicas:
        no nulo, longitud aceptable, sin caracteres de control o nombres reservados.
        """
        if not self.command or not isinstance(self.command, str) or len(self.command) > MAX_PATH_LEN or self._is_path_suspicious(self.command):
            return False
        if self._is_reserved_device_name(self.command):
            return False
        return True

    def _is_reserved_device_name(self, path_str: str) -> bool:
        """
        Detecta si un string de ruta corresponde a un dispositivo de hardware
        reservado (ej. COM1, NUL) que no debe abrirse como archivo.
        """
        if not isinstance(path_str, str) or "\0" in path_str:
            return True
        try:
            if path_str.upper().startswith(("\\\\.\\", "\\\\?\\")):
                return True
            return Path(path_str).stem.upper() in RESERVED_DEVICE_NAMES
        except (ValueError, TypeError, AttributeError):
            return True

    def _is_path_suspicious(self, path_string: str) -> bool:
        """
        Verifica la presencia de caracteres peligrosos para la shell o
        protocolos de red (UNC) no soportados para ejecución local.
        """
        if not isinstance(path_string, str):
            return True
        return any(c in path_string for c in SUSPICIOUS_CHARS) or path_string.startswith(r"\\")

    def _is_valid_executable(self, path: Path) -> bool:
        """Valida si el archivo es ejecutable y no un enlace simbólico (evita loops)."""
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_command: str) -> str:
        """Limpia el comando eliminando caracteres de control no imprimibles."""
        if not isinstance(raw_command, str):
            return ""
        return "".join(c for c in raw_command.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_command: str) -> str:
        """
        Extrae la ruta contenida entre comillas. Realiza validación de seguridad
        adicional para asegurar que no contenga referencias a padres ('..').
        """
        if not isinstance(raw_command, str) or len(raw_command) < 3:
            return ""
        
        end_quote: int = raw_command.find('"', 1)
        if end_quote == -1:
            return ""
            
        path_str: str = raw_command[1:end_quote].strip()
        
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
        Verifica la integridad del archivo en el sistema de archivos actual.
        Asegura que el archivo sea accesible y que la ruta no esté bloqueada.
        """
        try:
            if not p.is_file() or p.is_symlink():
                return False
            
            if is_protected_path(p) or not is_safe_to_modify(p):
                return False
                
            return p.exists()
        except (OSError, PermissionError, RuntimeError):
            return False

    def _resolve_and_cache_path(self, path_string: str) -> str:
        """
        Normaliza y resuelve rutas absolutas. Utiliza caché global para minimizar
        llamadas costosas al sistema de archivos durante el escaneo.
        """
        if not path_string or not isinstance(path_string, str) or not self.is_valid:
            return ""
        
        # Filtro adicional contra caracteres de control que rompen pathlib.Path
        if any(ord(c) < 32 for c in path_string):
            return ""
        
        if len(path_string) > MAX_PATH_LEN or path_string.startswith(r"\\") or "\0" in path_string:
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
        """
        Descompone la línea de comandos de inicio para aislar el ejecutable,
        gestionando tanto rutas simples como rutas entrecomilladas.
        """
        if not isinstance(command_line, str) or not command_line:
            return ""
        
        if command_line in _COMMAND_CACHE:
            return _COMMAND_CACHE[command_line]
        
        result: str = ""
        try:
            if command_line.startswith('"'):
                result = self._extract_quoted_path(command_line)
            else:
                parts: List[str] = command_line.split()
                if parts and isinstance(parts[0], str) and parts[0]:
                    if "\\" in parts[0] or "/" in parts[0]:
                        result = self._resolve_and_cache_path(parts[0])
        except (AttributeError, ValueError, IndexError, OSError, TypeError):
            result = ""
        
        _COMMAND_CACHE[command_line] = result
        return result
        
    @property
    def executable(self) -> str:
        """Retorna la ruta absoluta del ejecutable tras resolverla bajo demanda."""
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
    Identifica las rutas de carpetas 'Startup' del sistema y del usuario.
    
    Retorna una lista de objetos Path hacia directorios que requieren escaneo.
    Solo retorna rutas existentes, no protegidas y que no son enlaces simbólicos.
    """
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
    """
    Valida la integridad de una entrada individual de carpeta de inicio.
    
    Verifica que sea un archivo ejecutable, no un enlace simbólico y que no esté
    dentro de una ruta protegida.
    """
    try:
        if not entry.is_file(follow_symlinks=False) or entry.is_symlink():
            return None
        
        name_ext = os.path.splitext(entry.name)
        if name_ext[1].lower() not in EXECUTABLE_EXTS:
            return None
            
        entry_path = entry.path
        if not isinstance(entry_path, str) or len(entry_path) > MAX_PATH_LEN:
            return None
            
        p = Path(entry_path)
        if is_protected_path(p):
            return None
            
        return StartupEntry(name=name_ext[0], command=entry_path, source="carpeta")
    except (OSError, PermissionError, ValueError, TypeError):
        return None


def entries_from_folders(folders: Optional[Iterable[Path]] = None) -> StartupEntries:
    """
    Escanea las carpetas de inicio proporcionadas y retorna objetos StartupEntry válidos.
    
    Si no se proveen carpetas, utiliza las determinadas por `startup_folders`.
    """
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
    Filtro de seguridad para entradas del registro: descarta comandos malformados,
    rutas restringidas o caracteres de control que sugieran inyección.
    """
    if not isinstance(name, str) or not isinstance(command, str):
        return False
    
    # Prevenir inyección o rutas inválidas mediante caracteres de control
    if len(command) > MAX_PATH_LEN or any(ord(c) < 32 for c in command) or "\0" in command:
        return False
        
    clean_name = name.strip()
    clean_cmd = command.strip()
    
    if not clean_name or not clean_cmd or clean_cmd.startswith(r"\\") or clean_cmd in seen:
        return False
    
    try:
        # Extraer posible ruta quitando comillas
        path_candidate = clean_cmd.strip('"')
        if not path_candidate:
            return False
        
        # Validar que sea una ruta absoluta o al menos contenga separadores de disco
        if "\\" not in path_candidate and "/" not in path_candidate:
            return False
        
        # Seguridad: Prevenir directory traversal
        if ".." in path_candidate.split(os.sep):
            return False
            
        target_path = Path(path_candidate)
        if any(c in str(target_path) for c in SUSPICIOUS_CHARS):
            return False
        if is_protected_path(target_path):
            return False
        return True
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


def parse_registry_csv(csv_text: str, source: str = "registro") -> StartupEntries:
    """
    Convierte el output CSV de PowerShell en objetos StartupEntry.
    
    Espera un string con formato CSV donde las columnas 'Name' y 'Value'
    representan la clave del registro y el comando asociado.
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
            if not isinstance(row, dict):
                continue
            
            raw_name = row.get("Name")
            raw_cmd = row.get("Value")
            
            if not isinstance(raw_name, str) or not isinstance(raw_cmd, str):
                continue
                
            n, c = raw_name.strip(), raw_cmd.strip()
            if not n or not c:
                continue
            
            if _is_registry_entry_safe(n, c, seen_commands):
                seen_commands.add(c)
                parsed_entries.append(StartupEntry(name=n, command=c, source=source))
            
    except (csv.Error, OSError, ValueError, TypeError):
        return parsed_entries
    return parsed_entries


def entries_from_registry(keys: RegistryKeySet = REGISTRY_RUN_KEYS) -> StartupEntries:
    """
    Extrae programas del Registro de Windows mediante un subproceso PowerShell.
    
    Invoca PowerShell para obtener propiedades de claves Run, evitando ejecutar
    código arbitrario al usar -LiteralPath y parámetros estrictos.
    """
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
        if process.returncode == 0 and process.stdout and process.stdout.strip():
            return parse_registry_csv(process.stdout)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return []
    return []


def list_startup_entries() -> StartupEntries:
    """Consolida las fuentes de inicio (carpetas y registro) evitando duplicados."""
    global _FULL_SCAN_CACHE
    if _FULL_SCAN_CACHE is not None:
        return _FULL_SCAN_CACHE

    seen_commands: Set[str] = set()
    unique_entries: StartupEntries = []
    
    for entry in itertools.chain(entries_from_folders(), entries_from_registry()):
        if entry.command not in seen_commands:
            seen_commands.add(entry.command)
            unique_entries.append(entry)
            
    _FULL_SCAN_CACHE = unique_entries
    return unique_entries


def estimate_impact(entries: Iterable[StartupEntry]) -> str:
    """Calcula la severidad del impacto del arranque según la cantidad detectada."""
    count = len(entries) if isinstance(entries, list) else sum(1 for _ in entries)
    if count <= 5: return "ok"
    if count <= 10: return "info"
    if count <= 18: return "warning"
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera un reporte de texto legible por el usuario con el resumen del inventario."""
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
