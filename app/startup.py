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
    """Representa una entrada de inicio detectada.
    
    Implementa resolución perezosa (lazy loading) para la propiedad `executable`:
    la validación de existencia en disco y los chequeos de `safety.py` solo se
    ejecutan la primera vez que se accede a la propiedad para optimizar I/O.
    """
    name: str
    command: str
    source: str
    _exec_cache: Optional[str] = field(default=None, init=False)
    _checked_exists: bool = field(default=False, init=False)

    def _is_valid_executable(self, path: Path) -> bool:
        """Verifica si la extensión es ejecutable y descarta enlaces simbólicos."""
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_cmd: str) -> str:
        """Limpia caracteres de control o no imprimibles de la cadena de comando."""
        if not isinstance(raw_cmd, str):
            return ""
        return "".join(c for c in raw_cmd.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """Extrae rutas de comandos encerradas en comillas, validando que no contengan
        caracteres maliciosos ni apunten a ubicaciones protegidas por safety.py."""
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
        """Realiza la validación profunda de la ruta:
        1. Normaliza con resolve() para evitar trucos de rutas relativas.
        2. Verifica que la ruta final no esté en la lista negra de safety.py.
        3. Persiste el resultado en `_EXISTS_CACHE` para evitar llamadas de sistema redundantes.
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
                
            if is_protected_path(p) or p.is_symlink():
                _EXISTS_CACHE[path_str] = False
                return path_str
            
            if not p.exists():
                _EXISTS_CACHE[path_str] = False
                return path_str
                
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
        """Parsea un comando de inicio, manejando tanto rutas directas como comandos con argumentos.
        Detecta y bloquea comandos sospechosos que incluyan operadores de shell."""
        if any(char in cmd for char in ('&', '|', ';', '>', '<', '$', '`', '(', ')')):
            return ""

        if cmd.startswith('"'):
            return self._extract_quoted_path(cmd)
            
        parts: List[str] = cmd.split()
        return self._resolve_and_cache_path(parts[0]) if parts else ""
        
    @property
    def executable(self) -> str:
        """Devuelve la ruta absoluta del ejecutable tras resolución perezosa."""
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
    """Procesa el CSV exportado desde PowerShell para crear objetos StartupEntry."""
    if not isinstance(text, str) or not text.strip():
        return []
        
    parsed_entries: List[StartupEntry] = []
    
    try:
        reader: csv.DictReader = csv.DictReader(io.StringIO(text))
        for row in reader:
            if not isinstance(row, dict) or len(row) < 2:
                continue
            
            # Obtiene los valores usando las claves reales detectadas por DictReader
            values = list(row.values())
            name_raw = values[0]
            cmd_raw = values[1]
            
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
    """Consulta las claves del registro de Windows mediante PowerShell y caché global."""
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
