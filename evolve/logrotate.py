"""
logrotate.py — rotación de logs del bucle autónomo.

POR QUÉ HACE FALTA
------------------
El bucle corre 24/7 y escribe en `evolve_log.md` y `evolve/metrics.jsonl`
en cada iteración. A ~4 mejoras por corrida durante una semana, esos
archivos crecen sin techo y cada commit del bot los vuelve a subir enteros.
Sin rotación, el repo se infla y el log se vuelve ilegible.

Esto recorta los archivos dejando lo reciente y archiva el resto en
`evolve/archive/`, así no se pierde historia pero el archivo activo se
mantiene chico.

SEGURIDAD: solo toca archivos dentro de la carpeta del proyecto, y solo
borra dentro de `evolve/archive/`. Se verifica la contención de rutas antes
de cada borrado (`_is_within`), para que un parámetro mal formado no pueda
borrar nada afuera. No importa `app/safety.py` a propósito: este módulo
tiene que funcionar aunque el bucle esté en medio de reescribir `app/`.
"""

from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

__all__ = [
    "MAX_LOG_BYTES",
    "KEEP_RECENT_LINES",
    "KEEP_RECENT_METRICS",
    "MAX_ARCHIVE_FILES",
    "ARCHIVE_DIR_NAME",
    "rotate_text_log",
    "rotate_jsonl",
    "prune_archives",
    "rotate_all",
    "summarize",
]

# Umbral a partir del cual se rota el log de texto (256 KB).
MAX_LOG_BYTES = 256 * 1024
# Cuántas líneas recientes quedan en el archivo activo tras rotar.
KEEP_RECENT_LINES = 400
# Cuántos registros recientes quedan en metrics.jsonl tras rotar.
KEEP_RECENT_METRICS = 500
# Cuántos archivos históricos se conservan antes de descartar los más viejos.
MAX_ARCHIVE_FILES = 12
ARCHIVE_DIR_NAME = "archive"


def _is_within(child: Path, parent: Path) -> bool:
    """True si `child` está realmente dentro de `parent`, ya resueltos.

    Se resuelven las dos rutas antes de comparar: comparar strings sin
    resolver es lo que permitiría que un ".." escape de la carpeta.
    """
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _archive_dir(base_dir: str | Path) -> Path:
    """Carpeta de archivos históricos, creada si no existe."""
    path = Path(base_dir) / ARCHIVE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def rotate_text_log(
    path: str | Path,
    max_bytes: int = MAX_LOG_BYTES,
    keep_lines: int = KEEP_RECENT_LINES,
    archive_dir: str | Path | None = None,
) -> dict:
    """Rota un log de texto si superó `max_bytes`.

    Devuelve un dict con qué pasó, en vez de lanzar excepciones: la
    rotación es mantenimiento, nunca debe abortar una corrida del bucle.
    """
    log_path = Path(path)
    result = {"file": str(log_path), "rotated": False, "archived_lines": 0, "archive": None}

    if not log_path.is_file():
        result["reason"] = "no existe"
        return result

    try:
        if log_path.stat().st_size <= max_bytes:
            result["reason"] = "todavía es chico"
            return result

        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        if len(lines) <= keep_lines:
            result["reason"] = "pocas líneas para recortar"
            return result

        destination = Path(archive_dir) if archive_dir else _archive_dir(log_path.parent)
        destination.mkdir(parents=True, exist_ok=True)

        viejo, reciente = lines[:-keep_lines], lines[-keep_lines:]
        archivo = destination / f"{log_path.stem}-{_timestamp()}{log_path.suffix}"
        archivo.write_text("".join(viejo), encoding="utf-8")

        encabezado = (
            f"<!-- Log rotado el {datetime.now():%Y-%m-%d %H:%M:%S}. "
            f"Las {len(viejo)} líneas anteriores están en "
            f"{ARCHIVE_DIR_NAME}/{archivo.name} -->\n\n"
        )
        log_path.write_text(encabezado + "".join(reciente), encoding="utf-8")

        result.update(rotated=True, archived_lines=len(viejo), archive=str(archivo))
        return result
    except OSError as e:
        result["reason"] = f"error de E/S: {e}"
        return result


