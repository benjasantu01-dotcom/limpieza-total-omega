"""
startup.py — inventario de programas que arrancan con Windows.

SOLO LECTURA: lista lo que arranca con el sistema y estima su impacto, pero
**no deshabilita ni borra nada**. Tocar las claves de arranque del registro
de forma automática es una de las maneras más rápidas de dejar una PC en
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
from typing import Iterable, Optional, Iterator, List, Tuple, Dict

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

    @property
    def executable(self) -> str:
        """
        Extrae la ruta base del ejecutable a partir de la línea de comando.
        
        Lógica:
        1. Si la línea empieza con comillas, extrae el contenido hasta la siguiente comilla.
        2. Si no tiene comillas, retorna el primer token (separado por espacios).
        """
        raw_cmd: str = self.command.strip()
        if not raw_cmd:
            return ""
        
        # Caso 1: Ruta encapsulada en comillas (ej: "C:\App\prog.exe" /args)
        if raw_cmd.startswith('"'):
            end_quote: int = raw_cmd.find('"', 1)
            # Retorna el contenido entre comillas si es válido, sino el resto de la cadena
            return raw_cmd[1:end_quote] if end_quote != -1 else raw_cmd[1:]
        
        # Caso 2: Ruta simple (sin comillas)
        parts: List[str] = raw_cmd.split()
        return parts[0] if parts else ""


def startup_folders() -> List[Path]:
    """
    Retorna las rutas a las carpetas 'Inicio' (usuario y sistema) 
    verificando la existencia de los directorios en el sistema de archivos.
    """
    if os.name != "nt":
        return []
    candidates: List[Path] = []
    appdata: Optional[str] = os.environ.get("APPDATA")
    programdata: Optional[str] = os.environ.get("ProgramData")
    if appdata:
        candidates.append(Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup")
    if programdata:
        candidates.append(Path(programdata) / r"Microsoft\Windows\Start Menu\Programs\Startup")
    return [c for c in candidates if c.is_dir()]


def entries_from_folders(folders: Optional[Iterable[Path]] = None) -> List[StartupEntry]:
    """
    Escanea las carpetas de inicio en busca de archivos (ejecutables/accesos directos).

    Seguridad: Filtra 'desktop.ini' y symlinks para evitar ciclos. Verifica mediante
    `resolve()` que el archivo encontrado permanezca efectivamente dentro de la 
    carpeta base, evitando escapes mediante rutas relativas complejas.
    """
    if folders is None:
        folders = startup_folders()
    found_entries: List[StartupEntry] = []
    for folder in folders:
        try:
            base_path: Path = folder.resolve()
            if not base_path.exists():
                continue
        except (ValueError, PermissionError, OSError):
            continue

        try:
            for item in base_path.iterdir():
                try:
                    # Filtramos symlinks/junctions por seguridad y desktop.ini
                    if item.is_file() and not item.is_symlink() and item.name.lower() != "desktop.ini":
                        if base_path in item.resolve().parents:
                            found_entries.append(StartupEntry(name=item.stem, command=str(item), source="carpeta"))
                except (OSError, PermissionError, RuntimeError):
                    continue
        except (OSError, PermissionError):
            continue
    return found_entries


def parse_registry_csv(text: str, source: str = "registro") -> List[StartupEntry]:
    """
    Transforma el volcado CSV de PowerShell en objetos StartupEntry.
    
    Espera formato CSV generado por `ConvertTo-Csv`. Filtra las cabeceras estándar 
    de PowerShell y limpia las comillas de encapsulamiento que el motor de 
    registro suele incluir en los valores de cadena.
    """
    parsed_entries: List[StartupEntry] = []
    if not isinstance(text, str) or not text.strip():
        return parsed_entries
        
    for line in text.splitlines():
        clean_line: str = line.strip()
        if not clean_line:
            continue
        csv_row_parts: List[str] = line.split(",", 1)
        if len(csv_row_parts) < 2:
            continue
            
        name_raw: str = csv_row_parts[0].strip().strip('"').strip("'")
        value_raw: str = csv_row_parts[1].strip().strip('"').strip("'")
        
        # Validar que el nombre no sea una cabecera de sistema ignorando case
        if not name_raw or name_raw.lower() in ("name", "pscustomobject") or name_raw.upper().startswith("PS"):
            continue
        parsed_entries.append(StartupEntry(name=name_raw, command=value_raw, source=source))
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """
    Obtiene programas de inicio consultando las llaves del Registro vía PowerShell.
    
    Ejecuta un comando de PowerShell para extraer propiedades de las llaves 
    Run y las convierte a CSV para parseo seguro.
    """
    if os.name != "nt":
        return []
    all_entries: List[StartupEntry] = []
    
    for key in keys:
        if not key or not isinstance(key, str):
            continue
            
        # Escapado para PowerShell usando list2cmdline para evitar inyección en el comando
        safe_key: str = subprocess.list2cmdline([key])
        ps_cmd: str = (
            f"if (Test-Path {safe_key}) {{ (Get-Item {safe_key}).Property | ForEach-Object "
            f"{{ [PSCustomObject]@{{ Name = $_; Value = (Get-ItemProperty {safe_key}).$_ }} }} | "
            "ConvertTo-Csv -NoTypeInformation }"
        )
        try:
            result: subprocess.CompletedProcess = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                all_entries.extend(parse_registry_csv(result.stdout, source=key))
        except (OSError, subprocess.SubprocessError):
            continue
    return all_entries


def list_startup_entries() -> List[StartupEntry]:
    """Retorna una lista consolidada de programas, eliminando duplicados por nombre."""
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


def estimate_impact(entries: Iterable[StartupEntry]) -> str:
    """Clasifica el impacto en el rendimiento basado en la cantidad total de programas."""
    if entries is None:
        return "ok"
    total_count: int = sum(1 for _ in entries)
    if total_count <= 5:
        return "ok"
    if total_count <= 10:
        return "info"
    if total_count <= 18:
        return "warning"
    return "danger"


def summarize(entries: Optional[Iterable[StartupEntry]] = None) -> List[str]:
    """Genera un informe legible y estructurado de los programas de inicio detectados."""
    entries_list: List[StartupEntry] = list(entries) if entries is not None else list_startup_entries()
        
    lines: List[str] = [f"Programas que arrancan con el sistema: {len(entries_list)}"]
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
    if entries_list:
        lines.extend(["", HOW_TO_DISABLE])
    return lines
