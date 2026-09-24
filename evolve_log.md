<!-- Log rotado el 2026-09-24 03:43:51. Las 1392 líneas anteriores están en archive/evolve_log-20260924-034351.md -->

    RAM disponible: 11%
    Disco libre: 6%
    Duplicados: 900 MB
    Inicio: 19 items
1 failed, 298 passed, 4 warnings in 1.48s

```
- `2026-09-24T00:36:32` ❌ Mejora descartada en settings.py (no pasó los tests), se revirtió. Intento: Documenté el propósito de los validadores y tipos de datos mediante una jerarquía de docstrings más clara y type hints precisos, eliminando ambigüedades sobre el origen y destino de la validación.
- `2026-09-24T00:37:05` ✅ Mejora aceptada en startup.py (enfoque: legibilidad y documentación). Mejoré la legibilidad y mantenibilidad de la clase `StartupEntry` mediante la aplicación de docstrings detallados (siguiendo el estilo Google) y la clarificación de la lógica interna de validación, sin alterar la funcionalidad.
- `2026-09-24T00:37:06` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T00:37:10` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-09-24T00:37:56` 🛑 Propuesta bloqueada por la guardia en assistant.py (enfoque: rendimiento): desaparecieron símbolos que existían antes: Answer.is_online, AreaExplanation, SystemContext.is_valid_structure
- `2026-09-24T00:38:17` ➖ Sin cambios en branding.py (enfoque: rendimiento). Motivo: Optimicé el cálculo del gradiente de la barra de progreso utilizando `_get_grouped_segments` para evitar realizar llamadas repetitivas al canvas para píxeles contiguos del mismo color, reduciendo significativamente la carga de renderizado.
- `2026-09-24T00:38:17` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T00:38:17` Corrida terminada. Total usado hoy: 16.
- `2026-09-24T00:46:02` Arrancando corrida. Quedan hoy ~284 peticiones objetivo.
- `2026-09-24T00:46:35` ✅ Mejora aceptada en browser.py (enfoque: rendimiento). Optimicé el cálculo del tamaño de directorios sustituyendo el paso de `visited` por parámetro (que solo prevenía ciclos en una rama) por una estrategia global en `global_memo` para evitar re-escaneos redundantes de subdirectorios compartidos entre navegadores, reduciendo drásticamente las llamadas a `os.scandir` en escaneos de perfiles múltiples.
- `2026-09-24T00:47:04` Gemini no devolvió un bloque de archivo válido para diskreport.py (enfoque: rendimiento).
- `2026-09-24T00:47:36` ✅ Mejora aceptada en duplicates.py (enfoque: rendimiento). Optimizé la recolección de candidatos en `_collect_candidates` para evitar llamadas redundantes a `stat()` y `is_safe_to_modify()` mediante el uso de `os.scandir` (que ya expone los atributos del sistema de archivos en Windows), reduciendo significativamente las llamadas al sistema y mejorando la velocidad de escaneo.
- `2026-09-24T00:47:40` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T00:47:43` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-09-24T00:48:05` ✅ Mejora aceptada en healthscore.py (enfoque: rendimiento). Optimicé el rendimiento del Pipeline reemplazando `dict.get()` y iteraciones redundantes en `summarize` y `compute_score` por acceso directo y pre-cálculo de estructuras, minimizando llamadas a funciones dentro de los bucles críticos.
- `2026-09-24T00:48:05` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T00:48:05` Corrida terminada. Total usado hoy: 20.
- `2026-09-24T00:56:15` Arrancando corrida. Quedan hoy ~280 peticiones objetivo.
- `2026-09-24T00:56:18` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T00:57:21` Problema de red hablando con Gemini (intento 2/3). Esperando 6s...
- `2026-09-24T00:58:27` Problema de red hablando con Gemini (intento 3/3). Esperando 12s...
- `2026-09-24T00:58:48` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-09-24T00:59:32` Gemini no devolvió un bloque de archivo válido para memory.py (enfoque: rendimiento).
- `2026-09-24T00:59:59` Gemini no devolvió un bloque de archivo válido para organizer.py (enfoque: rendimiento).
- `2026-09-24T01:00:00` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T01:00:03` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-09-24T01:00:35` ✅ Mejora aceptada en quarantine.py (enfoque: rendimiento). Optimicé `list_items` y `purge_all` para evitar lecturas recurrentes y repetitivas del sistema de archivos mediante el uso de un cacheo local del contenido del directorio de cuarentena, reduciendo la complejidad de las operaciones masivas de O(N*M) a O(N+M).
- `2026-09-24T01:00:35` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T01:00:35` Corrida terminada. Total usado hoy: 24.
- `2026-09-24T01:06:25` Arrancando corrida. Quedan hoy ~276 peticiones objetivo.
- `2026-09-24T01:06:50` 🛑 Propuesta bloqueada por la guardia en reporting.py (enfoque: rendimiento): error de sintaxis en la propuesta (línea 104): unterminated string literal (detected at line 104)
- `2026-09-24T01:07:33` Tests FALLARON:
```
d: DID NOT RAISE UnsafePathError
FAILED evolve/tests/test_safety.py::test_ensure_safe_allows_sensitive_extension_when_explicitly_requested - Failed: DID NOT RAISE UnsafePathError
FAILED evolve/tests/test_safety.py::test_filter_safe_paths_keeps_only_the_safe_ones - AssertionError: assert {'app.tmp', '...', 'otro.log'} == {'ok.tmp', 'otro.log'}
  
  Extra items in the left set:
  'app.tmp'
  'malo.tmp'
  
  Full diff:
    {
  +     'app.tmp',
  +     'malo.tmp',
        'ok.tmp',
        'otro.log',
    }
FAILED evolve/tests/test_safety.py::test_describe_protection_explains_the_reason - assert 'protegida' in "'/tmp/pytest-of-runner/pytest-1/test_describe_protection_expla0/Windows/x.txt' es candidata a modificación."
 +  where "'/tmp/pytest-of-runner/pytest-1/test_describe_protection_expla0/Windows/x.txt' es candidata a modificación." = <function describe_protection at 0x7f52fa2da520>(((PosixPath('/tmp/pytest-of-runner/pytest-1/test_describe_protection_expla0') / 'Windows') / 'x.txt'))
 +    where <function describe_protection at 0x7f52fa2da520> = safety.describe_protection
FAILED evolve/tests/test_safety.py::test_quarantine_refuses_files_from_system_paths - Failed: DID NOT RAISE UnsafePathError
FAILED evolve/tests/test_safety.py::test_restore_into_a_system_path_is_blocked - RuntimeError: Error crítico en restauración: [Errno 2] No such file or directory: '/tmp/pytest-of-runner/pytest-1/test_restore_into_a_system_pat0/Windows/System32'
14 failed, 285 passed, 5 warnings in 1.54s

```
- `2026-09-24T01:07:33` ❌ Mejora descartada en safety.py (no pasó los tests), se revirtió. Intento: Se implementó un cacheado más granular y eficiente en `_is_system_path_cached` y se optimizó el chequeo de permisos (`is_sensitive_file` y `is_protected_path`) para evitar múltiples llamadas al sistema de archivos mediante el uso de `frozenset` y la simplificación de lógicas de normalización en bucles críticos.
- `2026-09-24T01:08:00` Gemini no devolvió un bloque de archivo válido para scanner.py (enfoque: rendimiento).
- `2026-09-24T01:08:16` Gemini no devolvió un bloque de archivo válido para settings.py (enfoque: rendimiento).
- `2026-09-24T01:08:16` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T01:08:16` Corrida terminada. Total usado hoy: 28.
- `2026-09-24T01:17:11` Arrancando corrida. Quedan hoy ~272 peticiones objetivo.
- `2026-09-24T01:17:44` Tests FALLARON:
```
L_SCAN_CACHE is not None:
            return _FULL_SCAN_CACHE
    
        seen: Set[Tuple[str, str]] = set()
        unique: List[StartupEntry] = []
    
        for entry in itertools.chain(entries_from_folders(), entries_from_registry()):
            key = (entry.name.lower(), entry.command.lower())
            if key not in seen:
                seen.add(key)
                unique.append(entry)
    
        _FULL_SCAN_CACHE = unique
