<!-- Log rotado el 2026-10-14 14:31:18. Las 1163 líneas anteriores están en archive/evolve_log-20261014-143118.md -->

E       ^^^^^^^^^^^^^^^^^^^^^^^
E   SyntaxError: name '_FULL_SCAN_CACHE' is used prior to global declaration
=========================== short test summary info ============================
ERROR evolve/tests/test_modules.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.37s

```
- `2026-10-14T11:36:07` ❌ Mejora descartada en startup.py (no pasó los tests), se revirtió. Intento: Optimicé el rendimiento de `list_startup_entries` y `entries_from_folders` evitando la resolución redundante de rutas mediante el uso de un conjunto de rutas normalizadas (`seen_paths`), acelerando el escaneo al procesar cada archivo exactamente una vez.
- `2026-10-14T11:37:03` ✅ Mejora aceptada en assistant.py (enfoque: robustez ante casos límite). Mejora la robustez ante estados inesperados o corrupciones en la entrada mediante la implementación de `_get_safe_attr` con validación de tipos, evitando que el acceso a datos (especialmente en `ingest` y `_build_payload`) lance excepciones por atributos inesperados o tipos no soportados.
- `2026-10-14T11:37:21` Gemini no devolvió un bloque de archivo válido para branding.py (enfoque: robustez ante casos límite).
- `2026-10-14T11:37:21` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T11:37:21` Corrida terminada. Total usado hoy: 276.
- `2026-10-14T11:45:31` Arrancando corrida. Quedan hoy ~24 peticiones objetivo.
- `2026-10-14T11:45:59` ✅ Mejora aceptada en browser.py (enfoque: robustez ante casos límite). Mejoré la robustez ante casos límite en `_sum_directory_recursive` implementando un control de profundidad basado en el sistema de archivos real y un manejo de errores más específico para evitar que fallos en subdirectorios aislados (como el acceso denegado a una carpeta del sistema de archivos no crítica) interrumpan el cálculo del tamaño total del caché.
- `2026-10-14T11:46:25` ✅ Mejora aceptada en diskreport.py (enfoque: robustez ante casos límite). Mejora la robustez ante la concurrencia y los errores de sistema al introducir `is_file()` y `is_dir()` con chequeo explícito de estados "en uso" (capturando `OSError` 32 - `ERROR_SHARING_VIOLATION` en Windows), evitando que `walk_files` se interrumpa inesperadamente por bloqueos de archivos del sistema durante el escaneo.
- `2026-10-14T11:46:50` ✅ Mejora aceptada en duplicates.py (enfoque: robustez ante casos límite). Se introdujo una comprobación explícita de `OSError` al realizar el `stat()` en `group_by_size` y `_collect_candidates` para evitar que el proceso completo se detenga ante archivos con permisos restringidos o bloqueos temporales por el sistema operativo.
- `2026-10-14T11:47:07` ✅ Mejora aceptada en healthscore.py (enfoque: robustez ante casos límite). Se ha robustecido el motor de validación de `SystemMetrics` mediante la implementación de `math.isnan` explícito y una verificación de estado previo en `validate()`, asegurando que el motor de scoring no procese métricas corruptas incluso si los valores de entrada son inesperadamente inconsistentes o nulos tras la inicialización.
- `2026-10-14T11:47:07` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T11:47:07` Corrida terminada. Total usado hoy: 280.
- `2026-10-14T11:55:42` Arrancando corrida. Quedan hoy ~20 peticiones objetivo.
- `2026-10-14T11:56:52` ➖ Sin cambios en main.py (enfoque: robustez ante casos límite). Motivo: Se reforzó la robustez ante errores de entrada del usuario en `on_trim_process` y `on_restore_quarantine` mediante validaciones adicionales de existencia y tipado antes de proceder con las operaciones de disco, evitando posibles excepciones de `TclError` o valores nulos no controlados.
- `2026-10-14T11:57:21` ✅ Mejora aceptada en memory.py (enfoque: robustez ante casos límite). Se ha añadido un robusto manejo de errores en `top_memory_processes` para evitar excepciones silenciosas ante procesos que desaparecen o deniegan acceso durante la enumeración, asegurando que la lista de candidatos sea siempre íntegra.
- `2026-10-14T11:57:46` Gemini no devolvió un bloque de archivo válido para organizer.py (enfoque: robustez ante casos límite).
- `2026-10-14T11:58:13` Tests FALLARON:
```
a: el hash del archivo copiado no coincide.
FAILED evolve/tests/test_safety.py::test_restore_into_a_system_path_is_blocked - RuntimeError: Error durante aislamiento: Error durante aislamiento: Error crítico en transferencia: Falla durante operación I/O de copia: Falla crítica: el hash del archivo copiado no coincide.
FAILED evolve/tests/test_safety.py::test_purge_item_cannot_delete_outside_the_quarantine - RuntimeError: Error durante aislamiento: Error durante aislamiento: Error crítico en transferencia: Falla durante operación I/O de copia: Falla crítica: el hash del archivo copiado no coincide.
FAILED evolve/tests/test_safety.py::test_purge_all_only_deletes_inside_the_quarantine - RuntimeError: Error durante aislamiento: Error durante aislamiento: Error crítico en transferencia: Falla durante operación I/O de copia: Falla crítica: el hash del archivo copiado no coincide.
FAILED evolve/tests/test_safety.py::test_quarantine_two_files_with_the_same_name_do_not_collide - RuntimeError: Error durante aislamiento: Error durante aislamiento: Error crítico en transferencia: Falla durante operación I/O de copia: Falla crítica: el hash del archivo copiado no coincide.
FAILED evolve/tests/test_safety.py::test_quarantine_summary_reports_size_and_origin - RuntimeError: Error durante aislamiento: Error durante aislamiento: Error crítico en transferencia: Falla durante operación I/O de copia: Falla crítica: el hash del archivo copiado no coincide.
8 failed, 291 passed, 4 warnings in 2.28s

