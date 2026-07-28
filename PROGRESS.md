# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **229** (45.4% de aceptación)
- Rechazadas por tests: 20
- Rechazadas por guardia de seguridad: 29
- Sin cambios (nada sustancial que mejorar): 6
- Sin respuesta de la IA (error o límite): 220

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-07-27 | 146 | 16 | 19 | 3 | 144 |
| 2026-07-28 | 83 | 4 | 10 | 3 | 76 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **57**
- manejo de errores y validación de entradas: **53**
- seguridad defensiva: **49**
- robustez ante casos límite: **36**
- rendimiento: **34**

## Mejoras aceptadas por archivo

- `assistant.py`: **21**
- `diskreport.py`: **20**
- `organizer.py`: **19**
- `settings.py`: **19**
- `browser.py`: **18**
- `healthscore.py`: **18**
- `scanner.py`: **17**
- `safety.py`: **16**
- `duplicates.py`: **16**
- `main.py`: **16**
- `quarantine.py`: **15**
- `startup.py`: **15**
- `memory.py`: **10**
- `branding.py`: **9**

## Últimas 15 mejoras aceptadas

- `2026-07-28T07:21:19` **startup.py** (manejo de errores y validación de entradas): Mejoré la robustez de `parse_registry_csv` añadiendo validación estricta de tipos y manejo de errores ante entradas mal formadas en el CSV, asegurando que `name_raw` y `value_raw` siempre contengan datos válidos antes de procesarlos.
- `2026-07-28T07:21:11` **settings.py** (manejo de errores y validación de entradas): Mejoré la robustez de `save()` y `validate()` incorporando el manejo de errores ante entradas de tipo inesperado (None, tipos incorrectos) y asegurando que las operaciones de sistema dentro de bloques `try` sean atómicas y protegidas ante fallos de permisos o escritura parcial.
- `2026-07-28T07:20:47` **scanner.py** (manejo de errores y validación de entradas): Mejoré la robustez de `scan_directory` y `scan_file` mediante una validación más estricta de rutas, asegurando que `Path.resolve()` se envuelva en un bloque de manejo de errores específico para capturar fallos de acceso al sistema de archivos, y añadiendo chequeos de nulidad en las entradas del iterador.
- `2026-07-28T07:20:26` **safety.py** (manejo de errores y validación de entradas): Mejoré la robustez de `is_within_directory` y `ensure_safe_to_modify` añadiendo validaciones explícitas contra rutas vacías, `None` o mal formadas antes de procesar, evitando que `Path.resolve()` o `Path.parts` lancen excepciones inesperadas en entornos con permisos restringidos.
- `2026-07-28T07:11:03` **quarantine.py** (manejo de errores y validación de entradas): Se ha mejorado la robustez del manejo de archivos mediante la validación explícita de `Path` en las funciones críticas de entrada, evitando errores de tiempo de ejecución y asegurando que las operaciones de entrada/salida manejen rutas correctamente tipadas antes de interactuar con el sistema de archivos.
- `2026-07-28T07:10:37` **organizer.py** (manejo de errores y validación de entradas): Se ha mejorado la robustez de `stage_for_review` implementando una validación de parámetros más estricta (verificando `is_dir` sobre el destino) y añadiendo un manejo de excepciones más granular para evitar que una falla en un solo archivo detenga el proceso completo, asegurando que los recursos (como el manejo de archivos) sean manejados de manera segura.
- `2026-07-28T07:10:15` **memory.py** (manejo de errores y validación de entradas): Mejora la robustez de `parse_windows_process_csv` implementando validaciones preventivas contra entradas inesperadas, como valores `None` o nombres de proceso vacíos, asegurando que la función no falle silenciosamente ni procese datos inválidos en el bucle principal.
- `2026-07-28T07:01:57` **main.py** (manejo de errores y validación de entradas): Mejoré el manejo de errores en `on_trim_process` y `on_restore_quarantine` para asegurar que las entradas de usuario (PID e ID) se validen correctamente, evitando excepciones no controladas antes de llegar a la lógica de negocio.
- `2026-07-28T07:01:15` **healthscore.py** (manejo de errores y validación de entradas): Mejoré la robustez de `compute_score` validando que `metrics` no sea `None` y asegurando que las funciones de puntuación manejen casos extremos de forma explícita, evitando divisiones por cero o valores fuera de rango antes de que `_clamp` actúe.
- `2026-07-28T07:00:52` **duplicates.py** (manejo de errores y validación de entradas): Se reforzó la robustez de `suggest_keeper` y `reclaimable_bytes` añadiendo validaciones de tipo explícitas y manejando casos de rutas inexistentes durante la selección del archivo a conservar, evitando posibles errores en tiempo de ejecución.
- `2026-07-28T07:00:04` **diskreport.py** (manejo de errores y validación de entradas): Mejoré la robustez de `summarize` y las funciones auxiliares capturando potenciales errores de `format_size` y validaciones de entrada, asegurando que el informe sea informativo incluso ante valores inesperados o rutas mal formadas.
- `2026-07-28T06:51:42` **browser.py** (manejo de errores y validación de entradas): Mejoré la robustez de `detect_profiles` y `summarize` implementando una validación exhaustiva de tipos y estados para los parámetros opcionales (`bases` y `cache_paths`), previniendo errores de ejecución ante entradas mal formadas o nulas.
- `2026-07-28T06:51:34` **branding.py** (manejo de errores y validación de entradas): Mejoré la robustez de `save_logo_svg` y `draw_logo` validando explícitamente parámetros críticos y manejando fallos de ejecución sin interrumpir el flujo visual de la aplicación.
- `2026-07-28T06:51:05` **assistant.py** (manejo de errores y validación de entradas): Mejoré la robustez de `build_context` al añadir una validación de tipo más estricta para `metrics` y `health`, previniendo errores de `AttributeError` si se pasan objetos inesperados, y asegurando que las conversiones numéricas no fallen silenciosamente ante datos malformados.
- `2026-07-28T05:28:48` **startup.py** (seguridad defensiva): Se ha mejorado la seguridad defensiva en `entries_from_folders` añadiendo una validación explícita para asegurar que el archivo detectado no sea un "punto de reparse" (junction o symbolic link a directorios fuera del árbol esperado), previniendo así posibles ataques de escalada o desbordamiento de contexto al procesar archivos del sistema.
