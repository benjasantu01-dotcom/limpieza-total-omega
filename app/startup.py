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

# Claves del registro donde los programas se anotan para arrancar solos.
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
    
    Atributos:
        name: Nombre descriptivo del programa.
        command: Línea de comando original desde el registro o carpeta.
        source: Origen de la detección ('carpeta' o 'registro').
    """
    name: str
    command: str
    source: str
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    def _is_valid_executable(self, path: Path) -> bool:
        """Valida que la extensión sea ejecutable y no sea un enlace simbólico (evita loops)."""
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_cmd: str) -> str:
        """Elimina caracteres de control y espacios en blanco no imprimibles."""
        if not isinstance(raw_cmd, str):
            return ""
        return "".join(c for c in raw_cmd.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """
        Extrae la ruta absoluta delimitada por comillas dobles.
        
        Validaciones:
            1. Verifica presencia de cierre de comillas.
            2. Filtra caracteres prohibidos en rutas de sistema.
            3. Aplica chequeo de seguridad via `is_protected_path`.
        """
        if not isinstance(raw_cmd, str) or len(raw_cmd) < 2:
            return ""
        end_quote: int = raw_cmd.find('"', 1)
        if end_quote == -1:
            return ""
        path_str: str = raw_cmd[1:end_quote].strip()
        
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
        """
        Resuelve y normaliza una ruta, verificando su existencia real en disco.
        
        Usa `_EXISTS_CACHE` para memoizar resultados de `realpath` y evitar
        consultas repetitivas al sistema de archivos, mejorando la performance.
        """
        if not isinstance(path_str, str) or not path_str or any(c in path_str for c in '<>|?*'):
            return ""
        
        if path_str in _EXISTS_CACHE:
            return path_str if _EXISTS_CACHE[path_str] else path_str
        
        try:
            p: Path = Path(path_str)
            if not p.is_absolute():
                _EXISTS_CACHE[path_str] = False
                return path_str
                
            # Verificación de integridad: evita seguir enlaces simbólicos maliciosos
            if is_protected_path(p) or p.is_symlink():
                _EXISTS_CACHE[path_str] = False
                return path_str
            
            # Realpath normaliza la ruta resolviendo junctions o atajos del sistema
            real_path = os.path.realpath(str(p))
            if not os.path.lexists(real_path) or is_protected_path(Path(real_path)):
                _EXISTS_CACHE[path_str] = False
                return ""
                
            _EXISTS_CACHE[real_path] = True
            return real_path
        except (OSError, ValueError, RuntimeError, TypeError):
            _EXISTS_CACHE[path_str] = False
            return path_str

    def _resolve_path_from_command(self, cmd: str) -> str:
        """
        Selecciona la estrategia de resolución de ruta según el formato del comando.
        
        Mitiga inyecciones de shell detectando caracteres de control y 
        delegando la extracción de la ruta al método correspondiente según
        presencia de comillas o parámetros adicionales.
        """
        if not cmd or not isinstance(cmd, str):
            return ""
        if any(char in cmd for char in ('&', '|', ';', '>', '<', '$', '`', '(', ')')):
            return ""

        if cmd.startswith('"'):
            return self._extract_quoted_path(cmd)
            
        try:
            parts: List[str] = cmd.split()
            if not parts:
                return ""
            return self._resolve_and_cache_path(parts[0])
        except (AttributeError, ValueError):
            return ""
        
    @property
    def executable(self) -> str:
        """
        Obtiene la ruta absoluta del ejecutable de forma perezosa.
        
        El resultado se almacena en `_exec_cache` durante la primera llamada.
        Si la ruta original es sospechosa o no existe, retorna una cadena vacía.
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
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            name, ext = os.path.splitext(entry.name)
                            if ext.lower() in EXECUTABLE_EXTS:
                                full_path = Path(entry.path)
                                if not is_protected_path(full_path):
                                    found_entries.append(StartupEntry(
                                        name=name,
                                        command=entry.path,
                                        source="carpeta"
                                    ))
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return found_entries


def parse_registry_csv(text: str, source: str = "registro") -> List[StartupEntry]:
    """
    Convierte el CSV crudo generado por PowerShell en objetos StartupEntry.
    
    Aplica filtros de seguridad: omite entradas con caracteres inválidos, 
    rutas protegidas o comandos que sugieran ejecución de scripts de PowerShell.
    """
    if not isinstance(text, str) or not text.strip():
        return []
        
    parsed_entries: List[StartupEntry] = []
    
    try:
        f = io.StringIO(text.strip())
        reader: csv.DictReader = csv.DictReader(f)
        
        for row in reader:
            try:
                if not isinstance(row, dict) or len(row) < 2:
                    continue
                
                # Validar que los valores existan y no sean None
                row_values = list(row.values())
                if len(row_values) < 2 or row_values[0] is None or row_values[1] is None:
                    continue
                
                vals = [str(v).strip() for v in row_values[:2]]
                name_raw, cmd_raw = vals[0], vals[1]
                
                name: str = "".join(c for c in name_raw if ord(c) >= 32).strip()
                cmd: str = "".join(c for c in cmd_raw if ord(c) >= 32).strip()
                
                if not name or not cmd or name.upper().startswith("PS"):
                    continue
                
                if any(c in cmd for c in '<>|?*'):
                    continue
                
                if is_protected_path(Path(cmd)):
                    continue
                    
                parsed_entries.append(StartupEntry(name=name, command=cmd, source=source))
            except (KeyError, ValueError, TypeError, OSError):
                continue
                
    except (csv.Error, OSError, ValueError, TypeError):
        return []
            
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """Recupera entradas de inicio desde el registro mediante una ejecución de PowerShell."""
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
            _REGISTRY_CACHE = parse_registry_csv(result.stdout)
            return _REGISTRY_CACHE
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def list_startup_entries() -> List[StartupEntry]:
    """Combina fuentes de carpetas y registro, eliminando duplicados mediante un ThreadPool."""
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
    """Clasifica el impacto de rendimiento según la cantidad de entradas."""
    count: int = len(entries)
    thresholds: List[Tuple[int, str]] = [(5, "ok"), (10, "info"), (18, "warning")]
    for limit, label in thresholds:
        if count <= limit:
            return label
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera informe textual con niveles de impacto para la interfaz de usuario."""
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