```
- `2026-10-14T11:58:13` ❌ Mejora descartada en quarantine.py (no pasó los tests), se revirtió. Intento: Se reforzó la robustez de `quarantine_file` ante casos de fallo en el sistema de archivos durante la persistencia, añadiendo una verificación de existencia y consistencia post-escritura más estricta antes de confirmar el registro en el manifiesto, evitando así estados inconsistentes si el I/O falla silenciosamente.
- `2026-10-14T11:58:13` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T11:58:13` Corrida terminada. Total usado hoy: 284.
- `2026-10-14T12:05:55` Arrancando corrida. Quedan hoy ~16 peticiones objetivo.
- `2026-10-14T12:06:16` 🛑 Propuesta bloqueada por la guardia en reporting.py (enfoque: robustez ante casos límite): error de sintaxis en la propuesta (línea 105): unterminated string literal (detected at line 105)
- `2026-10-14T12:07:07` ✅ Mejora aceptada en safety.py (enfoque: robustez ante casos límite). Se añadió una validación específica para detectar rutas que intentan acceder a la raíz del sistema de archivos (`C:\`, `D:\`) mediante el chequeo de `Path.parts` y `anchor`, mejorando la robustez ante intentos de manipular la raíz de unidades de disco, lo cual es crítico para evitar operaciones destructivas no deseadas.
- `2026-10-14T12:07:34` ✅ Mejora aceptada en scanner.py (enfoque: robustez ante casos límite). Mejora la robustez del escaneo frente a nombres de archivo con caracteres Unicode o no normalizados mediante el uso de `Path.name` en lugar de manipulación directa de strings en los chequeos, y añade una validación de `Path.exists()` preventiva dentro de `_is_safe_entry` para evitar excepciones por condiciones de carrera en sistemas de archivos dinámicos.
- `2026-10-14T12:07:49` Gemini no devolvió un bloque de archivo válido para settings.py (enfoque: robustez ante casos límite).
- `2026-10-14T12:07:50` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T12:07:50` Corrida terminada. Total usado hoy: 288.
- `2026-10-14T12:16:07` Arrancando corrida. Quedan hoy ~12 peticiones objetivo.
- `2026-10-14T12:16:37` Tests FALLARON:
```
.py:660: AssertionError
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
  /home/runner/work/limpieza-total-omega/limpieza-total-omega/app/safety.py:358: SyntaxWarning: invalid escape sequence '\)'
    """Comprueba si la ruta accede directamente a dispositivos (namespace \\.\)."""

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED evolve/tests/test_modules.py::test_executable_extracted_from_quoted_command - AssertionError: assert '' == 'C:\\Program ...\App\\app.exe'
  
  - C:\Program Files\App\app.exe
FAILED evolve/tests/test_modules.py::test_executable_extracted_from_unquoted_command - AssertionError: assert '' == '/usr/bin/app'
  
  - /usr/bin/app
2 failed, 297 passed, 4 warnings in 1.61s

```
- `2026-10-14T12:16:37` ❌ Mejora descartada en startup.py (no pasó los tests), se revirtió. Intento: Mejoré la robustez ante rutas corruptas o nombres de archivos inválidos en `_extract_quoted_path` y `_resolve_and_cache_path` mediante la validación del largo del path y el uso de `Path.exists()` con manejo de excepciones explícito para evitar fallos catastróficos en el bucle principal.
- `2026-10-14T12:16:42` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T12:17:24` ✅ Mejora aceptada en assistant.py (enfoque: seguridad defensiva). Se endureció la seguridad de `_is_safe_key` y `_get_source_value` para prevenir ataques de inyección mediante la manipulación de objetos fuente, impidiendo explícitamente el acceso a atributos ocultos o dunder methods que podrían haber sido omitidos en la validación original, asegurando que solo los campos definidos en `_VALIDATORS` sean procesados.
- `2026-10-14T12:17:57` Gemini no devolvió un bloque de archivo válido para branding.py (enfoque: seguridad defensiva).
- `2026-10-14T12:18:11` ✅ Mejora aceptada en browser.py (enfoque: seguridad defensiva). Se reforzó la seguridad defensiva en `_is_valid_cache_path` y `_resolve_browser_path` para garantizar que no solo se valide la existencia de las rutas, sino que se verifique explícitamente su naturaleza como subdirectorios del sandbox mediante `is_safe_to_modify` antes de cualquier procesamiento, eliminando la posibilidad de procesar rutas externas que pudieran haber eludido chequeos previos.
- `2026-10-14T12:18:11` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T12:18:11` Corrida terminada. Total usado hoy: 292.
- `2026-10-14T12:26:20` Arrancando corrida. Quedan hoy ~8 peticiones objetivo.
- `2026-10-14T12:26:49` ✅ Mejora aceptada en diskreport.py (enfoque: seguridad defensiva). Se reforzó la seguridad defensiva en `walk_files` y `_is_excluded_path` añadiendo una comprobación explícita para evitar seguir puntos de reparse (reparse points) o junctions de Windows, previniendo recursiones infinitas o saltos fuera del volumen analizado.
- `2026-10-14T12:27:29` ✅ Mejora aceptada en duplicates.py (enfoque: seguridad defensiva). Se reforzó la seguridad defensiva en la detección de archivos duplicados implementando un chequeo explícito de "puntos de reanálisis" (`reparse points`) y enlaces simbólicos directamente en `os.scandir` para impedir que la recursión escape del contexto definido, y añadiendo una validación adicional contra la lista de rutas protegidas antes de realizar cualquier operación de hashing.
- `2026-10-14T12:27:56` ✅ Mejora aceptada en healthscore.py (enfoque: seguridad defensiva). Mejoré la seguridad defensiva del motor de salud fortaleciendo la validación de entrada en los `message_factory` mediante la creación de un envoltorio de filtrado (`_apply_rule_safely`), asegurando que cualquier mensaje generado por las reglas se sanitice antes de entrar al flujo de la UI, evitando posibles inyecciones de texto malicioso o caracteres inesperados.
- `2026-10-14T12:28:51` ✅ Mejora aceptada en main.py (enfoque: seguridad defensiva). Se reforzó la seguridad defensiva en `main.py` encapsulando las operaciones que manipulan el sistema de archivos dentro de un nuevo decorador `@ensure_safety` aplicado a todos los métodos críticos (análisis, aislamiento, borrado, guardado), garantizando que cualquier intento de interactuar con rutas inseguras sea abortado inmediatamente por `safety.ensure_safe_to_modify` antes de ejecutar la lógica de negocio.
- `2026-10-14T12:28:51` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T12:28:51` Corrida terminada. Total usado hoy: 296.
- `2026-10-14T12:36:31` Arrancando corrida. Quedan hoy ~4 peticiones objetivo.
- `2026-10-14T12:37:09` Tests FALLARON:
```
    procesos = [memory.ProcessMemory("pesado", 99, 1024 * 1024 * 300)]
        texto = " ".join(memory.diagnose(memory.MemorySnapshot(1000, 100), procesos))
>       assert "pesado" in texto
E       AssertionError: assert 'pesado' in 'Memoria total: 1000 B En uso: 900 B (90.0%) Disponible: 100 B (10.0%) Estado: ajustado. Conviene cerrar aplicaciones innecesarias.   Mayor consumo: Proceso 99 — 300.0 MB'

evolve/tests/test_modules.py:396: AssertionError
=============================== warnings summary ===============================
evolve/tests/test_integrity.py::test_no_module_uses_package_style_imports
evolve/tests/test_integrity.py::test_no_new_third_party_dependencies
evolve/tests/test_integrity.py::test_boolean_misuse_of_ensure_is_not_present
evolve/tests/test_integrity.py::test_every_module_compiles
  /home/runner/work/limpieza-total-omega/limpieza-total-omega/app/safety.py:358: SyntaxWarning: invalid escape sequence '\)'
    """Comprueba si la ruta accede directamente a dispositivos (namespace \\.\)."""

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED evolve/tests/test_modules.py::test_diagnose_lists_top_processes - AssertionError: assert 'pesado' in 'Memoria total: 1000 B En uso: 900 B (90.0%) Disponible: 100 B (10.0%) Estado: ajustado. Conviene cerrar aplicaciones innecesarias.   Mayor consumo: Proceso 99 — 300.0 MB'
1 failed, 298 passed, 4 warnings in 1.55s

```
- `2026-10-14T12:37:09` ❌ Mejora descartada en memory.py (no pasó los tests), se revirtió. Intento: Se reforzó la seguridad defensiva en `memory.py` implementando una validación estricta de las rutas de ejecutables de procesos, asegurando que `_is_process_executable_safe` verifique mediante `is_protected_path` antes de permitir cualquier interacción, y evitando la fuga de nombres de procesos críticos en el informe final.
- `2026-10-14T12:37:40` ✅ Mejora aceptada en organizer.py (enfoque: seguridad defensiva). Mejoré la seguridad defensiva en `_process_directory` implementando una validación estricta de `entry.path` antes de la recursión, garantizando que ninguna ruta fuera de los límites permitidos pueda ser procesada o explorada, incluso si los filtros anteriores fallan.
- `2026-10-14T12:38:28` ✅ Mejora aceptada en quarantine.py (enfoque: seguridad defensiva). Mejoré la seguridad en la restauración de archivos validando explícitamente que la ruta original reconstruida (`destination`) no sea un enlace simbólico, evitando así riesgos de seguridad por desbordamiento del directorio de destino (ZipSlip/Path Traversal) durante el proceso de restauración.
- `2026-10-14T12:38:32` 🛑 Propuesta bloqueada por la guardia en reporting.py (enfoque: seguridad defensiva): error de sintaxis en la propuesta (línea 106): unterminated string literal (detected at line 106)
- `2026-10-14T12:38:32` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T12:38:32` Corrida terminada. Total usado hoy: 300.
- `2026-10-14T12:46:40` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T12:47:50` ✅ Mejora aceptada en safety.py (enfoque: seguridad defensiva). Se ha añadido una verificación de "punto de reparse" en el árbol de directorios que impide que la aplicación siga o manipule recursivamente directorios que son enlaces NTFS (junctions o symlinks), evitando que la recursión de `organizer.py` o `scanner.py` escape accidentalmente del sandbox configurado hacia otras partes del sistema de archivos.
- `2026-10-14T12:48:16` 🛑 Propuesta bloqueada por la guardia en scanner.py (enfoque: seguridad defensiva): desaparecieron símbolos que existían antes: Scanner._is_inside_base_root
- `2026-10-14T12:48:50` ➖ Sin cambios en settings.py (enfoque: seguridad defensiva). Motivo: Se ha restringido el acceso a la escritura de archivos de configuración mediante una validación estricta de permisos de usuario (`st_uid`), asegurando que solo el propietario del proceso actual pueda manipular el archivo, mitigando riesgos de escalada de privilegios o manipulación por otros usuarios en sistemas multiusuario.
- `2026-10-14T12:49:05` ✅ Mejora aceptada en startup.py (enfoque: seguridad defensiva). Se ha mejorado la robustez defensiva de `startup.py` añadiendo la validación mediante `is_safe_to_modify` a la resolución de rutas en `_resolve_and_cache_path`, garantizando que solo se procesen rutas que no violen las políticas de seguridad del sistema antes de realizar cualquier operación de acceso a disco.
- `2026-10-14T12:49:05` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T12:49:05` Corrida terminada. Total usado hoy: 304.
- `2026-10-14T12:56:52` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T12:56:55` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T12:56:55` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T12:57:15` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T12:57:15` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T12:57:45` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T12:57:45` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T12:58:00` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T12:58:00` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T12:58:20` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T12:58:20` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T12:58:51` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T12:58:51` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T12:59:06` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T12:59:06` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T12:59:26` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T12:59:26` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T12:59:56` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T12:59:56` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:00:11` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:00:11` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:00:32` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:00:32` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:01:02` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:01:02` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:01:02` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T13:01:02` Corrida terminada. Total usado hoy: 308.
- `2026-10-14T13:07:06` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T13:07:08` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:07:08` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:07:28` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:07:28` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:07:59` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:07:59` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:08:14` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:08:14` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:08:34` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:08:34` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:09:04` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:09:04` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:09:19` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:09:19` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:09:39` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:09:39` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:10:09` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:10:09` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:10:25` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:10:25` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:10:45` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:10:45` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:11:15` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:11:15` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:11:15` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T13:11:15` Corrida terminada. Total usado hoy: 312.
- `2026-10-14T13:17:19` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T13:17:21` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:17:21` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:17:41` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:17:41` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:18:12` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:18:12` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:18:27` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:18:27` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:18:47` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:18:47` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:19:17` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:19:17` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:19:32` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:19:32` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:19:52` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:19:52` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:20:23` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:20:23` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:20:38` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:20:38` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:20:58` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:20:58` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:21:28` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:21:28` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:21:28` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T13:21:28` Corrida terminada. Total usado hoy: 316.
- `2026-10-14T13:27:30` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T13:27:32` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:27:32` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:27:52` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:27:52` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:28:22` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:28:22` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:28:37` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:28:37` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:28:57` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:28:57` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:29:28` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:29:28` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:29:43` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:29:43` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:30:03` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:30:03` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:30:33` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:30:33` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:30:48` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:30:48` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:31:08` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:31:08` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:31:39` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:31:39` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:31:39` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T13:31:39` Corrida terminada. Total usado hoy: 320.
- `2026-10-14T13:37:40` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T13:37:44` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:37:44` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:38:04` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:38:04` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:38:34` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:38:34` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:38:49` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:38:49` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:39:09` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:39:09` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:39:40` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:39:40` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:39:55` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:39:55` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:40:15` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:40:15` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:40:45` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:40:45` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:41:00` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:41:00` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:41:20` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:41:20` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:41:51` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:41:51` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:41:51` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T13:41:51` Corrida terminada. Total usado hoy: 324.
- `2026-10-14T13:47:51` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T13:47:54` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:47:54` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:48:14` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:48:14` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:48:44` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:48:44` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:48:59` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:48:59` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:49:19` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:49:19` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:49:50` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:49:50` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:50:05` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:50:05` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:50:25` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:50:25` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:50:55` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:50:55` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:51:10` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:51:10` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:51:30` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:51:30` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:52:00` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:52:00` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:52:00` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T13:52:00` Corrida terminada. Total usado hoy: 328.
- `2026-10-14T13:58:04` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T13:58:07` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:58:07` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:58:27` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:58:27` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T13:58:57` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:58:57` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T13:59:12` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:59:12` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T13:59:33` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T13:59:33` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T14:00:03` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:00:03` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T14:00:18` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:00:18` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T14:00:38` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:00:38` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T14:01:08` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:01:08` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T14:01:24` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:01:24` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T14:01:44` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:01:44` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T14:02:14` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:02:14` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T14:02:14` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T14:02:14` Corrida terminada. Total usado hoy: 332.
- `2026-10-14T14:08:27` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T14:08:30` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:08:30` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T14:08:51` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:08:51` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T14:09:21` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:09:21` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T14:09:36` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:09:36` Rate limit de Gemini (intento 1/2). Esperando 20s...
- `2026-10-14T14:09:56` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:09:56` Rate limit de Gemini (intento 2/2). Esperando 30s...
- `2026-10-14T14:10:26` Detalle del 429 de Gemini: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. ",     "stat
- `2026-10-14T14:10:26` Se agotaron los reintentos por rate limit. Se salta esta iteración.
- `2026-10-14T14:10:42` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:10:50` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:11:35` ✅ Mejora aceptada en assistant.py (enfoque: manejo de errores y validación de entradas). Mejoré el manejo de errores en `ingest` y `_apply_field` del `SystemContext` para evitar fallos silenciosos al procesar datos externos, asegurando que las actualizaciones de estado sean atómicas y robustas ante valores malformados.
- `2026-10-14T14:11:36` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:11:40` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:12:07` ✅ Mejora aceptada en branding.py (enfoque: manejo de errores y validación de entradas). Mejoré la robustez de `save_logo_svg` y `draw_gradient_bar` mediante la validación explícita de `canvas_x` y `canvas_y`, la sanitización de `destination` en `save_logo_svg` y la captura granular de excepciones, asegurando que valores `None` o numéricos inválidos no provoquen fallos en tiempo de ejecución.
- `2026-10-14T14:12:07` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T14:12:07` Corrida terminada. Total usado hoy: 336.
- `2026-10-14T14:18:32` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T14:19:01` Tests FALLARON:
```

tmp_path = PosixPath('/tmp/pytest-of-runner/pytest-1/test_detect_profiles_finds_inj0')

    def test_detect_profiles_finds_injected_cache_folders(tmp_path):
        cache = tmp_path / "Navegador" / "Default" / "Cache"
        cache.mkdir(parents=True)
        (cache / "dato.bin").write_bytes(b"a" * 500)
    
        encontrados = browser.detect_profiles(
            bases=[tmp_path],
            cache_paths={"Navegador Falso": r"Navegador\Default\Cache"},
        )
>       assert len(encontrados) == 1
E       assert 0 == 1
E        +  where 0 = len([])

evolve/tests/test_modules.py:739: AssertionError
=============================== warnings summary ===============================
evolve/tests/test_integrity.py::test_no_module_uses_package_style_imports
evolve/tests/test_integrity.py::test_no_new_third_party_dependencies
evolve/tests/test_integrity.py::test_boolean_misuse_of_ensure_is_not_present
evolve/tests/test_integrity.py::test_every_module_compiles
  /home/runner/work/limpieza-total-omega/limpieza-total-omega/app/safety.py:358: SyntaxWarning: invalid escape sequence '\)'
    """Comprueba si la ruta accede directamente a dispositivos (namespace \\.\)."""

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED evolve/tests/test_modules.py::test_detect_profiles_finds_injected_cache_folders - assert 0 == 1
 +  where 0 = len([])
1 failed, 298 passed, 4 warnings in 1.38s

```
- `2026-10-14T14:19:01` ❌ Mejora descartada en browser.py (no pasó los tests), se revirtió. Intento: Mejoré la robustez de `_is_valid_rel_path` y `_resolve_browser_path` añadiendo validación explícita contra entradas vacías o malformadas y evitando el uso de decoradores en funciones críticas de flujo, asegurando que las excepciones de tipo sean manejadas con mayor precisión sin enmascarar errores de lógica.
- `2026-10-14T14:19:31` ✅ Mejora aceptada en diskreport.py (enfoque: manejo de errores y validación de entradas). Reforcé la robustez de `walk_files` y `summarize` capturando excepciones específicas en la lectura de atributos de archivo y envolviendo la iteración completa del escáner en un bloque que garantiza que el sistema no se detenga ante archivos bloqueados o inaccesibles, mejorando la fiabilidad del reporte.
- `2026-10-14T14:19:32` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:19:37` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:19:46` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-10-14T14:19:58` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-10-14T14:20:14` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:20:22` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:20:42` ✅ Mejora aceptada en healthscore.py (enfoque: manejo de errores y validación de entradas). Mejora la robustez del motor de puntuación validando que las métricas recibidas no contengan valores `None` o `NaN` antes de ser procesadas en el pipeline, evitando excepciones inesperadas que antes se silenciaban de forma genérica.
- `2026-10-14T14:20:42` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T14:20:42` Corrida terminada. Total usado hoy: 340.
- `2026-10-14T14:28:49` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T14:28:52` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:28:56` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:29:02` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-10-14T14:29:15` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-10-14T14:29:36` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:29:39` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:29:49` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-10-14T14:30:01` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-10-14T14:30:17` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:30:20` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:30:27` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-10-14T14:30:39` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-10-14T14:30:55` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:30:59` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:31:05` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-10-14T14:31:18` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-10-14T14:31:18` Rotación — log: 1163 líneas archivadas; metrics: 4 registros archivados; 2 archivo(s) histórico(s) descartado(s)
- `2026-10-14T14:31:18` Corrida terminada. Total usado hoy: 344.
- `2026-10-14T14:38:52` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T14:38:55` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:39:18` 🛑 Propuesta bloqueada por la guardia en reporting.py (enfoque: manejo de errores y validación de entradas): error de sintaxis en la propuesta (línea 111): unterminated string literal (detected at line 111)
- `2026-10-14T14:39:18` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:39:22` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:39:28` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-10-14T14:39:41` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-10-14T14:40:00` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:40:04` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:40:11` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-10-14T14:40:32` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-10-14T14:40:47` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:41:06` ✅ Mejora aceptada en settings.py (enfoque: manejo de errores y validación de entradas). Mejoré la robustez de la función `validate` envolviendo el acceso a los validadores en un bloque `try-except` más específico y asegurando que las claves inesperadas en el JSON no interrumpan el proceso, garantizando que el esquema de configuración siempre mantenga la integridad.
- `2026-10-14T14:41:06` Rotación — metrics: 4 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T14:41:06` Corrida terminada. Total usado hoy: 348.
- `2026-10-14T14:49:10` Arrancando corrida. Quedan hoy ~0 peticiones objetivo.
- `2026-10-14T14:49:14` Gemini devolvió 503 (falla temporal del servidor, intento 1/3). Esperando 3s...
- `2026-10-14T14:49:17` Gemini devolvió 503 (falla temporal del servidor, intento 2/3). Esperando 6s...
- `2026-10-14T14:49:23` Gemini devolvió 503 (falla temporal del servidor, intento 3/3). Esperando 12s...
- `2026-10-14T14:49:36` Gemini sigue devolviendo 503 tras 3 reintentos. Se salta esta iteración.
- `2026-10-14T14:50:35` ✅ Mejora aceptada en assistant.py (enfoque: legibilidad y documentación). Mejoré la legibilidad y mantenibilidad de `assistant.py` mediante la refactorización de `SystemContext._apply_field` para simplificar su lógica de validación, extrayendo la conversión a `float` y la verificación de límites a una función auxiliar (`_is_valid_numeric_update`), eliminando así el anidamiento innecesario y reduciendo la complejidad ciclomática sin cambiar el comportamiento.
- `2026-10-14T14:50:35` Tope duro de presupuesto alcanzado en medio de la corrida. Freno.
- `2026-10-14T14:50:35` Rotación — metrics: 2 registros archivados; 1 archivo(s) histórico(s) descartado(s)
- `2026-10-14T14:50:35` Corrida terminada. Total usado hoy: 350.
- `2026-10-14T14:59:22` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T15:09:36` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T15:19:52` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T15:30:06` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T15:40:18` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T15:50:32` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T16:00:43` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T16:10:53` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T16:21:07` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T16:31:20` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T16:41:31` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T16:51:41` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T17:01:52` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T17:12:05` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T17:22:18` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T17:32:40` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T17:42:43` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T17:52:57` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T18:03:17` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T18:13:32` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T18:23:52` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T18:34:01` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T18:44:18` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T18:54:30` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T19:04:41` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T19:14:58` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T19:25:10` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T19:35:26` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T19:45:41` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T19:55:58` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T20:06:08` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T20:16:18` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T20:26:30` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T20:36:46` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T20:46:54` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T20:57:02` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T21:07:15` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T21:17:26` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T21:27:37` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T21:37:51` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T21:48:04` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T21:58:16` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T22:08:26` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T22:18:36` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T22:28:48` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T22:38:57` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T22:49:07` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T22:59:17` Presupuesto diario agotado (350 usados). Corte hasta mañana.
- `2026-10-14T23:09:27` Presupuesto diario agotado (350 usados). Corte hasta mañana.
