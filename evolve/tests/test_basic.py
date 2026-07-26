"""
test_basic.py
Estos tests son el "portero" del bucle autónomo: cualquier cambio que
Gemini proponga en app/ se acepta SOLO si esto sigue pasando. Sin esto,
el bucle no tiene forma de saber si "mejoró" o rompió algo.

Importante: `app/main.py` NO se puede importar acá (necesita customtkinter
y una pantalla real, que no existen en GitHub Actions). Su protección vive
en evolve/guards.py, que valida sintaxis y símbolos sin importar el módulo.

Agregá más tests acá a medida que el proyecto crezca — cuantos más
tests, más confiable es el auto-mejorado.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import organizer  # noqa: E402
import scanner    # noqa: E402


# --------------------------------------------------------------------------
# Contrato público: estas funciones tienen que seguir existiendo
# --------------------------------------------------------------------------

def test_organizer_imports():
    assert hasattr(organizer, "scan_for_junk")
    assert hasattr(organizer, "stage_for_review")


def test_organizer_public_api_is_complete():
    for name in ("scan_for_junk", "sort_junk", "stage_for_review",
                 "delete_reviewed", "list_available_drives", "JunkFile"):
        assert hasattr(organizer, name), f"falta {name} en organizer"


def test_scanner_public_api_is_complete():
    for name in ("scan_file", "scan_directory", "check_double_extension",
                 "check_system_lookalike", "run_windows_defender_quick_scan"):
        assert hasattr(scanner, name), f"falta {name} en scanner"


# --------------------------------------------------------------------------
# organizer: comportamiento real sobre archivos de prueba
# --------------------------------------------------------------------------

def test_sort_junk_empty_list_does_not_crash():
    assert organizer.sort_junk([]) == []


def test_scan_for_junk_finds_junk_and_ignores_other_files(tmp_path):
    (tmp_path / "basura.tmp").write_text("x")
    (tmp_path / "registro.log").write_text("y")
    (tmp_path / "importante.docx").write_text("z")

    found = organizer.scan_for_junk([str(tmp_path)])
    names = {f.path.name for f in found}

    assert "basura.tmp" in names
    assert "registro.log" in names
    assert "importante.docx" not in names, "no debe marcar archivos que no son basura"


def test_scan_for_junk_recurses_into_subfolders(tmp_path):
    sub = tmp_path / "nivel1" / "nivel2"
    sub.mkdir(parents=True)
    (sub / "profundo.tmp").write_text("x")

    found = organizer.scan_for_junk([str(tmp_path)])
    assert any(f.path.name == "profundo.tmp" for f in found)


def test_scan_for_junk_skips_system_folders(tmp_path):
    system_dir = tmp_path / "Windows"
    system_dir.mkdir()
    (system_dir / "critico.tmp").write_text("x")
    (tmp_path / "normal.tmp").write_text("y")

    found = organizer.scan_for_junk([str(tmp_path)])
    names = {f.path.name for f in found}

    assert "normal.tmp" in names
    assert "critico.tmp" not in names, "nunca debe entrar a carpetas de sistema"


def test_scan_for_junk_handles_missing_path_without_crashing(tmp_path):
    assert organizer.scan_for_junk([str(tmp_path / "no-existe")]) == []


def test_scan_for_junk_blocklist_covers_critical_folders():
    for folder in ("windows", "program files", "program files (x86)"):
        assert folder in organizer.SYSTEM_FOLDER_BLOCKLIST


def _junk(name: str, size: int, days_old: int) -> "organizer.JunkFile":
    return organizer.JunkFile(
        path=Path(name),
        size_bytes=size,
        modified=datetime.now() - timedelta(days=days_old),
    )


def test_sort_junk_by_size_ascending_and_descending():
    files = [_junk("grande", 300, 1), _junk("chico", 100, 1), _junk("medio", 200, 1)]

    asc = organizer.sort_junk(files, by="size", ascending=True)
    assert [f.path.name for f in asc] == ["chico", "medio", "grande"]

    desc = organizer.sort_junk(files, by="size", ascending=False)
    assert [f.path.name for f in desc] == ["grande", "medio", "chico"]


def test_sort_junk_by_date_puts_oldest_first_when_ascending():
    files = [_junk("nuevo", 100, 1), _junk("viejo", 100, 90)]
    asc = organizer.sort_junk(files, by="date", ascending=True)
    assert [f.path.name for f in asc] == ["viejo", "nuevo"]


def test_sort_junk_does_not_mutate_the_original_list():
    files = [_junk("b", 200, 1), _junk("a", 100, 1)]
    original_order = [f.path.name for f in files]
    organizer.sort_junk(files, by="size")
    assert [f.path.name for f in files] == original_order


def test_junkfile_size_mb_conversion():
    assert organizer.JunkFile(Path("x.tmp"), 1024 * 1024, datetime.now()).size_mb == 1.0


def test_stage_for_review_moves_files_without_deleting_them(tmp_path):
    origen = tmp_path / "origen"
    origen.mkdir()
    archivo = origen / "mover.tmp"
    archivo.write_text("contenido")
    revision = tmp_path / "revision"

    found = organizer.scan_for_junk([str(origen)])
    dest = organizer.stage_for_review(found, review_dir=str(revision))

    assert not archivo.exists(), "el archivo debe salir de su lugar original"
    movidos = list(dest.iterdir())
    assert len(movidos) == 1
    assert movidos[0].read_text() == "contenido", "el contenido no debe perderse"


def test_delete_reviewed_only_touches_the_review_folder(tmp_path):
    revision = tmp_path / "revision"
    revision.mkdir()
    (revision / "descartado.tmp").write_text("x")
    afuera = tmp_path / "no-tocar.tmp"
    afuera.write_text("y")

    borrados = organizer.delete_reviewed(review_dir=str(revision))

    assert borrados == 1
    assert afuera.exists(), "nunca debe borrar nada fuera de la carpeta de revisión"


def test_delete_reviewed_on_missing_folder_returns_zero(tmp_path):
    assert organizer.delete_reviewed(review_dir=str(tmp_path / "no-existe")) == 0


def test_list_available_drives_does_not_crash():
    # No debe fallar sin importar el sistema operativo, solo puede
    # devolver una lista vacía en no-Windows.
    assert isinstance(organizer.list_available_drives(), list)


def test_organizer_never_exposes_a_direct_delete_all():
    # Guardrail explícito: nos aseguramos de que nadie (ni la IA
    # "mejorando" el código) agregue una función que borre en masa
    # sin pasar por la carpeta de revisión.
    import inspect
    source = inspect.getsource(organizer)
    assert "shutil.rmtree" not in source


# --------------------------------------------------------------------------
# scanner: heurísticas
# --------------------------------------------------------------------------

def test_scanner_double_extension_detection():
    result = scanner.check_double_extension(Path("factura.pdf.exe"))
    assert result is not None
    assert result.severity == "warning"


def test_scanner_normal_file_is_clean():
    assert scanner.check_double_extension(Path("factura.pdf")) is None


def test_scanner_flags_system_lookalike_outside_system32():
    # Se usa PureWindowsPath a propósito: los tests corren en Linux (GitHub
    # Actions) y ahí un Path normal no reconoce las barras invertidas, así
    # que `.name` devolvería la ruta entera y el test fallaría siempre.
    result = scanner.check_system_lookalike(PureWindowsPath(r"C:\Users\test\Downloads\svchost.exe"))
    assert result is not None
    assert result.severity == "warning"


def test_scanner_does_not_flag_real_system_file():
    assert scanner.check_system_lookalike(PureWindowsPath(r"C:\Windows\System32\svchost.exe")) is None


def test_scanner_lookalike_logic_is_os_independent():
    # La misma heurística tiene que valer con rutas estilo POSIX, para que el
    # resultado no dependa de en qué sistema corran los tests.
    flagged = scanner.check_system_lookalike(PurePosixPath("/home/user/Downloads/svchost.exe"))
    assert flagged is not None and flagged.severity == "warning"


def test_scan_directory_returns_list_on_empty_folder(tmp_path):
    assert scanner.scan_directory(str(tmp_path)) == []


# --------------------------------------------------------------------------
# guards: la red que protege lo que los tests no pueden importar
# --------------------------------------------------------------------------

def test_guard_rejects_syntax_errors():
    ok, reason = __import__("guards").validate_change("def a():\n    return 1\n", "def a(:\n", "x.py")
    assert not ok
    assert "sintaxis" in reason


def test_guard_rejects_removing_an_existing_function():
    guards = __import__("guards")
    original = "def uno():\n    return 1\n\n\ndef dos():\n    return 2\n"
    nuevo = "def uno():\n    return 1\n"
    ok, reason = guards.validate_change(original, nuevo, "x.py")
    assert not ok


def test_guard_accepts_adding_a_new_function():
    guards = __import__("guards")
    original = "def uno():\n    return 1\n"
    nuevo = "def uno():\n    return 1\n\n\ndef dos():\n    return 2\n"
    ok, _ = guards.validate_change(original, nuevo, "x.py")
    assert ok


def test_guard_rejects_removing_a_class_method():
    guards = __import__("guards")
    original = "class A:\n    def m(self):\n        pass\n\n    def _priv(self):\n        pass\n"
    nuevo = "class A:\n    def m(self):\n        pass\n"
    ok, _ = guards.validate_change(original, nuevo, "x.py")
    assert not ok


def test_guard_rejects_empty_file():
    ok, _ = __import__("guards").validate_change("def a():\n    return 1\n", "   \n", "x.py")
    assert not ok


# --------------------------------------------------------------------------
# tracking: la rotación debe cubrir toda la matriz archivo x enfoque
# --------------------------------------------------------------------------

def test_rotation_covers_every_file_and_category_combination():
    tracking = __import__("tracking")
    files = ["a.py", "b.py", "c.py"]
    cats = ["c1", "c2"]
    seen = {tracking.pick_assignment(i, files, cats) for i in range(len(files) * len(cats))}
    assert len(seen) == len(files) * len(cats), "la rotación no debe repetir antes de cubrir todo"


def test_rotation_wraps_around_after_full_cycle():
    tracking = __import__("tracking")
    files, cats = ["a.py", "b.py"], ["c1", "c2"]
    total = len(files) * len(cats)
    assert tracking.pick_assignment(0, files, cats) == tracking.pick_assignment(total, files, cats)