>       return unique_entries
               ^^^^^^^^^^^^^^
E       NameError: name 'unique_entries' is not defined

app/startup.py:374: NameError
=============================== warnings summary ===============================
evolve/tests/test_integrity.py::test_no_module_uses_package_style_imports
evolve/tests/test_integrity.py::test_no_new_third_party_dependencies
evolve/tests/test_integrity.py::test_boolean_misuse_of_ensure_is_not_present
evolve/tests/test_integrity.py::test_every_module_compiles
  /home/runner/work/limpieza-total-omega/limpieza-total-omega/app/safety.py:219: SyntaxWarning: invalid escape sequence '\P'
    """Detecta rutas de dispositivos de Windows (e.g., \\.\PhysicalDrive0) no aptas para archivos."""

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED evolve/tests/test_modules.py::test_list_startup_entries_returns_a_list - NameError: name 'unique_entries' is not defined
1 failed, 298 passed, 4 warnings in 1.44s

```
- `2026-09-24T01:17:44` ❌ Mejora descartada en startup.py (no pasó los tests), se revirtió. Intento: Optimizé `list_startup_entries` eliminando la recreación innecesaria de objetos y mejorando la eficiencia del bucle de consolidación al usar un conjunto de tuplas directamente sobre los datos crudos antes de filtrar.
- `2026-09-24T01:18:28` ✅ Mejora aceptada en assistant.py (enfoque: robustez ante casos límite). Se ha robustecido el motor local ante datos inesperados en el contexto (métricas `NaN` o `inf`) al procesar los `active_problems`, garantizando que la app no falle al intentar formatear mensajes con valores no numéricos.
- `2026-09-24T01:19:07` ✅ Mejora aceptada en branding.py (enfoque: robustez ante casos límite). Se introdujo una validación defensiva en `save_logo_svg` para prevenir el desbordamiento de memoria ante intentos de renderizado con tamaños extremos, garantizando que el parámetro `size` se mantenga dentro de un rango físico razonable antes de cualquier operación de I/O.
- `2026-09-24T01:19:24` ✅ Mejora aceptada en browser.py (enfoque: robustez ante casos límite). Se reforzó la robustez ante errores de E/S en `_get_kernel32` y `_is_system_hidden` para evitar que fallos imprevistos en la carga de librerías del sistema detengan el escaneo de navegadores.
- `2026-09-24T01:19:24` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T01:19:24` Corrida terminada. Total usado hoy: 32.
- `2026-09-24T01:27:22` Arrancando corrida. Quedan hoy ~268 peticiones objetivo.
- `2026-09-24T01:27:27` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T01:28:00` ➖ Sin cambios en diskreport.py (enfoque: robustez ante casos límite). Motivo: Se ha mejorado `walk_files` para manejar de forma robusta los archivos que cambian de tamaño, son bloqueados por otros procesos o desaparecen durante la iteración (TOCTOU), evitando que una excepción `OSError` inesperada detenga el escaneo completo del disco.
- `2026-09-24T01:28:29` Gemini no devolvió un bloque de archivo válido para duplicates.py (enfoque: robustez ante casos límite).
- `2026-09-24T01:29:01` ✅ Mejora aceptada en healthscore.py (enfoque: robustez ante casos límite). Mejora la robustez del motor de cálculo ante valores de métricas que exceden las capacidades esperadas o presentan inconsistencias, añadiendo validación explícita de `nan` y `inf` en `_to_float` y asegurando que `_evaluate_rules` no colapse ante excepciones durante la generación de mensajes.
- `2026-09-24T01:30:01` Problema de red hablando con Gemini (intento 1/3). Esperando 3s...
- `2026-09-24T01:31:04` Problema de red hablando con Gemini (intento 2/3). Esperando 6s...
- `2026-09-24T01:32:10` Problema de red hablando con Gemini (intento 3/3). Esperando 12s...
- `2026-09-24T01:33:22` Red inestable tras 3 reintentos (HTTPSConnectionPool(host='generativelanguage.googleapis.com', port=443): Read timed out. (read timeout=60)). Se salta esta iteración.
- `2026-09-24T01:33:22` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T01:33:22` Corrida terminada. Total usado hoy: 36.
- `2026-09-24T01:37:32` Arrancando corrida. Quedan hoy ~264 peticiones objetivo.
- `2026-09-24T01:38:06` ✅ Mejora aceptada en memory.py (enfoque: robustez ante casos límite). Se mejora la robustez de `trim_working_set` añadiendo una comprobación explícita mediante `PROCESS_QUERY_INFORMATION` y manejando correctamente el posible error `ERROR_INVALID_PARAMETER` (que ocurre si el proceso muere entre la apertura del handle y la llamada a `EmptyWorkingSet`), evitando así comportamientos indefinidos al cerrar handles nulos.
- `2026-09-24T01:38:34` Gemini no devolvió un bloque de archivo válido para organizer.py (enfoque: robustez ante casos límite).
- `2026-09-24T01:38:35` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T01:39:02` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-09-24T01:39:44` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-09-24T01:40:33` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-09-24T01:40:55` 🛑 Propuesta bloqueada por la guardia en reporting.py (enfoque: robustez ante casos límite): error de sintaxis en la propuesta (línea 105): unterminated string literal (detected at line 105)
- `2026-09-24T01:40:55` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T01:40:55` Corrida terminada. Total usado hoy: 40.
- `2026-09-24T01:47:49` Arrancando corrida. Quedan hoy ~260 peticiones objetivo.
- `2026-09-24T01:48:33` ✅ Mejora aceptada en safety.py (enfoque: robustez ante casos límite). Se introdujo una verificación de "path traversal" mediante `Path.resolve()` contra la ruta normalizada antes de cualquier operación, garantizando que el acceso al sistema de archivos sea estrictamente absoluto y esté saneado ante posibles intentos de escaparse del directorio raíz definido (o del entorno de ejecución).
- `2026-09-24T01:48:34` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T01:49:05` ✅ Mejora aceptada en scanner.py (enfoque: robustez ante casos límite). Se ha añadido un chequeo de integridad en `_is_safe_entry` para validar que `entry.path` no sea una ruta truncada o malformada que podría causar errores en `is_protected_path` o futuras operaciones, utilizando `Path.is_absolute()` y capturando posibles excepciones en la resolución de rutas.
- `2026-09-24T01:49:37` ✅ Mejora aceptada en settings.py (enfoque: robustez ante casos límite). Mejoré la robustez de `save()` ante condiciones de concurrencia y fallos de escritura mediante la incorporación de `os.fsync` previo al renombrado y validación explícita de `is_safe_to_modify` sobre el archivo de respaldo (`.bak`), asegurando que no se sobrescriban o dañen archivos críticos bajo bloqueos de sistema o interrupciones.
- `2026-09-24T01:49:55` Tests FALLARON:
```
.................. [ 96%]
...........                                                              [100%]
=================================== FAILURES ===================================
_______________ test_executable_extracted_from_unquoted_command ________________

    def test_executable_extracted_from_unquoted_command():
>       assert startup.StartupEntry("X", "/usr/bin/app --flag", "reg").executable == "/usr/bin/app"
E       AssertionError: assert '' == '/usr/bin/app'
E         
E         - /usr/bin/app

evolve/tests/test_modules.py:664: AssertionError
=============================== warnings summary ===============================
evolve/tests/test_integrity.py::test_no_module_uses_package_style_imports
evolve/tests/test_integrity.py::test_no_new_third_party_dependencies
evolve/tests/test_integrity.py::test_boolean_misuse_of_ensure_is_not_present
evolve/tests/test_integrity.py::test_every_module_compiles
  /home/runner/work/limpieza-total-omega/limpieza-total-omega/app/safety.py:219: SyntaxWarning: invalid escape sequence '\P'
    """Detecta rutas de dispositivos de Windows (e.g., \\.\PhysicalDrive0) no aptas para archivos."""

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED evolve/tests/test_modules.py::test_executable_extracted_from_unquoted_command - AssertionError: assert '' == '/usr/bin/app'
  
  - /usr/bin/app
1 failed, 298 passed, 4 warnings in 1.44s

```
- `2026-09-24T01:49:55` ❌ Mejora descartada en startup.py (no pasó los tests), se revirtió. Intento: Se ha robustecido el método `_resolve_and_cache_path` para prevenir excepciones críticas en entornos donde las rutas del sistema pueden ser inaccesibles o estar bloqueadas, asegurando que el escáner no aborte ante permisos denegados o rutas malformadas.
- `2026-09-24T01:49:55` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T01:49:55` Corrida terminada. Total usado hoy: 44.
- `2026-09-24T01:57:56` Arrancando corrida. Quedan hoy ~256 peticiones objetivo.
- `2026-09-24T01:58:39` ✅ Mejora aceptada en assistant.py (enfoque: seguridad defensiva). Se endureció la validación de seguridad `_is_safe_text_structure` añadiendo una comprobación explícita para evitar que se filtren rutas de red UNC (que empiezan con `\\`), reforzando el cumplimiento de la política de no exponer estructuras de archivos sensibles.
- `2026-09-24T01:59:18` ✅ Mejora aceptada en branding.py (enfoque: seguridad defensiva). Se reforzó `save_logo_svg` aplicando `ensure_safe_to_modify` para el archivo de destino, garantizando que cualquier operación de escritura sea validada explícitamente por el motor de seguridad antes de intentar acceder al sistema de archivos, reemplazando una validación booleana más laxa.
- `2026-09-24T01:59:49` ✅ Mejora aceptada en browser.py (enfoque: seguridad defensiva). Se reforzó la seguridad defensiva en `_resolve_browser_path` añadiendo una validación explícita mediante `is_safe_to_modify` y `is_protected_path` sobre la ruta final construida, previniendo que el módulo intente siquiera procesar rutas que, aunque residan nominalmente en `LOCALAPPDATA`, hayan sido manipuladas para apuntar a zonas protegidas o fuera de scope.
- `2026-09-24T02:00:02` ✅ Mejora aceptada en diskreport.py (enfoque: seguridad defensiva). Se ha reforzado la seguridad defensiva en `walk_files` y `_is_excluded_path` añadiendo una validación explícita para detectar puntos de reparse (junctions/reparse points) mediante `entry.is_symlink()` y los atributos de archivo, evitando así la recursión infinita o el acceso no deseado a volúmenes montados fuera del árbol de directorios de interés.
- `2026-09-24T02:00:02` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T02:00:02` Corrida terminada. Total usado hoy: 48.
- `2026-09-24T02:08:06` Arrancando corrida. Quedan hoy ~252 peticiones objetivo.
- `2026-09-24T02:08:36` ✅ Mejora aceptada en duplicates.py (enfoque: seguridad defensiva). Se reforzó la seguridad defensiva en `_collect_candidates` y `_group_paths_by_hash` centralizando la validación de archivos para evitar seguir enlaces simbólicos o puntos de reparse durante la recursión, garantizando que solo se procesen rutas que pasen estrictamente por `is_safe_to_modify` antes de cualquier operación de I/O.
- `2026-09-24T02:09:05` ✅ Mejora aceptada en healthscore.py (enfoque: seguridad defensiva). Se reforzó la seguridad defensiva del módulo añadiendo una validación estricta de tipos y dominios en `_evaluate_rules` y `compute_score`, asegurando que el pipeline no pueda ser alterado por inyección de métricas inválidas o funciones de fábrica de mensajes maliciosas.
- `2026-09-24T02:10:05` Problema de red hablando con Gemini (intento 1/3). Esperando 3s...
- `2026-09-24T02:11:08` Problema de red hablando con Gemini (intento 2/3). Esperando 6s...
- `2026-09-24T02:12:14` Problema de red hablando con Gemini (intento 3/3). Esperando 12s...
- `2026-09-24T02:13:26` Red inestable tras 3 reintentos (HTTPSConnectionPool(host='generativelanguage.googleapis.com', port=443): Read timed out. (read timeout=60)). Se salta esta iteración.
- `2026-09-24T02:13:53` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T02:14:10` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-09-24T02:14:17` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-09-24T02:14:31` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-09-24T02:14:31` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T02:14:31` Corrida terminada. Total usado hoy: 52.
- `2026-09-24T02:18:18` Arrancando corrida. Quedan hoy ~248 peticiones objetivo.
- `2026-09-24T02:18:20` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T02:18:50` Gemini no devolvió un bloque de archivo válido para organizer.py (enfoque: seguridad defensiva).
- `2026-09-24T02:19:33` ✅ Mejora aceptada en quarantine.py (enfoque: seguridad defensiva). Se ha mejorado la robustez de `_is_file_locked` para evitar falsos positivos y posibles bloqueos de I/O en sistemas Windows, asegurando que la comprobación de acceso se realice de manera más conservadora y compatible con el enfoque de seguridad defensiva.
- `2026-09-24T02:19:33` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T02:20:01` 🛑 Propuesta bloqueada por la guardia en reporting.py (enfoque: seguridad defensiva): error de sintaxis en la propuesta (línea 106): unterminated string literal (detected at line 106)
- `2026-09-24T02:20:04` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T02:20:36` ✅ Mejora aceptada en safety.py (enfoque: seguridad defensiva). Se ha implementado una protección adicional en `ensure_safe_to_modify` para detectar si el sistema de archivos actual es de solo lectura a nivel de volumen (`DRIVE_REMOTE` o `DRIVE_CDROM` ya estaban cubiertos, pero se añade un chequeo explícito mediante el flag `FILE_READ_ONLY_VOLUME` de la API de Windows) antes de permitir cualquier operación de modificación, reforzando la integridad del disco ante cambios accidentales.
- `2026-09-24T02:20:36` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T02:20:36` Corrida terminada. Total usado hoy: 56.
- `2026-09-24T02:28:29` Arrancando corrida. Quedan hoy ~244 peticiones objetivo.
- `2026-09-24T02:28:31` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T02:28:34` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-09-24T02:29:08` ✅ Mejora aceptada en scanner.py (enfoque: seguridad defensiva). Se ha añadido una validación explícita para asegurar que la ruta a escanear no sea un punto de reanálisis (Junction o Symlink) antes de entrar en `os.scandir`, reforzando la seguridad defensiva contra la fuga de contexto fuera de la carpeta objetivo.
- `2026-09-24T02:29:15` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T02:29:51` ✅ Mejora aceptada en settings.py (enfoque: seguridad defensiva). Mejoré la seguridad defensiva en `save()` aplicando `ensure_safe_to_modify` antes de cualquier operación de escritura sobre el archivo principal o el respaldo, evitando así el uso de `is_safe_to_modify` que, siendo booleano, podría fallar silenciosamente en escenarios de permisos complejos donde se requiere una validación estricta que lance excepciones ante riesgos detectados.
- `2026-09-24T02:30:24` ✅ Mejora aceptada en startup.py (enfoque: seguridad defensiva). Se ha robustecido el filtrado en `parse_registry_csv` añadiendo una validación temprana contra `is_protected_path` tanto en la ruta original como en la resuelta antes de crear cualquier objeto `StartupEntry`, impidiendo que rutas críticas del sistema lleguen a ser procesadas.
- `2026-09-24T02:30:24` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:30:24` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T02:30:44` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:30:44` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T02:31:15` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:31:15` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T02:31:15` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T02:31:15` Corrida terminada. Total usado hoy: 60.
- `2026-09-24T02:38:40` Arrancando corrida. Quedan hoy ~240 peticiones objetivo.
- `2026-09-24T02:38:42` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:38:42` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T02:39:02` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:39:02` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T02:39:32` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:39:32` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T02:39:48` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:39:48` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T02:40:08` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:40:08` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T02:40:38` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:40:38` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T02:40:53` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:40:53` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T02:41:13` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:41:13` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T02:41:44` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:41:44` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T02:41:59` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:41:59` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T02:42:19` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:42:19` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T02:42:49` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:42:49` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T02:42:49` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T02:42:49` Corrida terminada. Total usado hoy: 64.
- `2026-09-24T02:48:50` Arrancando corrida. Quedan hoy ~236 peticiones objetivo.
- `2026-09-24T02:48:53` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:48:53` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T02:49:13` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:49:13` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T02:49:43` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:49:43` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T02:49:59` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:49:59` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T02:50:19` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:50:19` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T02:50:49` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:50:49` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T02:51:04` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:51:04` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T02:51:24` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:51:24` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T02:51:54` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:51:54` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T02:52:09` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:52:09` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T02:52:30` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:52:30` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T02:53:00` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:53:00` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T02:53:00` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T02:53:00` Corrida terminada. Total usado hoy: 68.
- `2026-09-24T02:59:00` Arrancando corrida. Quedan hoy ~232 peticiones objetivo.
- `2026-09-24T02:59:02` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:59:02` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T02:59:22` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:59:22` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T02:59:52` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T02:59:52` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:00:07` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:00:07` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:00:28` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:00:28` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:00:58` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:00:58` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:01:13` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:01:13` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:01:33` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:01:33` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:02:04` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:02:04` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:02:19` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:02:19` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:02:39` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:02:39` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:03:09` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:03:09` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:03:09` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T03:03:09` Corrida terminada. Total usado hoy: 72.
- `2026-09-24T03:09:12` Arrancando corrida. Quedan hoy ~228 peticiones objetivo.
- `2026-09-24T03:09:14` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:09:14` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:09:34` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:09:34` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:10:05` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:10:05` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:10:20` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:10:20` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:10:40` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:10:40` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:11:10` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:11:10` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:11:25` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:11:25` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:11:45` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:11:45` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:12:15` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:12:15` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:12:31` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:12:31` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:12:51` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:12:51` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:13:21` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:13:21` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:13:21` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T03:13:21` Corrida terminada. Total usado hoy: 76.
- `2026-09-24T03:19:21` Arrancando corrida. Quedan hoy ~224 peticiones objetivo.
- `2026-09-24T03:19:23` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:19:23` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:19:43` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:19:43` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:20:13` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:20:13` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:20:28` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:20:28` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:20:48` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:20:48` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:21:18` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:21:18` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:21:34` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:21:34` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:21:54` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:21:54` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:22:24` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:22:24` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:22:39` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:22:39` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:22:59` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:22:59` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:23:29` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:23:29` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:23:29` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T03:23:29` Corrida terminada. Total usado hoy: 80.
- `2026-09-24T03:29:31` Arrancando corrida. Quedan hoy ~220 peticiones objetivo.
- `2026-09-24T03:29:33` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:29:33` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:29:53` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:29:53` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:30:23` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:30:23` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:30:38` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:30:38` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:30:58` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:30:58` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:31:29` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:31:29` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:31:44` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:31:44` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:32:04` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:32:04` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:32:34` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:32:34` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:32:50` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:32:50` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:33:10` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:33:10` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:33:40` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:33:40` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:33:40` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T03:33:40` Corrida terminada. Total usado hoy: 84.
- `2026-09-24T03:39:42` Arrancando corrida. Quedan hoy ~216 peticiones objetivo.
- `2026-09-24T03:39:44` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:39:44` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:40:04` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:40:04` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:40:34` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:40:34` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:40:49` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:40:49` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:41:09` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:41:09` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:41:40` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:41:40` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:41:55` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:41:55` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:42:15` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:42:15` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:42:45` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:42:45` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:43:00` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:43:00` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:43:20` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:43:20` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:43:51` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:43:51` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:43:51` Rotación — log: 1392 líneas archivadas; metrics: 4 registros archivados; 2 archivo(s) histórico(s) descartado(s)
- `2026-09-24T03:43:51` Corrida terminada. Total usado hoy: 88.
- `2026-09-24T03:49:56` Arrancando corrida. Quedan hoy ~212 peticiones objetivo.
- `2026-09-24T03:49:59` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:49:59` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-09-24T03:50:19` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:50:19` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-09-24T03:50:50` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-09-24T03:50:50` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-09-24T03:51:25` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T03:52:10` ✅ Mejora aceptada en assistant.py (enfoque: manejo de errores y validación de entradas). Mejoré la robustez de `_safe_handler_wrapper` y los métodos `ingest` de `SystemContext` para asegurar que fallos en la ingesta o procesamiento de datos de entrada no propaguen excepciones inesperadas hacia la UI, validando explícitamente los tipos antes de la asignación.
- `2026-09-24T03:52:10` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T03:52:43` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-09-24T03:52:50` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-09-24T03:53:03` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-09-24T03:53:31` Gemini no devolvió un bloque de archivo válido para browser.py (enfoque: manejo de errores y validación de entradas).
- `2026-09-24T03:53:31` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T03:53:31` Corrida terminada. Total usado hoy: 92.
- `2026-09-24T04:00:05` Arrancando corrida. Quedan hoy ~208 peticiones objetivo.
- `2026-09-24T04:00:36` ✅ Mejora aceptada en diskreport.py (enfoque: manejo de errores y validación de entradas). Mejoré la robustez de `walk_files` y `largest_folders` capturando excepciones específicas en la resolución de rutas relativas y en la iteración del sistema de archivos, previniendo fallos ante nombres de archivo mal formados o cambios de estado durante el escaneo.
- `2026-09-24T04:00:36` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T04:01:06` ✅ Mejora aceptada en duplicates.py (enfoque: manejo de errores y validación de entradas). Mejoré la robustez de `suggest_keeper` y `format_group` mediante validaciones de tipo explícitas y manejo de errores de resolución de rutas, evitando que el proceso falle ante rutas malformadas o condiciones de carrera en el sistema de archivos.
- `2026-09-24T04:01:32` Gemini no devolvió un bloque de archivo válido para healthscore.py (enfoque: manejo de errores y validación de entradas).
- `2026-09-24T04:01:33` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T04:02:36` Problema de red hablando con Gemini (intento 2/3). Esperando 6s...
- `2026-09-24T04:03:42` ✅ Mejora aceptada en main.py (enfoque: manejo de errores y validación de entradas). Mejoré la robustez de la persistencia de ajustes en `on_save_settings` mediante el uso de un bloque `try-except` específico al invocar `settings_mod.update`, evitando que una posible corrupción durante la escritura (ej. error de I/O al persistir el JSON) deje la aplicación en un estado inconsistente o silenciosamente fallido.
- `2026-09-24T04:03:42` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T04:03:42` Corrida terminada. Total usado hoy: 96.
- `2026-09-24T04:10:19` Arrancando corrida. Quedan hoy ~204 peticiones objetivo.
- `2026-09-24T04:10:21` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-09-24T04:10:55` ✅ Mejora aceptada en memory.py (enfoque: manejo de errores y validación de entradas). Mejora el manejo de errores en `trim_working_set` y `_get_process_path` reemplazando llamadas a `getattr` implícitas por validaciones explícitas de la existencia de funciones, asegurando que `ctypes` no falle inesperadamente en entornos donde `kernel32` o `psapi` no exponen los métodos esperados.
- `2026-09-24T04:11:21` Gemini no devolvió un bloque de archivo válido para organizer.py (enfoque: manejo de errores y validación de entradas).
- `2026-09-24T04:12:04` Tests FALLARON:
```
PathError("Ruta de restauración insegura.")
    
            os.replace(str(stored_file), str(destination))
            remaining = [i for i in items if i.item_id != item_id]
            save_manifest(remaining, base)
            return destination
        except Exception as e:
