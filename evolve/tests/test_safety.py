"""
test_safety.py — los tests que impiden que la app rompa el sistema.

Estos NO son tests de "funciona bien". Son el contrato de seguridad: si
alguno falla, el bucle autónomo rechaza el cambio y no se commitea nada.
Son la razón por la que la app puede reescribirse sola durante una semana
sin que exista un camino que borre algo crítico.

REGLA AL ESCRIBIR TESTS ACÁ: nunca usar rutas literales de Windows como
r"C:\\Windows". Los tests corren en Linux (GitHub Actions) y ahí esa string
es un nombre de archivo cualquiera, así que el test pasaría o fallaría por
el motivo equivocado. En su lugar se arman carpetas reales con
`tmp_path / "Windows"`, que funciona igual en los dos sistemas.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import guards  # noqa: E402
import quarantine  # noqa: E402
import safety  # noqa: E402


# --------------------------------------------------------------------------
# Contrato público de safety: estos nombres no pueden desaparecer
# --------------------------------------------------------------------------

def test_safety_public_api_is_complete():
    for name in ("UnsafePathError", "ensure_safe_to_modify", "is_safe_to_modify",
                 "is_protected_path",
                 "is_within_directory", "is_drive_root", "filter_safe_paths",
                 "is_sensitive_file", "normalize", "describe_protection",
                 "PROTECTED_DIR_NAMES", "SENSITIVE_EXTENSIONS"):
        assert hasattr(safety, name), f"falta {name} en safety"


def test_protected_list_covers_the_critical_windows_folders():
    # Si alguna de estas se saca de la lista, hay un camino para borrar
    # archivos de sistema. Es el test más importante del proyecto.
    for folder in ("windows", "system32", "program files", "program files (x86)",
                   "programdata", "$recycle.bin", "system volume information",
                   "syswow64", "winsxs", "recovery"):
        assert folder in safety.PROTECTED_DIR_NAMES, f"{folder} quedó sin proteger"


def test_sensitive_extensions_cover_executables_and_keys():
    for ext in (".sys", ".dll", ".exe", ".msi", ".drv", ".pem", ".pfx"):
        assert ext in safety.SENSITIVE_EXTENSIONS, f"{ext} quedó sin proteger"


# --------------------------------------------------------------------------
# Detección de rutas peligrosas
# --------------------------------------------------------------------------

def test_drive_root_is_always_protected(tmp_path):
    # tmp_path.anchor es "/" en Linux y "C:\\" en Windows: sirve en los dos.
    assert safety.is_drive_root(tmp_path.anchor)
    assert safety.is_protected_path(tmp_path.anchor)


def test_system_folders_are_detected_as_protected(tmp_path):
    for nombre in ("Windows", "Program Files", "System32", "ProgramData", ".ssh"):
        objetivo = tmp_path / nombre / "archivo.txt"
        assert safety.is_protected_path(objetivo), f"{nombre} debería estar protegida"


def test_protection_is_case_insensitive(tmp_path):
    assert safety.is_protected_path(tmp_path / "WINDOWS" / "x.txt")
    assert safety.is_protected_path(tmp_path / "windows" / "x.txt")
    assert safety.is_protected_path(tmp_path / "WiNdOwS" / "x.txt")


def test_nested_system_folder_is_protected(tmp_path):
    # La carpeta protegida puede estar en cualquier nivel de la ruta.
    profundo = tmp_path / "datos" / "respaldo" / "Windows" / "System32" / "x.dat"
    assert safety.is_protected_path(profundo)


def test_normal_user_folders_are_not_protected(tmp_path):
    for nombre in ("Descargas", "Documentos", "Fotos", "proyecto"):
        assert not safety.is_protected_path(tmp_path / nombre / "archivo.txt")


def test_path_traversal_cannot_disguise_a_system_folder(tmp_path):
    # "carpeta/../Windows/x" se resuelve a "Windows/x" y tiene que detectarse.
    disfrazada = tmp_path / "carpeta" / ".." / "Windows" / "x.txt"
    assert safety.is_protected_path(disfrazada)


# --------------------------------------------------------------------------
# ensure_safe_to_modify: la única puerta de las operaciones destructivas
# --------------------------------------------------------------------------

def test_ensure_safe_blocks_system_paths(tmp_path):
    with pytest.raises(safety.UnsafePathError):
        safety.ensure_safe_to_modify(tmp_path / "Windows" / "kernel.dat")


def test_ensure_safe_blocks_drive_root(tmp_path):
    with pytest.raises(safety.UnsafePathError):
        safety.ensure_safe_to_modify(tmp_path.anchor)


def test_ensure_safe_blocks_sensitive_extensions_by_default(tmp_path):
    with pytest.raises(safety.UnsafePathError):
        safety.ensure_safe_to_modify(tmp_path / "programa.exe")


def test_ensure_safe_allows_sensitive_extension_when_explicitly_requested(tmp_path):
    # La cuarentena necesita poder mover un .exe sospechoso; lo que nunca
    # se permite es la ruta de sistema, ni con allow_sensitive.
    assert safety.ensure_safe_to_modify(tmp_path / "sospechoso.exe", allow_sensitive=True)
    with pytest.raises(safety.UnsafePathError):
        safety.ensure_safe_to_modify(tmp_path / "Windows" / "x.exe", allow_sensitive=True)


def test_ensure_safe_allows_a_normal_user_file(tmp_path):
    resultado = safety.ensure_safe_to_modify(tmp_path / "basura.tmp")
    assert resultado.name == "basura.tmp"


def test_filter_safe_paths_keeps_only_the_safe_ones(tmp_path):
    entradas = [
        tmp_path / "ok.tmp",
        tmp_path / "Windows" / "malo.tmp",
        tmp_path / "otro.log",
        tmp_path / "Program Files" / "app.tmp",
        tmp_path / "instalador.exe",
    ]
    seguras = {p.name for p in safety.filter_safe_paths(entradas)}
    assert seguras == {"ok.tmp", "otro.log"}


def test_filter_safe_paths_on_empty_list():
    assert safety.filter_safe_paths([]) == []


# --------------------------------------------------------------------------
# is_within_directory: lo que evita escapar de una carpeta permitida
# --------------------------------------------------------------------------

def test_is_within_directory_detects_real_containment(tmp_path):
    dentro = tmp_path / "sub" / "archivo.txt"
    assert safety.is_within_directory(dentro, tmp_path)


def test_is_within_directory_rejects_traversal_escape(tmp_path):
    base = tmp_path / "permitida"
    base.mkdir()
    escape = base / ".." / "afuera.txt"
    assert not safety.is_within_directory(escape, base)


def test_is_within_directory_same_path_requires_allow_equal(tmp_path):
    assert not safety.is_within_directory(tmp_path, tmp_path)
    assert safety.is_within_directory(tmp_path, tmp_path, allow_equal=True)


def test_describe_protection_explains_the_reason(tmp_path):
    assert "protegida" in safety.describe_protection(tmp_path / "Windows" / "x.txt")
    assert "raíz" in safety.describe_protection(tmp_path.anchor)


# --------------------------------------------------------------------------
# Cuarentena: destruir es reversible o no se hace
# --------------------------------------------------------------------------

@pytest.fixture
def cuarentena(tmp_path):
    return tmp_path / "_Cuarentena"


def test_quarantine_moves_the_file_without_deleting_it(tmp_path, cuarentena):
    origen = tmp_path / "sospechoso.exe"
    origen.write_text("contenido importante")

    item = quarantine.quarantine_file(origen, reason="prueba", base=cuarentena)

    assert not origen.exists(), "el archivo debe salir de su lugar original"
    guardado = cuarentena / item.stored_name
    assert guardado.exists(), "el archivo debe seguir existiendo en cuarentena"
    assert guardado.read_text() == "contenido importante", "no se puede perder el contenido"


def test_quarantine_records_the_original_path_for_restoring(tmp_path, cuarentena):
    origen = tmp_path / "datos" / "archivo.tmp"
    origen.parent.mkdir()
    origen.write_text("x")

    item = quarantine.quarantine_file(origen, base=cuarentena)

    assert Path(item.original_path).name == "archivo.tmp"
    assert item.item_id and item.stored_name


def test_restore_puts_the_file_back_exactly_where_it_was(tmp_path, cuarentena):
    origen = tmp_path / "carpeta" / "vuelve.txt"
    origen.parent.mkdir()
    origen.write_text("soy el original")

    item = quarantine.quarantine_file(origen, base=cuarentena)
    destino = quarantine.restore_item(item.item_id, base=cuarentena)

    assert destino == origen.resolve()
    assert origen.read_text() == "soy el original"
    assert quarantine.list_items(base=cuarentena) == []


def test_quarantine_refuses_files_from_system_paths(tmp_path, cuarentena):
    sistema = tmp_path / "Windows" / "System32"
    sistema.mkdir(parents=True)
    critico = sistema / "importante.dat"
    critico.write_text("no me toques")

    with pytest.raises(safety.UnsafePathError):
        quarantine.quarantine_file(critico, base=cuarentena)

    assert critico.exists(), "un archivo de sistema nunca se mueve"


def test_restore_into_a_system_path_is_blocked(tmp_path, cuarentena):
    # Se simula un manifiesto manipulado que apunta a una carpeta de sistema.
    origen = tmp_path / "normal.txt"
    origen.write_text("x")
    item = quarantine.quarantine_file(origen, base=cuarentena)

    items = quarantine.load_manifest(cuarentena)
    items[0].original_path = str(tmp_path / "Windows" / "System32" / "colado.txt")
    quarantine.save_manifest(items, cuarentena)

    with pytest.raises(safety.UnsafePathError):
        quarantine.restore_item(item.item_id, base=cuarentena)

    assert not (tmp_path / "Windows").exists(), "no debe crear ni escribir en rutas de sistema"


def test_purge_item_cannot_delete_outside_the_quarantine(tmp_path, cuarentena):
    victima = tmp_path / "no-tocar.txt"
    victima.write_text("importante")

    origen = tmp_path / "cualquiera.txt"
    origen.write_text("x")
    item = quarantine.quarantine_file(origen, base=cuarentena)

    # Manifiesto manipulado para apuntar afuera de la cuarentena.
    items = quarantine.load_manifest(cuarentena)
    items[0].stored_name = "../no-tocar.txt"
    quarantine.save_manifest(items, cuarentena)

    with pytest.raises(safety.UnsafePathError):
        quarantine.purge_item(item.item_id, base=cuarentena)

    assert victima.exists(), "purge nunca puede borrar fuera de la cuarentena"


def test_purge_all_only_deletes_inside_the_quarantine(tmp_path, cuarentena):
    afuera = tmp_path / "intacto.txt"
    afuera.write_text("y")

    for nombre in ("a.txt", "b.txt"):
        f = tmp_path / nombre
        f.write_text("x")
        quarantine.quarantine_file(f, base=cuarentena)

    borrados = quarantine.purge_all(base=cuarentena)

    assert borrados == 2
    assert afuera.exists()
    assert quarantine.list_items(base=cuarentena) == []


def test_quarantine_two_files_with_the_same_name_do_not_collide(tmp_path, cuarentena):
    for sub in ("uno", "dos"):
        carpeta = tmp_path / sub
        carpeta.mkdir()
        (carpeta / "igual.tmp").write_text(sub)

    a = quarantine.quarantine_file(tmp_path / "uno" / "igual.tmp", base=cuarentena)
    b = quarantine.quarantine_file(tmp_path / "dos" / "igual.tmp", base=cuarentena)

    assert a.stored_name != b.stored_name
    assert (cuarentena / a.stored_name).read_text() == "uno"
    assert (cuarentena / b.stored_name).read_text() == "dos"


def test_quarantine_missing_file_raises_clearly(tmp_path, cuarentena):
    with pytest.raises(FileNotFoundError):
        quarantine.quarantine_file(tmp_path / "no-existe.txt", base=cuarentena)


def test_corrupt_manifest_does_not_break_the_app(tmp_path, cuarentena):
    cuarentena.mkdir(parents=True)
    (cuarentena / quarantine.MANIFEST_NAME).write_text("{esto no es json valido")
    assert quarantine.load_manifest(cuarentena) == []
    assert quarantine.summarize(cuarentena) == ["La cuarentena está vacía."]


def test_quarantine_summary_reports_size_and_origin(tmp_path, cuarentena):
    origen = tmp_path / "pesado.bin"
    origen.write_bytes(b"0" * 2048)
    quarantine.quarantine_file(origen, reason="motivo de prueba", base=cuarentena)

    texto = "\n".join(quarantine.summarize(cuarentena))
    assert "pesado.bin" in texto
    assert "motivo de prueba" in texto
    assert "restaurar" in texto


# --------------------------------------------------------------------------
# guards: la IA no puede debilitar la capa de seguridad
# --------------------------------------------------------------------------

_RELLENO = "\n" + "# comentario de relleno para el ratio de tamaño\n" * 12


def _fuente_safety(extra_dirs: int = 3, con_funcion: bool = True) -> str:
    dirs = ", ".join(f'"carpeta{i}"' for i in range(extra_dirs))
    funcion = (
        "def ensure_safe_to_modify(p):\n    return p\n\n"
        if con_funcion else ""
    )
    return (
        f"PROTECTED_DIR_NAMES = frozenset({{{dirs}}})\n"
        "SENSITIVE_EXTENSIONS = frozenset({\".exe\", \".dll\"})\n\n"
        "class UnsafePathError(Exception):\n    pass\n\n"
        f"{funcion}"
        "def is_safe_to_modify(p):\n    return True\n\n"
        "def is_protected_path(p):\n    return False\n\n"
        "def is_within_directory(a, b):\n    return True\n\n"
        "def is_drive_root(p):\n    return False\n\n"
        "def filter_safe_paths(ps):\n    return list(ps)\n"
        + _RELLENO
    )


def test_guard_rejects_removing_a_safety_function():
    ok, motivo = guards.validate_change(
        _fuente_safety(con_funcion=True),
        _fuente_safety(con_funcion=False),
        "app/safety.py",
    )
    assert not ok
    assert "seguridad" in motivo


def test_guard_rejects_shrinking_the_protected_folder_list():
    ok, motivo = guards.validate_change(
        _fuente_safety(extra_dirs=8),
        _fuente_safety(extra_dirs=4),
        "app/safety.py",
    )
    assert not ok
    assert "protección" in motivo


def test_guard_accepts_growing_the_protected_folder_list():
    ok, _ = guards.validate_change(
        _fuente_safety(extra_dirs=4),
        _fuente_safety(extra_dirs=9),
        "app/safety.py",
    )
    assert ok


def test_guard_critical_modules_include_safety_and_quarantine():
    assert "safety.py" in guards.CRITICAL_MODULES
    assert "quarantine.py" in guards.CRITICAL_MODULES


def test_guard_protects_the_organizer_blocklist_too():
    assert "SYSTEM_FOLDER_BLOCKLIST" in guards.PROTECTED_COLLECTIONS
    assert "PROTECTED_DIR_NAMES" in guards.PROTECTED_COLLECTIONS


# --------------------------------------------------------------------------
# Guardrail sobre el código: nadie agrega un borrado masivo
# --------------------------------------------------------------------------

def test_no_module_uses_rmtree_outside_of_nothing():
    # rmtree borra carpetas enteras de forma recursiva. En un proyecto que
    # se reescribe solo, la forma más segura de tratarlo es prohibirlo.
    import inspect
    import browser
    import diskreport
    import duplicates
    import startup
    for module in (quarantine, safety, browser, diskreport, duplicates, startup):
        source = inspect.getsource(module)
        assert "rmtree" not in source, f"{module.__name__} no debe usar rmtree"
