"""
test_integrity.py — que la app siga siendo ejecutable y coherente.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
Falla real, encontrada después de 221 mejoras automáticas aceptadas: la IA
cambió los imports de `from safety import X` a `from app.safety import X` en
cinco módulos. Los 197 tests siguieron pasando en verde, porque pytest corre
desde la raíz del repo y ahí `app.safety` resuelve. Pero la app dejó de
arrancar:

    $ python app/main.py
    ModuleNotFoundError: No module named 'app'

O sea: el bucle tuvo luz verde durante horas mientras el producto estaba
roto. El problema no fue la IA, fue que ningún test verificaba lo único que
al usuario le importa: que la app abra.

Peor todavía, quedaban dos módulos de `safety` cargados a la vez (`safety` y
`app.safety`), cada uno con su propia clase `UnsafePathError`. Un
`except safety.UnsafePathError` en `main.py` no atrapa la excepción lanzada
por `app.safety`: son clases distintas para Python. Una operación bloqueada
se convertía en un crash.

Los tests de acá son estructurales: no prueban comportamiento, prueban que
el proyecto siga siendo el proyecto. Son barato de correr y cierran la puerta
a toda esta familia de fallas.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"

# main.py se excluye de los tests que importan de verdad: necesita
# customtkinter y una pantalla real, que no existen en GitHub Actions. Se
# cubre igual, pero con análisis estático (ver más abajo).
GUI_MODULE = "main.py"

# Módulos que solo leen. Ninguno tiene permitido usar el chequeo de
# escritura: `ensure_safe_to_modify` rechaza extensiones sensibles y rutas de
# sistema, que estos módulos necesitan poder *mirar*.
READ_ONLY_MODULES = (
    "assistant.py",
    "scanner.py",
    "startup.py",
    "memory.py",
    "diskreport.py",
    "browser.py",
    "duplicates.py",
    "healthscore.py",
    "reporting.py",
)

# De los anteriores, los que además no deben escribir NINGÚN archivo.
# `reporting.py` queda afuera porque su trabajo es justamente guardar el
# informe en la ruta que el usuario eligió en el diálogo.
NEVER_WRITE_MODULES = tuple(m for m in READ_ONLY_MODULES if m != "reporting.py")


def calls_and_imports(tree: ast.Module) -> set[str]:
    """Nombres realmente llamados o importados, ignorando comentarios y textos.

    Buscar el nombre como texto suelto daría falsos positivos con los
    comentarios que explican por qué NO se usa esa función.
    """
    usados: set[str] = set()
    for nodo in ast.walk(tree):
        if isinstance(nodo, ast.Call):
            if isinstance(nodo.func, ast.Name):
                usados.add(nodo.func.id)
            elif isinstance(nodo.func, ast.Attribute):
                usados.add(nodo.func.attr)
        elif isinstance(nodo, ast.ImportFrom):
            usados.update(alias.name for alias in nodo.names)
    return usados


def app_modules(include_gui: bool = False) -> list[Path]:
    """Todos los módulos de la app, opcionalmente incluyendo la interfaz."""
    return sorted(
        f for f in APP_DIR.glob("*.py")
        if not f.name.startswith("__") and (include_gui or f.name != GUI_MODULE)
    )


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_names(tree: ast.Module) -> list[str]:
    """Módulos raíz importados: de `from a.b import c` devuelve 'a'."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module.split(".")[0])
    return names


# --------------------------------------------------------------------------
# Lo central: la app tiene que poder importarse como la corre el usuario
# --------------------------------------------------------------------------

def test_every_module_imports_the_way_the_app_actually_runs():
    """Importa todos los módulos con solo `app/` en el path.

    Es exactamente el entorno de `python app/main.py`: el directorio del
    script es lo único en sys.path, la raíz del repo NO está. Se hace en un
    subproceso para no contaminar el pytest que está corriendo.
    """
    modules = [f.stem for f in app_modules()]
    codigo = "import " + ", ".join(modules) + "\nprint('OK')"

    resultado = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=APP_DIR, capture_output=True, text=True, timeout=120,
    )

    assert resultado.returncode == 0, (
        "La app no puede importar sus propios módulos como la ejecuta el "
        f"usuario (python app/main.py):\n{resultado.stderr}"
    )
    assert "OK" in resultado.stdout


def test_no_module_uses_package_style_imports():
    """Prohíbe `from app.x import y` y `import app.x`.

    Funciona al correr pytest desde la raíz, pero rompe la app, porque cuando
    se ejecuta `python app/main.py` no existe ningún paquete llamado `app`.
    """
    culpables = []
    for archivo in app_modules(include_gui=True):
        if "app" in imported_names(parse(archivo)):
            culpables.append(archivo.name)

    assert not culpables, (
        "Estos módulos importan con estilo de paquete (app.algo), lo que "
        f"rompe `python app/main.py`: {', '.join(culpables)}. "
        "Usá imports planos: `from safety import ...`"
    )


