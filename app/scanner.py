"""
scanner.py
Detector HEURÍSTICO de archivos sospechosos. Esto es un complemento
educativo/demostrativo, NO un antivirus real. Para protección seria,
este módulo se apoya en Windows Defender (ya instalado en Windows 11)
en vez de reinventar un motor de firmas.

Señales heurísticas que marca (no borra nada, solo informa):
- Doble extensión (ej. "factura.pdf.exe")
- Ejecutables en carpetas de descargas/temp recién creados
- Nombres que imitan archivos de sistema pero fuera de System32
"""

from __future__ import annotations
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta

DOUBLE_EXTENSION_RE = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
SUSPICIOUS_EXECUTABLE_EXT = {".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"}
SYSTEM_LOOKALIKES = {"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"}


@dataclass
class Suspicion:
    path: Path
    reason: str
    severity: str  # "info" | "warning"


def check_double_extension(path: Path) -> Suspicion | None:
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = 24) -> Suspicion | None:
    if path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except FileNotFoundError:
        return None
    if datetime.now() - mtime < timedelta(hours=hours):
        return Suspicion(path, "Ejecutable reciente en carpeta de descargas", "info")
    return None


def check_system_lookalike(path: Path) -> Suspicion | None:
    if path.name.lower() in SYSTEM_LOOKALIKES and "system32" not in str(path.parent).lower():
        return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None


def scan_file(path: Path) -> list[Suspicion]:
    checks = [check_double_extension, check_recent_executable_in_downloads, check_system_lookalike]
    results = []
    for check in checks:
        r = check(path)
        if r:
            results.append(r)
    return results


def scan_directory(directory: str) -> list[Suspicion]:
    results = []
    for p in Path(directory).rglob("*"):
        if p.is_file():
            results.extend(scan_file(p))
    return results


def run_windows_defender_quick_scan() -> str:
    """Dispara un escaneo rápido con Windows Defender (motor real),
    en vez de reinventar detección de malware. Requiere PowerShell
    con permisos adecuados. Devuelve la salida cruda del comando."""
    try:
        result = subprocess.run(
            ["powershell", "-Command", "Start-MpScan -ScanType QuickScan"],
            capture_output=True, text=True, timeout=1800,
        )
        return result.stdout or result.stderr
    except FileNotFoundError:
        return "PowerShell no disponible (¿estás en Windows?)."
    except subprocess.TimeoutExpired:
        return "El escaneo tardó demasiado y se canceló por timeout."