def rotate_jsonl(
    path: str | Path,
    keep_records: int = KEEP_RECENT_METRICS,
    archive_dir: str | Path | None = None,
) -> dict:
    """Rota un archivo JSONL dejando los últimos `keep_records` registros.

    Las líneas que no sean JSON válido se conservan igual en el archivo
    histórico: se prefiere guardar algo corrupto antes que descartarlo.
    """
    jsonl_path = Path(path)
    result = {"file": str(jsonl_path), "rotated": False, "archived_records": 0, "archive": None}

    if not jsonl_path.is_file():
        result["reason"] = "no existe"
        return result

    try:
        lines = [l for l in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        if len(lines) <= keep_records:
            result["reason"] = "pocos registros para recortar"
            return result

        destination = Path(archive_dir) if archive_dir else _archive_dir(jsonl_path.parent)
        destination.mkdir(parents=True, exist_ok=True)

        viejo, reciente = lines[:-keep_records], lines[-keep_records:]
        archivo = destination / f"{jsonl_path.stem}-{_timestamp()}.jsonl"
        archivo.write_text("\n".join(viejo) + "\n", encoding="utf-8")
        jsonl_path.write_text("\n".join(reciente) + "\n", encoding="utf-8")

        result.update(rotated=True, archived_records=len(viejo), archive=str(archivo))
        return result
    except OSError as e:
        result["reason"] = f"error de E/S: {e}"
        return result


def prune_archives(archive_dir: str | Path, keep_files: int = MAX_ARCHIVE_FILES) -> int:
    """Borra los archivos históricos más viejos. Devuelve cuántos borró.

    Solo borra archivos que estén realmente dentro de `archive_dir`; si
    algo quedara fuera (enlace, ruta rara), se saltea sin tocarlo.
    """
    directory = Path(archive_dir)
    if not directory.is_dir():
        return 0

    try:
        archivos = [f for f in directory.iterdir() if f.is_file()]
    except OSError:
        return 0

    if len(archivos) <= keep_files:
        return 0

    archivos.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0)
    borrados = 0
    for viejo in archivos[: len(archivos) - keep_files]:
        if not _is_within(viejo, directory):
            continue  # nunca borrar fuera de la carpeta de archivo
        try:
            viejo.unlink()
            borrados += 1
        except OSError:
            continue
    return borrados


def rotate_all(repo_root: str | Path = ".", evolve_dir_name: str = "evolve") -> dict:
    """Rota todos los logs del proyecto y limpia archivos históricos viejos.

    Es lo que llama el bucle al final de cada corrida. Devuelve un resumen
    para poder loguear qué se rotó.
    """
    root = Path(repo_root)
    evolve_dir = root / evolve_dir_name
    archive = _archive_dir(evolve_dir)

    return {
        "log": rotate_text_log(root / "evolve_log.md", archive_dir=archive),
        "metrics": rotate_jsonl(evolve_dir / "metrics.jsonl", archive_dir=archive),
        "pruned": prune_archives(archive),
        "archive_dir": str(archive),
    }


def summarize(result: dict) -> str:
    """Resumen de una línea de lo que hizo `rotate_all`, para el log."""
    partes = []
    log = result.get("log", {})
    metrics = result.get("metrics", {})
    if log.get("rotated"):
        partes.append(f"log: {log['archived_lines']} líneas archivadas")
    if metrics.get("rotated"):
        partes.append(f"metrics: {metrics['archived_records']} registros archivados")
    if result.get("pruned"):
        partes.append(f"{result['pruned']} archivo(s) histórico(s) descartado(s)")
    return "Rotación — " + ("; ".join(partes) if partes else "nada para rotar")
