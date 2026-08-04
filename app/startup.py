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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Iterator, List, Tuple, Dict, Sequence
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

# Caché global de archivos validados para evitar I/O repetitivo.
_EXISTS_CACHE: Dict[str, bool] = {}

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
    """Representa una entrada de inicio (programa) detectada en el sistema."""
    name: str
    command: str
    source: str  # Indica si proviene de una carpeta o una ruta de registro específica
    _exec_cache: Optional[str] = None
    _checked_exists: bool = False

    def _is_valid_executable(self, path: Path) -> bool:
        """Determina si un objeto Path apunta a un ejecutable válido y seguro."""
        try:
            return path.suffix.lower() in EXECUTABLE_EXTS and not path.is_symlink()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _sanitize_command(self, raw_cmd: str) -> str:
        """Filtra caracteres no imprimibles del comando para evitar inyecciones o errores de display."""
        return "".join(c for c in raw_cmd.strip() if ord(c) >= 32)

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """
        Extrae la ruta de un comando entrecomillado.
        Retorna la cadena de la ruta si es segura, o una cadena vacía en caso contrario.
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
        Expande y valida la ruta de un ejecutable contra el sistema de archivos.
        Usa _EXISTS_CACHE para minimizar llamadas a disco (I/O).
        """
        if not isinstance(path_str, str) or not path_str or any(c in path_str for c in '<>|?*'):
            return ""
        try:
            p: Path = Path(path_str)
            if is_protected_path(p) or p.is_symlink():
                return path_str
            
            # Resolve puede fallar con permisos denegados en directorios padre
            p_abs = p.expanduser().resolve(strict=False)
            
            if is_protected_path(p_abs):
                return ""
                
            p_str = str(p_abs)
            if p_str not in _EXISTS_CACHE:
                _EXISTS_CACHE[p_str] = p_abs.is_file()
            
            return p_str if _EXISTS_CACHE[p_str] else path_str
        except (OSError, ValueError, RuntimeError, TypeError):
            return path_str

    def _resolve_path_from_command(self, cmd: str) -> str:
        """Selecciona la estrategia de extracción según el formato del comando (comillas o directo)."""
        if cmd.startswith('"'):
            return self._extract_quoted_path(cmd)
        parts: List[str] = cmd.split()
        return self._resolve_and_cache_path(parts[0]) if parts else ""
        
    @property
    def executable(self) -> str:
        """
        Resuelve el ejecutable principal mediante caché. 
        Retorna la ruta absoluta si existe, o el comando original si no pudo resolverse.
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
    """Identifica las rutas de las carpetas 'Startup' (usuario y sistema) verificando existencia y seguridad."""
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
    except (ValueError, TypeError):
        pass
    return [c for c in candidates if c.is_dir() and not is_protected_path(c)]


def entries_from_folders(folders: Optional[Sequence[Path]] = None) -> List[StartupEntry]:
    """Escanea las carpetas de inicio filtrando archivos ejecutables y evitando rutas protegidas."""
    if folders is None:
        folders = startup_folders()
    found_entries: List[StartupEntry] = []
    for folder in folders:
        try:
            for item in folder.iterdir():
                if item.is_file() and item.suffix.lower() in EXECUTABLE_EXTS and not item.is_symlink():
                    if not is_protected_path(item):
                        found_entries.append(StartupEntry(name=item.stem, command=str(item), source="carpeta"))
        except (OSError, PermissionError, RuntimeError):
            continue
    return found_entries


def parse_registry_csv(text: str, source: str = "registro") -> List[StartupEntry]:
    """
    Parsea la salida cruda de PowerShell (CSV) en objetos StartupEntry.
    Ignora claves del sistema o campos de metadatos de PowerShell ('PS*').
    """
    if not isinstance(text, str) or not text.strip():
        return []
        
    parsed_entries: List[StartupEntry] = []
    lines: List[str] = text.splitlines()
    if len(lines) < 2:
        return []
        
    for line in lines[1:]:
        if not line or not line.strip():
            continue
        parts: List[str] = line.split(",", 1)
        if len(parts) < 2:
            continue
            
        name_raw = parts[0].strip().strip('"')
        cmd_raw = parts[1].strip().strip('"')
        
        name: str = "".join(c for c in name_raw if ord(c) >= 32)
        cmd: str = "".join(c for c in cmd_raw if ord(c) >= 32)
        
        if not name or name.lower() in ("name", "pscustomobject") or name.upper().startswith("PS"):
            continue
        
        if not cmd or any(c in cmd for c in '<>|?*'):
            continue
            
        try:
            p: Path = Path(cmd)
            if is_protected_path(p):
                continue
            parsed_entries.append(StartupEntry(name=name, command=cmd, source=source))
        except (OSError, ValueError, TypeError):
            continue
            
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """
    Consulta al registro de Windows mediante PowerShell (solo lectura).
    El timeout de 30s evita bloqueos en caso de que PowerShell no responda.
    """
    if os.name != "nt":
        return []
    
    ps_cmd: str = "; ".join(f"Get-ItemProperty '{k}' -ErrorAction SilentlyContinue | Select-Object * -ExcludeProperty PS*" for k in keys)
    ps_cmd = f"$data = {ps_cmd}; $data | ConvertTo-Csv -NoTypeInformation"
    
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            return parse_registry_csv(result.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def list_startup_entries() -> List[StartupEntry]:
    """Consolida las entradas de carpetas y registro, eliminando duplicados por nombre."""
    seen_names: set[str] = set()
    unique_entries: List[StartupEntry] = []
    
    for entry in entries_from_folders():
        if entry.name.lower() not in seen_names:
            seen_names.add(entry.name.lower())
            unique_entries.append(entry)
            
    for entry in entries_from_registry():
        if entry.name.lower() not in seen_names:
            seen_names.add(entry.name.lower())
            unique_entries.append(entry)
            
    return unique_entries


def estimate_impact(entries: Sequence[StartupEntry]) -> str:
    """Clasifica el impacto en el rendimiento basado en la cantidad de elementos detectados."""
    count: int = len(entries)
    thresholds: List[Tuple[int, str]] = [(5, "ok"), (10, "info"), (18, "warning")]
    for limit, label in thresholds:
        if count <= limit:
            return label
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """Genera un informe descriptivo y legible para el usuario final sobre el estado del inicio."""
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