def test_gui_only_imports_modules_that_exist():
    """Valida los imports locales de main.py sin importarlo.

    main.py no se puede importar en CI, así que sus imports se verifican
    leyendo el código: cada módulo local que menciona tiene que existir como
    archivo en app/.
    """
    disponibles = {f.stem for f in app_modules(include_gui=True)}
    # Se usa el listado real de la librería estándar en vez de una lista
    # escrita a mano, que quedaba desactualizada cada vez que la UI necesitaba
    # un módulo nuevo y hacía fallar el test por el motivo equivocado.
    externos = set(sys.stdlib_module_names) | {"customtkinter"}
    faltantes = [
        nombre for nombre in imported_names(parse(APP_DIR / GUI_MODULE))
        if nombre not in disponibles and nombre not in externos
    ]
    assert not faltantes, f"main.py importa módulos que no existen: {faltantes}"


def test_no_new_third_party_dependencies():
    """La única dependencia externa permitida es customtkinter (solo la UI)."""
    permitidos_externos = {"customtkinter"}
    locales = {f.stem for f in app_modules(include_gui=True)}
    estandar = set(sys.stdlib_module_names)

    for archivo in app_modules(include_gui=True):
        for nombre in imported_names(parse(archivo)):
            assert nombre in estandar or nombre in locales or nombre in permitidos_externos, (
                f"{archivo.name} importa '{nombre}', que no es de la librería "
                "estándar ni del proyecto. No se permiten dependencias nuevas."
            )


# --------------------------------------------------------------------------
# El contrato de safety: dos funciones, dos usos, sin mezclarlos
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def safety():
    sys.path.insert(0, str(APP_DIR))
    import safety as modulo
    return modulo


def test_ensure_raises_instead_of_returning_false(safety, tmp_path):
    """`ensure_safe_to_modify` lanza. Nunca devuelve un valor falso.

    De esto depende que sea seguro: si devolviera False, olvidarse de mirar
    el resultado terminaría en un borrado. Y es también la razón por la que
    NO se puede usar dentro de un `if`.
    """
    with pytest.raises(safety.UnsafePathError):
        safety.ensure_safe_to_modify(tmp_path / "Windows" / "x.txt")

    # Cuando la ruta es segura devuelve un Path, que siempre es verdadero.
    assert safety.ensure_safe_to_modify(tmp_path / "ok.tmp")


def test_is_safe_returns_bool_and_never_raises(safety, tmp_path):
    """`is_safe_to_modify` es la variante para usar en un `if`."""
    assert safety.is_safe_to_modify(tmp_path / "ok.tmp") is True
    assert safety.is_safe_to_modify(tmp_path / "Windows" / "x.txt") is False
    assert safety.is_safe_to_modify(tmp_path.anchor) is False
    assert safety.is_safe_to_modify(tmp_path / "prog.exe") is False
    assert safety.is_safe_to_modify(tmp_path / "prog.exe", allow_sensitive=True) is True
    # Basura de entrada: devuelve False, no explota.
    for basura in (None, "", 12345, [], {}):
        assert safety.is_safe_to_modify(basura) is False


def test_the_two_functions_agree(safety, tmp_path):
    """Misma decisión, distinta forma de comunicarla."""
    for candidato in (tmp_path / "ok.log", tmp_path / "Windows" / "n.txt",
                      tmp_path / "x.dll", tmp_path.anchor):
        try:
            safety.ensure_safe_to_modify(candidato)
            lanzo = False
        except safety.UnsafePathError:
            lanzo = True
        assert safety.is_safe_to_modify(candidato) is (not lanzo), (
            f"las dos funciones no coinciden para {candidato}"
        )


def test_boolean_misuse_of_ensure_is_not_present():
    """Busca el patrón `if ... ensure_safe_to_modify(...)`, que es dead code.

    `ensure_safe_to_modify` devuelve un Path o lanza, así que dentro de un
    `if` la condición es siempre verdadera: el chequeo no filtra nada y la
    excepción se escapa del bucle. Parece seguro y no lo es. La IA escribió
    exactamente esto en cuatro módulos.
    """
    culpables = []
    for archivo in app_modules(include_gui=True):
        for nodo in ast.walk(parse(archivo)):
            if not isinstance(nodo, (ast.If, ast.IfExp, ast.comprehension)):
                continue
            pruebas = nodo.ifs if isinstance(nodo, ast.comprehension) else [nodo.test]
            for prueba in pruebas:
                for hijo in ast.walk(prueba):
                    if (isinstance(hijo, ast.Call)
                            and isinstance(hijo.func, ast.Name)
                            and hijo.func.id == "ensure_safe_to_modify"):
                        culpables.append(f"{archivo.name}:{hijo.lineno}")

    assert not culpables, (
        "`ensure_safe_to_modify` está usado como condición en "
        f"{', '.join(culpables)}. Devuelve un Path o lanza, así que el `if` "
        "no filtra nada. Para condiciones usá `is_safe_to_modify`."
    )


