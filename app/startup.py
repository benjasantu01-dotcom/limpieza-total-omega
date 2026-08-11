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

# Extensiones que consideramos ejecutables para el escaneo de carpetas.
EXECUTABLE_EXTS: Tuple[str, ...] = ('.exe', '.bat', '.cmd', '.scr', '.lnk')

# Caché global de archivos validados y resultados de registro para evitar I/O repetitivo.
_EXISTS_CACHE: Dict[str, bool] = {}
_REGISTRY_CACHE: Optional[List[StartupEntry]] = None

# Se le muestra al usuario en vez de ofrecer un botón que toque el registro.
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
    Representa un elemento de inicio detectado y provee mecanismos de resolución segura.
    
    Esta clase implementa una estrategia de resolución perezosa para `executable`,
    asegurando que las validaciones de `safety.py` y el acceso al disco (I/O) 
    ocurran solo una vez por instancia y bajo demanda.
    """
    name: str
    command: str
    source: str  # Indica si proviene de una carpeta o una ruta de registro específica
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    def _is_valid_executable(self, path: Path) -> bool:
        """Filtra extensiones permitidas y descarta enlaces simbólicos para evitar bucles."""
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_cmd: str) -> str:
        """Limpia la cadena de comando eliminando caracteres no imprimibles."""
        if not isinstance(raw_cmd, str):
            return ""
        return "".join(c for c in raw_cmd.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """
        Extracts a quoted path from a command string with basic validation.
        """
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
        """
        Valida la existencia física y la seguridad de la ruta en disco.
        """
        if not isinstance(path_str, str) or not path_str:
            return ""
        
        if any(c in path_str for c in '<>|?*'):
            return ""
        
        if path_str in _EXISTS_CACHE:
            return path_str if _EXISTS_CACHE[path_str] else path_str
        
        try:
            p: Path = Path(path_str)
            if not p.is_absolute():
                _EXISTS_CACHE[path_str] = False
                return path_str
                
            if is_protected_path(p) or p.is_symlink():
                _EXISTS_CACHE[path_str] = False
                return path_str
            
            if not p.exists():
                _EXISTS_CACHE[path_str] = False
                return path_str
                
            # Resolver la ruta real para normalizar, manejando permisos denegados
            try:
                p_abs: Path = p.resolve(strict=True)
            except (OSError, PermissionError):
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
        """
        Determina la ruta del binario analizando si el comando es una ruta directa 
        o un comando con argumentos (formato con o sin comillas).
        """
        # Seguridad: Bloquea comandos que parecen invocar shells o redirecciones
        if any(char in cmd for char in ('&', '|', ';', '>', '<', '$', '`', '(', ')')):
            return ""

        if cmd.startswith('"'):
            return self._extract_quoted_path(cmd)
            
        parts: List[str] = cmd.split()
        return self._resolve_and_cache_path(parts[0]) if parts else ""
        
    @property
    def executable(self) -> str:
        """
        Ruta absoluta del ejecutable.
        La resolución se realiza bajo demanda; el resultado se almacena en `_exec_cache`.
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
    """Identifica rutas de carpetas de Inicio del sistema, filtrando protegidas."""
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
    return [c for c in candidates if c.is_dir() and not is_protected_path(c)]


def entries_from_folders(folders: Optional[Sequence[Path]] = None) -> List[StartupEntry]:
    """Escanea directorios Startup del SO y mapea archivos ejecutables a StartupEntry."""
    if folders is None:
        folders = startup_folders()
    
    found_entries: List[StartupEntry] = []

    for folder in folders:
        try:
            for item in folder.iterdir():
                if item.is_file() and item.suffix.lower() in EXECUTABLE_EXTS and not item.is_symlink():
                    if is_protected_path(item):
                        continue
                    try:
                        name: str = item.stem
                        if name:
                            found_entries.append(StartupEntry(name=name, command=str(item), source="carpeta"))
                    except OSError:
                        continue
        except (OSError, PermissionError):
            continue
    return found_entries


def parse_registry_csv(text: str, source: str = "registro") -> List[StartupEntry]:
    """
    Parsea la salida CSV de PowerShell usando el módulo csv estándar.
    """
    if not isinstance(text, str) or not text.strip():
        return []
        
    parsed_entries: List[StartupEntry] = []
    
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            if not isinstance(row, dict):
                continue
            keys = list(row.keys())
            if len(keys) < 2:
                continue
                
            name_raw = row.get(keys[0])
            cmd_raw = row.get(keys[1])
            
            if not isinstance(name_raw, str) or not isinstance(cmd_raw, str):
                continue
                
            name = "".join(c for c in name_raw if ord(c) >= 32).strip()
            cmd = "".join(c for c in cmd_raw if ord(c) >= 32).strip()
            
            if not name or name.lower() in ("name", "pscustomobject") or name.upper().startswith("PS"):
                continue
            
            try:
                p = Path(cmd)
                if is_protected_path(p):
                    continue
            except (ValueError, TypeError):
                continue
                
            parsed_entries.append(StartupEntry(name=name, command=cmd, source=source))
            
    except (csv.Error, OSError, ValueError, TypeError):
        return []
            
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """Consulta las claves del registro de Windows mediante PowerShell y caché global."""
    global _REGISTRY_CACHE
    if os.name != "nt":
        return []
    
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE
    
    targets = ", ".join(f"'{k}'" for k in keys)
    ps_cmd = f"Get-ItemProperty {targets} -ErrorAction SilentlyContinue | Select-Object * -ExcludeProperty PS* | ConvertTo-Csv -NoTypeInformation"
    
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
    """Combina y deduplica entradas de inicio halladas en carpetas y registro."""
    seen_names: Set[str] = set()
    unique_entries: List[StartupEntry] = []
    
    all_raw_entries: List[StartupEntry] = entries_from_folders() + entries_from_registry()
    
    for entry in all_raw_entries:
        name_normalized: str = entry.name.lower()
        if name_normalized not in seen_names:
            seen_names.add(name_normalized)
            unique_entries.append(entry)
            
    return unique_entries


def estimate_impact(entries: Sequence[StartupEntry]) -> str:
    """Clasifica la criticidad del impacto de inicio según volumen de entradas."""
    count: int = len(entries)
    thresholds: List[Tuple[int, str]] = [(5, "ok"), (10, "info"), (18, "warning")]
    for limit, label in thresholds:
        if count <= limit:
            return label
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera una representación textual legible del reporte para la interfaz."""
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
