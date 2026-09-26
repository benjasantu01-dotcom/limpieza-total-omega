# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **196** (38.9% de aceptación)
- Rechazadas por tests: 17
- Rechazadas por guardia de seguridad: 35
- Sin cambios (nada sustancial que mejorar): 14
- Sin respuesta de la IA (error o límite): 242

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-09-25 | 95 | 8 | 18 | 7 | 128 |
| 2026-09-26 | 101 | 9 | 17 | 7 | 114 |

## Mejoras aceptadas por enfoque

- robustez ante casos límite: **43**
- manejo de errores y validación de entradas: **43**
- legibilidad y documentación: **43**
- seguridad defensiva: **37**
- rendimiento: **30**

## Mejoras aceptadas por archivo

- `diskreport.py`: **21**
- `scanner.py`: **18**
- `settings.py`: **18**
- `assistant.py`: **18**
- `safety.py`: **17**
- `healthscore.py`: **16**
- `duplicates.py`: **14**
- `quarantine.py`: **14**
- `memory.py`: **13**
- `organizer.py`: **11**
- `branding.py`: **10**
- `browser.py`: **10**
- `startup.py`: **9**
- `main.py`: **7**

## Últimas 15 mejoras aceptadas

- `2026-09-26T13:40:46` **diskreport.py** (legibilidad y documentación): Se introdujeron type hints más precisos (específicamente en el `defaultdict` de `_collect_summary_data`) y se documentó con mayor claridad el contrato de las funciones principales para asegurar la mantenibilidad a largo plazo sin alterar la lógica de escaneo.
- `2026-09-26T13:40:33` **browser.py** (legibilidad y documentación): Mejoré la documentación técnica mediante la adición de docstrings estructurados y precisos en las funciones críticas de detección y recorrido, clarificando las precondiciones de seguridad y el manejo de excepciones para facilitar el mantenimiento.
- `2026-09-26T13:30:15` **startup.py** (manejo de errores y validación de entradas): Reforcé la robustez de `parse_registry_csv` añadiendo una validación explícita para evitar errores cuando el CSV de PowerShell retorna filas mal formadas o encabezados inesperados, evitando que una entrada corrupta bloquee el procesamiento total.
- `2026-09-26T13:30:02` **settings.py** (manejo de errores y validación de entradas): Mejoré la robustez de `settings.py` implementando validaciones de entrada más estrictas y manejo explícito de errores de tipo en las funciones de conversión, asegurando que `validate` nunca propague excepciones hacia afuera.
- `2026-09-26T13:29:05` **safety.py** (manejo de errores y validación de entradas): Mejoré la robustez de `_get_file_attrs` y `_is_volume_readonly` añadiendo un manejo explícito de errores para evitar fallos catastróficos si la API de Windows devuelve estados inesperados o si las estructuras de memoria fallan, y centralicé la validación de `path_str` en `is_protected_path` para prevenir excepciones por tipos no válidos.
- `2026-09-26T13:19:09` **organizer.py** (manejo de errores y validación de entradas): Mejora la robustez de `_is_file_locked` para evitar falsos positivos y posibles bloqueos mediante una validación estricta de permisos de apertura y el manejo explícito de errores de acceso, asegurando que solo los archivos efectivamente bloqueados por el sistema sean reportados.
- `2026-09-26T13:18:42` **memory.py** (manejo de errores y validación de entradas): Mejoré la robustez de `parse_windows_process_csv` implementando una validación explícita para evitar errores de conversión en líneas mal formadas o vacías, garantizando que el recolector de procesos no falle ante datos de entrada inesperados.
- `2026-09-26T13:09:25` **healthscore.py** (manejo de errores y validación de entradas): Se ha mejorado la robustez de `compute_score` y `summarize` implementando validaciones de tipo explícitas y manejo defensivo de valores nulos o corruptos, garantizando que el motor de puntuación nunca falle ante entradas inesperadas.
- `2026-09-26T13:08:30` **diskreport.py** (manejo de errores y validación de entradas): Se reforzó la robustez de `_collect_summary_data` y las funciones públicas mediante la validación proactiva de `size_bytes` y la captura de errores en operaciones críticas de sistema, asegurando que el análisis no se interrumpa ante metadatos corruptos o cambios inesperados en el sistema de archivos durante el escaneo.
- `2026-09-26T12:59:39` **assistant.py** (manejo de errores y validación de entradas): Mejoré la robustez de `_extract_text_from_gemini_json` y `_call_gemini` para prevenir excepciones ante respuestas malformadas o inesperadas de la API, asegurando que el flujo siempre retorne un estado válido sin romper la ejecución.
- `2026-09-26T11:38:44` **startup.py** (seguridad defensiva): Se ha mejorado la robustez de `_is_valid_registry_entry` añadiendo una validación explícita mediante `is_protected_path` sobre la ruta resuelta después de limpiar el comando, previniendo que rutas maliciosas o fuera de los límites permitidos sean procesadas en caso de inyección de valores en el registro.
- `2026-09-26T11:37:06` **scanner.py** (seguridad defensiva): Se ha añadido un chequeo de bloqueo de acceso de lectura (`os.access(path, os.R_OK)`) dentro de `_is_safe_entry` y en las funciones de escaneo, garantizando que no se intenten analizar archivos que están siendo bloqueados por el sistema operativo o en uso exclusivo, mejorando la robustez defensiva ante errores de acceso.
- `2026-09-26T11:27:57` **safety.py** (seguridad defensiva): Se ha añadido una verificación de "puntos de reparse padre" en `ensure_safe_to_modify` para mitigar ataques de bypass de sandbox donde un directorio padre (que sí es seguro) contiene un punto de reparse que redirige silenciosamente a un sistema de archivos restringido.
- `2026-09-26T11:19:10` **main.py** (seguridad defensiva): Se reforzó la seguridad defensiva mediante la implementación de `_ensure_path_writable_and_clean` en los métodos de entrada de usuario (`on_target_choice_changed`), centralizando la validación contra puntos de reparse (junctions/symlinks) y rutas protegidas antes de realizar cualquier operación de disco o escaneo, cumpliendo estrictamente con el enfoque de seguridad defensiva.
- `2026-09-26T11:16:27` **duplicates.py** (seguridad defensiva): Se ha mejorado la robustez de `_collect_candidates` implementando una validación de seguridad anticipada mediante `is_safe_to_modify` antes de procesar cualquier entrada, asegurando que el escáner no intente ni siquiera obtener metadatos de rutas prohibidas que podrían disparar errores de acceso o violar el principio de mínima exposición a rutas sensibles.
