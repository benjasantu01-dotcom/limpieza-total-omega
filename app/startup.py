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
        """Verifica si la ruta apunta a un ejecutable reconocido o existe físicamente."""
        try:
            return path.suffix.lower() in ('.exe', '.bat', '.cmd', '.scr') or path.exists()
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _extract_quoted_path(self, raw_cmd: str) -> str:
        """
        Extrae la ruta de un ejecutable envuelto en comillas dobles.
        Retorna la ruta absoluta como string si es válida, o cadena vacía.
        """
        end_quote: int = raw_cmd.find('"', 1)
        if end_quote == -1:
            return ""
        path_str = raw_cmd[1:end_quote]
        if not path_str or any(c in path_str for c in '<>|?*'):
            return ""
        
        try:
            path = Path(path_str).expanduser()
            return str(path) if self._is_valid_executable(path) else ""
        except (OSError, ValueError, RuntimeError, TypeError):
            return ""
        
    @property
    def executable(self) -> str:
        """
        Normaliza la ruta del ejecutable analizando el comando de inicio.
        Utiliza cache interno para evitar llamadas repetidas al sistema de archivos.
        """
        if self._checked_exists:
            return self._exec_cache or ""
            
        if not self.command:
            self._checked_exists = True
            return ""

        # Limpieza de caracteres no imprimibles del comando crudo
        cmd: str = "".join(c for c in self.command.strip() if ord(c) >= 32)
        if not cmd:
            self._checked_exists = True
            return ""
        
        self._checked_exists = True
        if cmd.startswith('"'):
            self._exec_cache = self._extract_quoted_path(cmd)
            return self._exec_cache
        
        parts: List[str] = cmd.split()
        if not parts:
            return ""
            
        try:
            path = Path(parts[0]).expanduser()
            # Validar si existe antes de intentar resolver o convertir
            self._exec_cache = str(path) if path.exists() else parts[0]
            return self._exec_cache
        except (OSError, ValueError, RuntimeError, TypeError):
            self._exec_cache = parts[0]
            return self._exec_cache


def startup_folders() -> List[Path]:
    """Retorna las rutas a las carpetas 'Inicio' (usuario y sistema)."""
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
    return [c for c in candidates if c.is_dir()]


def entries_from_folders(folders: Optional[Sequence[Path]] = None) -> List[StartupEntry]:
    """Escanea las carpetas de inicio detectando ejecutables accesibles."""
    if folders is None:
        folders = startup_folders()
    found_entries: List[StartupEntry] = []
    for folder in folders:
        if is_protected_path(folder):
            continue
            
        try:
            if not folder.exists() or not folder.is_dir():
                continue
            base_path: Path = folder.resolve()
        except (ValueError, PermissionError, OSError, RuntimeError):
            continue

        try:
            for item in base_path.iterdir():
                try:
                    if not item.name or item.name.lower() == "desktop.ini":
                        continue
                    # No seguir enlaces simbólicos para prevenir escapes del directorio
                    if item.is_file() and not item.is_symlink():
                        if is_protected_path(item):
                            continue
                        
                        resolved_item: Path = item.resolve()
                        if base_path == resolved_item.parent:
                            found_entries.append(StartupEntry(name=item.stem, command=str(item), source="carpeta"))
                except (OSError, PermissionError, RuntimeError):
                    continue
        except (OSError, PermissionError, RuntimeError):
            continue
    return found_entries


def parse_registry_csv(text: str, source: str = "registro") -> List[StartupEntry]:
    """
    Procesa el output crudo de PowerShell en formato CSV.
    Aplica filtros de seguridad sobre la existencia y protección de las rutas.
    """
    parsed_entries: List[StartupEntry] = []
    if not isinstance(text, str) or not text.strip():
        return parsed_entries
        
    for line in text.splitlines():
        clean_line: str = line.strip()
        if not clean_line:
            continue
            
        columns: List[str] = clean_line.split(",", 1)
        if len(columns) < 2:
            continue
            
        name_key: str = columns[0].strip().strip('"\'')
        value_cmd: str = columns[1].strip().strip('"\'')
        
        # Validar que los valores extraídos sean strings útiles y no cabeceras de PS
        if not name_key or name_key.lower() in ("name", "pscustomobject") or name_key.upper().startswith("PS"):
            continue
            
        entry = StartupEntry(name=name_key, command=value_cmd, source=source)
        
        try:
            executable_path = entry.executable
            # Validar existencia solo si la ruta parece absoluta
            if executable_path and os.path.isabs(executable_path) and os.path.exists(executable_path):
                if is_protected_path(Path(executable_path)):
                    continue
        except (OSError, ValueError, TypeError):
            continue
                
        parsed_entries.append(entry)
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """
    Ejecuta consultas de PowerShell para extraer entradas del Registro de Windows.
    Utiliza un comando unificado para minimizar el número de procesos externos.
    """
    if os.name != "nt":
        return []
    
    query_parts: List[str] = []
    for key in keys:
        if isinstance(key, str):
            safe_key: str = subprocess.list2cmdline([key])
            query_parts.append(f"try {{ (Get-ItemProperty {safe_key} -ErrorAction SilentlyContinue).psobject.properties | Select-Object Name, Value | ConvertTo-Csv -NoTypeInformation }} catch {{ }}")
    
    ps_cmd: str = " ; ".join(query_parts)
    
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
    """Consolida las entradas provenientes de carpetas del sistema y del Registro."""
    seen_names: set[str] = set()
    unique_entries: List[StartupEntry] = []
    
    def _gen_entries() -> Iterator[StartupEntry]:
        yield from entries_from_folders()
        yield from entries_from_registry()

    for entry in _gen_entries():
        key: str = entry.name.lower()
        if key not in seen_names:
            seen_names.add(key)
            unique_entries.append(entry)
    return unique_entries


def estimate_impact(entries: Sequence[StartupEntry]) -> str:
    """Clasifica el impacto en el rendimiento: 'ok', 'info', 'warning' o 'danger'."""
    count: int = len(entries)
    thresholds: List[Tuple[int, str]] = [(5, "ok"), (10, "info"), (18, "warning")]
    for limit, label in thresholds:
        if count <= limit:
            return label
    return "danger"


def summarize(entries: Optional[Sequence[StartupEntry]] = None) -> List[str]:
    """
    Genera un informe detallado para la interfaz de usuario.
    Incluye un desglose por origen y recomendaciones de seguridad.
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
