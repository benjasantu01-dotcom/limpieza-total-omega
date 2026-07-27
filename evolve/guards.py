"""
guards.py
Validaciones que corren ANTES de los tests, sobre el archivo que la IA
acaba de reescribir. Existen porque los tests solos no alcanzan:

- `app/main.py` no lo importa ningún test (necesita customtkinter y una
  pantalla real, que no hay en GitHub Actions). Sin estas guardias, la
  IA podía romper main.py por completo y el cambio se aceptaba igual
  porque los tests seguían pasando. Acá se valida la sintaxis de forma
  directa, sin importar el módulo.

- En una semana de corridas autónomas (~2000 iteraciones) el riesgo real
  no es un error ruidoso, es la pérdida silenciosa de funcionalidad: que
  la IA "simplifique" borrando una función que nadie testea. Por eso se
  compara el inventario de símbolos antes y después.
"""

from __future__ import annotations
import ast

# Si el archivo nuevo pesa menos que esta fracción del original,
# asumimos que la IA borró código en vez de mejorarlo.
MIN_SIZE_RATIO = 0.6

# Módulos donde una "mejora" mal hecha puede dañar el sistema del usuario.
# Para estos se exige que sigan existiendo ciertos símbolos, más allá de la
# comparación genérica: son la capa que evita borrados peligrosos.
CRITICAL_MODULES = {
    "safety.py": (
        "ensure_safe_to_modify",
        "is_safe_to_modify",
        "is_protected_path",
        "is_within_directory",
        "is_drive_root",
        "filter_safe_paths",
        "UnsafePathError",
        "PROTECTED_DIR_NAMES",
        "SENSITIVE_EXTENSIONS",
    ),
    "quarantine.py": ("quarantine_file", "restore_item", "purge_item", "purge_all"),
}

# Colecciones que no pueden encogerse: cada elemento que se saca de acá es
# una carpeta o extensión que deja de estar protegida. Si la IA "simplifica"
# una de estas listas, el cambio se rechaza.
PROTECTED_COLLECTIONS = (
    "PROTECTED_DIR_NAMES",
    "SENSITIVE_EXTENSIONS",
    "SYSTEM_FOLDER_BLOCKLIST",
    "NEVER_TOUCH",
)


def _module_level_names(tree: ast.AST) -> set[str]:
    """Nombres asignados a nivel de módulo (constantes incluidas)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _collection_sizes(tree: ast.AST) -> dict[str, int]:
    """Cuenta los elementos de las colecciones protegidas del módulo.

    Solo cuenta literales (set, list, tuple, dict, y las envueltas en una
    llamada como `frozenset({...})`). Si no se puede contar, la clave no
    aparece y la comparación posterior simplemente no aplica.
    """
    sizes: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in PROTECTED_COLLECTIONS:
                continue
            value = node.value
            # Desenvuelve frozenset({...}) / set([...]) para contar el literal.
            if isinstance(value, ast.Call) and value.args:
                value = value.args[0]
            if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
                sizes[target.id] = len(value.elts)
            elif isinstance(value, ast.Dict):
                sizes[target.id] = len(value.keys)
    return sizes


def _collect_symbols(tree: ast.AST) -> set[str]:
    """Inventario de símbolos que deben sobrevivir a una mejora.

    Incluye funciones y clases de nivel superior, y los métodos de cada
    clase. Se ignoran las funciones privadas de nivel superior (las que
    empiezan con "_") para no bloquear refactors legítimos que renombran
    un helper interno.
    """
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                symbols.add(node.name)
        elif isinstance(node, ast.ClassDef):
            symbols.add(node.name)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Los métodos sí se exigen todos, incluso los privados:
                    # borrar algo como _build_layout rompe la app entera.
                    symbols.add(f"{node.name}.{item.name}")
    return symbols


def validate_change(original: str, new: str, file_name: str) -> tuple[bool, str]:
    """Devuelve (es_valido, motivo). El motivo se loguea cuando se rechaza."""
    if not new.strip():
        return False, "el archivo propuesto quedó vacío"

    # 1. Sintaxis: vale para cualquier archivo, incluso los que no se pueden importar.
    try:
        new_tree = ast.parse(new)
    except SyntaxError as e:
        return False, f"error de sintaxis en la propuesta (línea {e.lineno}): {e.msg}"

    # 2. Encogimiento sospechoso: señal típica de borrado en vez de mejora.
    if len(original) > 0 and len(new) < len(original) * MIN_SIZE_RATIO:
        pct = round(len(new) / len(original) * 100)
        return False, f"el archivo se encogió al {pct}% del original (posible pérdida de código)"

    # 3. Módulos críticos: sus símbolos de seguridad son obligatorios.
    #    Se chequea antes que la comparación genérica para que el motivo del
    #    rechazo que queda en el log diga que fue por seguridad, y para que
    #    valga incluso si el archivo original ya venía dañado.
    base_name = str(file_name).replace("\\", "/").rsplit("/", 1)[-1]
    required = CRITICAL_MODULES.get(base_name)
    if required:
        present = _collect_symbols(new_tree) | _module_level_names(new_tree)
        faltantes = [name for name in required if name not in present]
        if faltantes:
            return False, (
                f"'{base_name}' es un módulo de seguridad: no puede perder "
                f"{', '.join(faltantes)}"
            )

    # 4. Pérdida silenciosa de funcionalidad.
    try:
        original_tree = ast.parse(original)
    except SyntaxError:
        # El archivo original ya estaba roto: no podemos comparar, dejamos pasar
        # el resto de las validaciones (la sintaxis nueva ya se verificó arriba).
        return True, "ok (original no parseable, solo se validó la sintaxis nueva)"

    missing = _collect_symbols(original_tree) - _collect_symbols(new_tree)
    if missing:
        return False, f"desaparecieron símbolos que existían antes: {', '.join(sorted(missing))}"

    # 5. Las listas de protección no pueden encogerse: cada elemento que se
    #    saca es una carpeta o extensión que queda expuesta a borrado.
    antes, despues = _collection_sizes(original_tree), _collection_sizes(new_tree)
    for nombre, cantidad_antes in antes.items():
        cantidad_despues = despues.get(nombre)
        if cantidad_despues is not None and cantidad_despues < cantidad_antes:
            return False, (
                f"se redujo la lista de protección {nombre} "
                f"({cantidad_antes} -> {cantidad_despues} elementos)"
            )

    return True, "ok"
