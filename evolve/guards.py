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

    # 3. Pérdida silenciosa de funcionalidad.
    try:
        original_tree = ast.parse(original)
    except SyntaxError:
        # El archivo original ya estaba roto: no podemos comparar, dejamos pasar
        # el resto de las validaciones (la sintaxis nueva ya se verificó arriba).
        return True, "ok (original no parseable, solo se validó la sintaxis nueva)"

    missing = _collect_symbols(original_tree) - _collect_symbols(new_tree)
    if missing:
        return False, f"desaparecieron símbolos que existían antes: {', '.join(sorted(missing))}"

    return True, "ok"