def test_read_only_modules_do_not_use_the_write_check():
    """Los módulos que solo leen no pueden usar el chequeo de escritura.

    `ensure_safe_to_modify` rechaza `.exe`, `.dll` y las rutas de sistema.
    Eso es correcto para borrar, y desastroso para leer: el escáner
    heurístico abortaba al encontrar el primer ejecutable (justo lo que tiene
    que revisar), y la pestaña de Inicio no funcionaba nunca en Windows,
    porque la carpeta de arranque vive debajo de un directorio "Windows".
    """
    culpables = []
    for nombre in READ_ONLY_MODULES:
        archivo = APP_DIR / nombre
        if not archivo.exists():
            continue
        if "ensure_safe_to_modify" in calls_and_imports(parse(archivo)):
            culpables.append(nombre)

    assert not culpables, (
        f"Estos módulos son de solo lectura y usan el chequeo de escritura: "
        f"{', '.join(culpables)}. Para saber si algo es de sistema sin "
        "bloquear la lectura, usá `is_protected_path`."
    )


def test_read_only_modules_never_delete_or_move():
    """Ningún módulo de solo lectura puede borrar ni mover archivos."""
    destructivos = {"unlink", "rmdir", "rmtree", "move", "remove", "rename", "replace"}
    for nombre in READ_ONLY_MODULES:
        archivo = APP_DIR / nombre
        if not archivo.exists():
            continue
        usados = calls_and_imports(parse(archivo)) & destructivos
        assert not usados, (
            f"{nombre} debería ser de solo lectura pero llama a "
            f"{', '.join(sorted(usados))}"
        )


def test_analysis_modules_never_write_files():
    """Los módulos de análisis tampoco escriben archivos."""
    escrituras = {"write_text", "write_bytes", "mkdir", "touch"}
    for nombre in NEVER_WRITE_MODULES:
        archivo = APP_DIR / nombre
        if not archivo.exists():
            continue
        usados = calls_and_imports(parse(archivo)) & escrituras
        assert not usados, (
            f"{nombre} es de solo análisis pero llama a {', '.join(sorted(usados))}"
        )


# --------------------------------------------------------------------------
# Coherencia general del proyecto
# --------------------------------------------------------------------------

def test_every_module_compiles():
    """Ningún módulo puede quedar con error de sintaxis."""
    for archivo in app_modules(include_gui=True):
        try:
            parse(archivo)
        except SyntaxError as e:
            pytest.fail(f"{archivo.name} no compila: línea {e.lineno}: {e.msg}")


def test_safety_is_the_only_module_defining_the_protection_lists():
    """Las listas de protección viven en un solo lugar.

    Si otro módulo define su propia copia de `PROTECTED_DIR_NAMES`, arreglar
    una se vuelve arreglar la mitad, y la otra mitad queda expuesta.
    """
    for archivo in app_modules(include_gui=True):
        if archivo.name == "safety.py":
            continue
        contenido = archivo.read_text(encoding="utf-8")
        assert "PROTECTED_DIR_NAMES =" not in contenido, (
            f"{archivo.name} define su propia lista de protección. "
            "Tiene que importarla de safety.py."
        )


def test_gui_confirms_before_every_destructive_action():
    """Cada acción destructiva de la interfaz pide confirmación.

    Se verifica que los manejadores que borran o mueven llamen a `_confirm`,
    así una mejora de la IA no puede dejar un botón que borre de una sola
    pulsación.
    """
    tree = parse(APP_DIR / GUI_MODULE)
    destructivos = ("on_delete_reviewed", "on_purge_quarantine",
                    "on_quarantine_findings", "on_quarantine_duplicates",
                    "on_stage", "on_trim_process")

    encontrados = {
        nodo.name: nodo for nodo in ast.walk(tree)
        if isinstance(nodo, ast.FunctionDef) and nodo.name in destructivos
    }

    for nombre in destructivos:
        nodo = encontrados.get(nombre)
        assert nodo is not None, f"main.py perdió el manejador {nombre}"
        llamadas = {
            hijo.func.attr for hijo in ast.walk(nodo)
            if isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Attribute)
        }
        assert "_confirm" in llamadas, (
            f"{nombre} hace algo destructivo sin pedir confirmación al usuario"
        )
