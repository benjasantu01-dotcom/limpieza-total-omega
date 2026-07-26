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
from app.safety import ensure_safe_to_modify

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
        """Extrae la ruta base del ejecutable a partir de la línea de comando.
        
        Elimina comillas, ignora argumentos adicionales y retorna el token inicial,
        que es el ejecutable propiamente dicho. Retorna cadena vacía si no hay comando.
        """
        if not self.command:
            return ""
        
        cmd: str = self.command.strip()
        cmd = cmd.strip('"')
        
        if not cmd:
            return ""
            
        # Si el comando original estaba entre comillas, extraemos el contenido hasta la segunda comilla
        if self.command.strip().startswith('"'):
            end_quote_idx: int = self.command.find('"', 1)
            return self.command[1:end_quote_idx] if end_quote_idx > 0 else self.command[1:]
        
        # Caso sin comillas: el ejecutable es el primer segmento de la ruta o comando
        tokens = cmd.split()
        return tokens[0] if tokens else ""


def startup_folders() -> List[Path]:
    """Retorna las rutas a las carpetas 'Inicio' (usuario y sistema) verificando existencia."""
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
    """Escanea las carpetas de inicio en busca de archivos (accesos directos o ejecutables).
    
    Aplica `ensure_safe_to_modify` para cumplir con las políticas de seguridad
    y filtra archivos de sistema irrelevantes como 'desktop.ini'.
    """
    if folders is None:
        folders = startup_folders()
    found_entries: List[StartupEntry] = []
    for folder in folders:
        try:
            ensure_safe_to_modify(folder)
            base_path: Path = folder.resolve()
            if not base_path.exists():
                continue
        except (ValueError, PermissionError, OSError):
            continue

        try:
            for item in base_path.iterdir():
                try:
                    if item.is_file() and item.name.lower() != "desktop.ini":
                        # Validación de seguridad: confirma que el objeto está bajo la ruta base
                        if base_path in item.resolve().parents:
                            found_entries.append(StartupEntry(name=item.stem, command=str(item), source="carpeta"))
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            continue
    return found_entries


def parse_registry_csv(text: str, source: str = "registro") -> List[StartupEntry]:
    """Procesa el volcado CSV de PowerShell a objetos StartupEntry.
    
    Elimina cabeceras de PowerShell y limpia los valores obtenidos del registro.
    """
    parsed_entries: List[StartupEntry] = []
    if not text:
        return parsed_entries
        
    for line in text.splitlines():
        clean_line = line.strip()
        if not clean_line:
            continue
        # La estructura CSV esperada es: "NombrePropiedad","Valor"
        csv_row_parts: List[str] = clean_line.split(",", 1)
        if len(csv_row_parts) < 2:
            continue
            
        name: str = csv_row_parts[0].strip().strip('"').strip("'")
        value: str = csv_row_parts[1].strip().strip('"').strip("'")
        
        # Omite metadatos propios del formato de salida de PowerShell
        if not name or name.lower() == "name" or name.startswith("PS"):
            continue
        parsed_entries.append(StartupEntry(name=name, command=value, source=source))
    return parsed_entries


def entries_from_registry(keys: Iterable[str] = REGISTRY_RUN_KEYS) -> List[StartupEntry]:
    """Obtiene programas de inicio consultando las llaves del Registro vía PowerShell.
    
    Construye un comando de PowerShell para leer propiedades como objetos y 
    convertirlos a CSV para facilitar su parseo posterior.
    """
    if os.name != "nt":
        return []
    all_entries: List[StartupEntry] = []
    for key in keys:
        ps_cmd: str = (
            f"if (Test-Path '{key}') {{ (Get-Item '{key}').Property | ForEach-Object "
            f"{{ [PSCustomObject]@{{ Name = $_; Value = (Get-ItemProperty '{key}').$_ }} }} | "
            "ConvertTo-Csv -NoTypeInformation }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                all_entries.extend(parse_registry_csv(result.stdout, source=key))
        except (OSError, subprocess.SubprocessError):
            continue
    return all_entries


def list_startup_entries() -> List[StartupEntry]:
    """Agrega todas las entradas detectadas filtrando duplicados por nombre (case-insensitive)."""
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
    total_count: int = sum(1 for _ in entries)
    if total_count <= 5:
        return "ok"
    if total_count <= 10:
        return "info"
    if total_count <= 18:
        return "warning"
    return "danger"


def summarize(entries: Optional[Iterable[StartupEntry]] = None) -> List[str]:
    """Genera un reporte legible de los programas de inicio detectados."""
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
