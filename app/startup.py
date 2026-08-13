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
from typing import Iterable, Optional, Iterator, List, Tuple, Dict, Sequence, Set
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

# Claves del registro donde los programas se anotan para arrancar solos.
REGISTRY_RUN_KEYS: Tuple[str, ...] = (
    r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
)

# Extensiones consideradas ejecutables para el escaneo de carpetas.
EXECUTABLE_EXTS: Tuple[str, ...] = ('.exe', '.bat', '.cmd', '.scr', '.lnk')

# Caché global para evitar operaciones de I/O redundantes durante la sesión.
_EXISTS_CACHE: Dict[str, bool] = {}
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
    
    Utiliza resolución perezosa (`lazy loading`) para el ejecutable: la validación 
    de existencia en disco y el chequeo de seguridad (`safety.py`) se realizan 
    solo al acceder a la propiedad `executable`.
    """
    name: str
    command: str
    source: str
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    def _is_valid_executable(self, path: Path) -> bool:
        """Determina si un objeto Path corresponde a un archivo ejecutable permitido."""
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_cmd: str) -> str:
        """Limpia caracteres de control (ASCII < 32) de la cadena de comando."""
        if not isinstance(raw_cmd, str):
            return ""
        return "".join(c for c in raw_cmd.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """Extrae la ruta de un comando cuando está encapsulada entre comillas."""
        if not isinstance(raw_cmd, str) or len(raw_cmd) < 2:
            return ""
        end_quote: int = raw_cmd.find('"', 1)
        if end_quote == -1:
            return ""
        path_str: str = raw_cmd[1:end_quote]
        
        if not path_str or any(c in path_str for c in '<>|?*'):
            return ""
        
        try:
            p: Path = Path(path_str)
            if is_protected_path(p):
                return ""
            return str(p)
        except (OSError, ValueError, RuntimeError, TypeError):
            return ""

    def _resolve_and_cache_path(self, path_str: str) -> str:
        """Normaliza y valida una ruta, aplicando caché para optimizar el I/O."""
        if not isinstance(path_str, str) or not path_str or any(c in path_str for c in '<>|?*'):
            return ""
        
        if path_str in _EXISTS_CACHE:
            return path_str if _EXISTS_CACHE[path_str] else path_str
        
        try:
            p: Path = Path(path_str)
            if not p.parts or not p.is_absolute():
                _EXISTS_CACHE[path_str] = False
                return path_str
                
            if is_protected_path(p) or p.is_symlink():
                _EXISTS_CACHE[path_str] = False
                return path_str
            
            if not p.exists():
                _EXISTS_CACHE[path_str] = False
                return path_str
                
            try:
                p_abs: Path = p.resolve(strict=True)
            except (OSError, PermissionError, RuntimeError):
                return path_str
                
            if is_protected_path(p_abs):
                _EXISTS_CACHE[path_str] = False
                return ""
                
            p_str: str = str(p_abs)
            _EXISTS_CACHE[p_str] = p_abs.is_file()
            return p_str if _EXISTS_CACHE[p_str] else path_str
        except (OSError, ValueError, RuntimeError, TypeError):
            _EXISTS_CACHE[path_str] = False
            return path_str

    def _resolve_path_from_command(self, cmd: str) -> str:
        """Parsea la cadena de comando para extraer e identificar el ejecutable."""
        if not cmd:
            return ""
        # Evitar comandos complejos que contengan operadores de shell
        if any(char in cmd for char in ('&', '|', ';', '>', '<', '$', '`', '(', ')')):
            return ""

        if cmd.startswith('"'):
            return self._extract_quoted_path(cmd)
            
        parts: List[str] = cmd.split()
        return self._resolve_and_cache_path(parts[0]) if parts else ""
        
    @property
    def executable(self) -> str:
        """Retorna la ruta absoluta del ejecutable tras ejecutar la resolución perezosa."""
        if self._checked_exists:
            return self._exec_cache or ""
            
        self._checked_exists = True
        if not self.command:
            return ""

        cmd: str = self._sanitize_command(self.command)
        self._exec_cache = self._resolve_path_from_command(cmd) if cmd else ""
            
        return self._exec_cache or ""


def startup_folders() -> List[Path]:
    """Identifica las rutas estándar de carpetas de Inicio en Windows."""
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
    """Escanea directorios de inicio y retorna una lista de objetos StartupEntry."""
    found_entries: List[StartupEntry] = []
    scan_folders = folders if folders is not None else startup_folders()
    
    for folder in scan_folders:
        if not folder or not folder.is_dir():
            continue
        try:
            found_entries.extend(
                StartupEntry(name=item.stem, command=str(item), source="carpeta")
                for item in folder.iterdir()
                if item.is_file() and item.stem and not is_protected_path(item) 
                and item.suffix.lower() in EXECUTABLE_EXTS and not item.is_symlink()
            )
        except (OSError, PermissionError):
            continue
    return found_entries


def parse_registry_csv(text: str, source: str = "registro") -> List[StartupEntry]:
    """Convierte el CSV generado por PowerShell en objetos StartupEntry."""
    if not isinstance(text, str) or not text.strip():
        return []
        
    parsed_entries: List[StartupEntry] = []
    
    try:
        reader: csv.DictReader = csv.DictReader(io.StringIO(text))
        for row in reader:
            if not isinstance(row, dict) or len(row) < 2:
                continue
            
            vals = list(row.values())
            name_raw = vals[0] if len(vals) > 0 else None
            cmd_raw = vals[1] if len(vals) > 1 else None
            
            if not isinstance(name_raw, str) or not isinstance(cmd_raw, str):
                continue
                
            name: str = "".join(c for c in name_raw if ord(c) >= 32).strip()
            cmd: str = "".join(c for c in cmd_raw if ord(c) >= 32).strip()
            
            if not name or name.lower() in ("name", "pscustomobject") or name.upper().startswith("PS"):
                continue
            
            if not cmd or any(c in cmd for c in '<>|?*'):
                continue
            
            try:
                p: Path = Path(cmd)
                if is_protected_path(p):
                    continue
            except (ValueError, TypeError, OSError):
                continue
                
            parsed_entries.append(StartupEntry(name=name, command=cmd, source=source))
            
    except (csv.Error, OSError, ValueError, TypeError):
        return []
            
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """Recupera entradas de inicio desde el registro mediante PowerShell."""
    global _REGISTRY_CACHE
    if os.name != "nt":
        return []
    
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE
    
    targets: str = ", ".join(f"'{k}'" for k in keys)
    ps_cmd: str = f"Get-ItemProperty {targets} -ErrorAction SilentlyContinue | Select-Object * -ExcludeProperty PS* | ConvertTo-Csv -NoTypeInformation"
    
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            _REGISTRY_CACHE = parse_registry_csv(result.stdout)
            return _REGISTRY_CACHE
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def list_startup_entries() -> List[StartupEntry]:
    """Combina fuentes de carpetas y registro, eliminando duplicados por nombre."""
    seen_names: Set[str] = set()
    unique_entries: List[StartupEntry] = []
    
    def _generator() -> Iterator[StartupEntry]:
        yield from entries_from_folders()
        yield from entries_from_registry()
    
    for entry in _generator():
        name_normalized: str = entry.name.lower()
        if name_normalized not in seen_names:
            seen_names.add(name_normalized)
            unique_entries.append(entry)
            
    return unique_entries


def estimate_impact(entries: Sequence[StartupEntry]) -> str:
    """Clasifica el impacto de rendimiento según la cantidad de entradas."""
    count: int = len(entries)
    thresholds: List[Tuple[int, str]] = [(5, "ok"), (10, "info"), (18, "warning")]
    for limit, label in thresholds:
        if count <= limit:
            return label
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera una salida de texto para la interfaz con el estado del inicio."""
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
