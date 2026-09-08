# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **214** (42.5% de aceptación)
- Rechazadas por tests: 18
- Rechazadas por guardia de seguridad: 35
- Sin cambios (nada sustancial que mejorar): 21
- Sin respuesta de la IA (error o límite): 216

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-09-07 | 145 | 13 | 25 | 16 | 129 |
| 2026-09-08 | 69 | 5 | 10 | 5 | 87 |

## Mejoras aceptadas por enfoque

- robustez ante casos límite: **48**
- seguridad defensiva: **48**
- manejo de errores y validación de entradas: **45**
- legibilidad y documentación: **40**
- rendimiento: **33**

## Mejoras aceptadas por archivo

- `settings.py`: **20**
- `assistant.py`: **20**
- `safety.py`: **19**
- `duplicates.py`: **18**
- `scanner.py`: **17**
- `memory.py`: **17**
- `quarantine.py`: **17**
- `healthscore.py`: **17**
- `browser.py`: **16**
- `branding.py`: **13**
- `main.py`: **12**
- `diskreport.py`: **10**
- `startup.py`: **10**
- `organizer.py`: **8**

## Últimas 15 mejoras aceptadas

- `2026-09-08T07:29:58` **startup.py** (manejo de errores y validación de entradas): Mejora la robustez del parseo CSV en `parse_registry_csv` asegurando que las filas vacías o mal formadas sean ignoradas explícitamente mediante la validación de `row` y evitando `StopIteration` u errores de acceso al intentar leer los nombres de campo.
- `2026-09-08T07:29:43` **settings.py** (manejo de errores y validación de entradas): Mejoré la robustez de `save()` capturando excepciones durante la creación del directorio y validando explícitamente que la ruta resuelta no sea un punto de reparse para prevenir escrituras fuera del alcance esperado, incluso si `is_safe_to_modify` ya hace chequeos básicos.
- `2026-09-08T07:29:13` **scanner.py** (manejo de errores y validación de entradas): Mejoré el manejo de errores en `Scanner.process_entry` y `scan_directory` validando explícitamente los parámetros y capturando excepciones de sistema de forma más granular para evitar que operaciones fallidas en archivos individuales interrumpan el flujo del escaneo.
- `2026-09-08T07:28:49` **safety.py** (manejo de errores y validación de entradas): Mejoré el manejo de errores en `_check_file_integrity` y `_validate_structural_safety` para evitar que excepciones inesperadas durante la inspección de archivos silencien violaciones de seguridad o interrumpan prematuramente el flujo de validación.
- `2026-09-08T07:19:41` **quarantine.py** (manejo de errores y validación de entradas): Mejoré la robustez de `quarantine_file` añadiendo una validación explícita para asegurar que la ruta original es absoluta y verificable, evitando excepciones genéricas al intentar manipular rutas relativas o mal formadas antes de iniciar el proceso de aislamiento.
- `2026-09-08T07:18:37` **memory.py** (manejo de errores y validación de entradas): Mejoré la robustez de `parse_windows_process_csv` agregando una validación explícita para evitar errores de tipo al procesar los campos extraídos, asegurando que `int()` solo se ejecute tras verificar la naturaleza del dato, evitando excepciones inesperadas en el bucle de parseo.
- `2026-09-08T07:09:14` **healthscore.py** (manejo de errores y validación de entradas): Mejoré la robustez de `SystemMetrics` mediante la adición de un chequeo explícito de tipos y valores nulos en `__post_init__`, evitando que valores no válidos propaguen estados erróneos hacia el pipeline de cálculo.
- `2026-09-08T07:08:46` **duplicates.py** (manejo de errores y validación de entradas): Mejoré la robustez de las funciones de cálculo de hash y los validadores de rutas añadiendo comprobaciones contra `None`, rutas vacías o errores de tipo, evitando que excepciones en el sistema de archivos detengan procesos críticos.
- `2026-09-08T06:59:23` **assistant.py** (manejo de errores y validación de entradas): Se ha mejorado la robustez de `_extract_text_from_gemini_json` implementando una validación de seguridad más rigurosa para evitar errores de ejecución ante respuestas de API malformadas o inesperadas, además de reforzar la integridad del bucle de parseo en `ingest` mediante la validación explícita de `float` y `int` para prevenir inyecciones de tipos no deseados.
- `2026-09-08T05:37:12` **settings.py** (seguridad defensiva): Se reforzó la seguridad de `save()` implementando una comprobación de integridad en la ruta base antes de cualquier operación de disco y garantizando que la creación de directorios sea atómica y segura mediante `ensure_safe_to_modify` para evitar escrituras en ubicaciones prohibidas.
- `2026-09-08T05:27:06` **quarantine.py** (seguridad defensiva): Mejoré la seguridad defensiva en `purge_all` y `quarantine_file` implementando una validación estricta de que el archivo a borrar o procesar no sea un enlace simbólico, reforzando la protección contra ataques de redirección de archivos fuera del sandbox.
- `2026-09-08T05:17:49` **main.py** (seguridad defensiva): Mejoré la seguridad defensiva en `on_trim_process` y `on_quarantine_findings` al implementar validaciones granulares de la ruta (`ensure_safe_to_modify`) justo antes de las operaciones de E/S, garantizando que ninguna ruta externa o manipulada por el usuario pueda saltarse los filtros de seguridad del sistema antes de procesar cambios.
- `2026-09-08T05:16:40` **healthscore.py** (seguridad defensiva): Se reforzó la robustez del sistema de inferencia agregando validaciones defensivas en `_evaluate_rules` para asegurar que las entradas sigan siendo íntegras y finitas durante la ejecución de las reglas, evitando posibles desbordamientos o comportamientos indefinidos al procesar métricas externas.
- `2026-09-08T05:16:14` **duplicates.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_collect_candidates` y `_is_valid_candidate` integrando el uso de `path.resolve()` antes de realizar chequeos de seguridad y garantizando que el acceso al disco sea consistente con las reglas de exclusión y protección, previniendo posibles ataques de *Time-of-Check Time-of-Use* (TOCTOU) y errores de validación de rutas mediante el uso consistente de `Path.resolve(strict=False)`.
- `2026-09-08T05:08:10` **browser.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_sum_directory_recursive` mediante la verificación estricta de que cada subdirectorio visitado se mantenga dentro de la `base_check_path` (si está definida), evitando posibles escapes de ruta durante la recursión profunda.