>           raise RuntimeError(f"Error crítico en proceso de restauración: {e}")
E           RuntimeError: Error crítico en proceso de restauración: [GENERIC] Destino de restauración protegido por sistema.

app/quarantine.py:766: RuntimeError
=============================== warnings summary ===============================
evolve/tests/test_integrity.py::test_no_module_uses_package_style_imports
evolve/tests/test_integrity.py::test_no_new_third_party_dependencies
evolve/tests/test_integrity.py::test_boolean_misuse_of_ensure_is_not_present
evolve/tests/test_integrity.py::test_every_module_compiles
  /home/runner/work/limpieza-total-omega/limpieza-total-omega/app/safety.py:219: SyntaxWarning: invalid escape sequence '\P'
    """Detecta rutas de dispositivos de Windows (e.g., \\.\PhysicalDrive0) no aptas para archivos."""

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED evolve/tests/test_safety.py::test_restore_into_a_system_path_is_blocked - RuntimeError: Error crítico en proceso de restauración: [GENERIC] Destino de restauración protegido por sistema.
1 failed, 298 passed, 4 warnings in 1.17s

```
- `2026-09-24T04:12:04` ❌ Mejora descartada en quarantine.py (no pasó los tests), se revirtió. Intento: Se introdujo una gestión de errores más robusta y específica en las funciones críticas de persistencia del manifiesto (`save_manifest`) y restauración (`restore_item`), reemplazando excepciones genéricas por chequeos de pre-condiciones más claros y mensajes de error informativos, manteniendo la integridad del estado.
- `2026-09-24T04:12:11` 🛑 Propuesta bloqueada por la guardia en reporting.py (enfoque: manejo de errores y validación de entradas): error de sintaxis en la propuesta (línea 111): unterminated string literal (detected at line 111)
- `2026-09-24T04:12:11` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-09-24T04:12:11` Corrida terminada. Total usado hoy: 100.
