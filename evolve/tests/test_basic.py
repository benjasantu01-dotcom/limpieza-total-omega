"""
test_basic.py
Estos tests son el "portero" del bucle autónomo: cualquier cambio que
Gemini proponga en app/ se acepta SOLO si esto sigue pasando. Sin esto,
el bucle no tiene forma de saber si "mejoró" o rompió algo.

Agregá más tests acá a medida que el proyecto crezca — cuantos más
tests, más confiable es el auto-mejorado.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))

import organizer  # noqa: E402
import scanner    # noqa: E402


def test_organizer_imports():
    assert hasattr(organizer, "scan_for_junk")
    assert hasattr(organizer, "stage_for_review")


def test_sort_junk_empty_list_does_not_crash():
    assert organizer.sort_junk([]) == []


def test_scanner_double_extension_detection():
    from pathlib import Path as P
    result = scanner.check_double_extension(P("factura.pdf.exe"))
    assert result is not None
    assert result.severity == "warning"


def test_scanner_normal_file_is_clean():
    from pathlib import Path as P
    result = scanner.check_double_extension(P("factura.pdf"))
    assert result is None


def test_organizer_never_exposes_a_direct_delete_all():
    # Guardrail explícito: nos aseguramos de que nadie (ni la IA
    # "mejorando" el código) agregue una función que borre en masa
    # sin pasar por la carpeta de revisión.
    import inspect
    source = inspect.getsource(organizer)
    assert "shutil.rmtree" not in source
