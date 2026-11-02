# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **174** (34.5% de aceptación)
- Rechazadas por tests: 13
- Rechazadas por guardia de seguridad: 57
- Sin cambios (nada sustancial que mejorar): 12
- Sin respuesta de la IA (error o límite): 248

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-10-31 | 0 | 0 | 0 | 0 | 10 |
| 2026-11-01 | 107 | 8 | 32 | 9 | 194 |
| 2026-11-02 | 67 | 5 | 25 | 3 | 44 |

## Mejoras aceptadas por enfoque

- robustez ante casos límite: **39**
- rendimiento: **36**
- manejo de errores y validación de entradas: **35**
- legibilidad y documentación: **34**
- seguridad defensiva: **30**

## Mejoras aceptadas por archivo

- `safety.py`: **19**
- `assistant.py`: **18**
- `healthscore.py`: **16**
- `browser.py`: **15**
- `quarantine.py`: **15**
- `memory.py`: **14**
- `diskreport.py`: **13**
- `scanner.py`: **13**
- `settings.py`: **12**
- `organizer.py`: **11**
- `startup.py`: **10**
- `branding.py`: **10**
- `duplicates.py`: **8**

## Últimas 15 mejoras aceptadas

- `2026-11-02T06:01:47` **quarantine.py** (robustez ante casos límite): Se ha mejorado la robustez de `quarantine_file` al introducir un chequeo de pre-condición que valida el estado del sistema de archivos antes de iniciar la transferencia atómica, previniendo condiciones de carrera donde el archivo de origen pudiera ser eliminado o reemplazado durante la fase de validación inicial.
- `2026-11-02T06:01:07` **organizer.py** (robustez ante casos límite): Mejoré la robustez de `_process_directory` implementando un manejo defensivo ante la profundidad de recursión, rutas con caracteres no decodificables y fallos de permisos específicos, asegurando que el proceso no aborte por errores en un único nodo del árbol de directorios.
- `2026-11-02T06:00:20` **memory.py** (robustez ante casos límite): Mejoré la robustez de `_extract_process_info` para manejar de forma segura nombres de proceso que contienen caracteres especiales o rutas largas, añadiendo una validación explícita mediante `is_protected_path` sobre la ruta del ejecutable para evitar lecturas no autorizadas o excepciones en procesos de sistema con privilegios elevados.
- `2026-11-02T05:50:01` **healthscore.py** (robustez ante casos límite): Se ha mejorado la robustez ante casos límite mediante una validación más estricta en `SystemMetrics.validate`, asegurando que valores como `inf` o `nan` no solo sean detectados por `math.isfinite` sino normalizados de forma segura a los mínimos definidos, además de proteger la integridad del `PipelineEntry` contra la posible falta de funciones de score (`scorer`) o reglas (`rules`).
- `2026-11-02T05:49:25` **diskreport.py** (robustez ante casos límite): Se ha mejorado la robustez de `_is_reparse_point` y `walk_files` para manejar correctamente errores de acceso en rutas con caracteres especiales o permisos restringidos que podrían interrumpir el escaneo, añadiendo una comprobación adicional mediante `os.access` y una gestión de excepciones más granular en el acceso a atributos.
- `2026-11-02T05:30:11` **scanner.py** (rendimiento): Se optimizó el rendimiento del escaneo recursivo reemplazando la creación repetitiva de objetos `Path` en el bucle principal de `Scanner.process_entry` por operaciones directas sobre `entry.path` (cadena), evitando el alto costo de instanciación y resolución de rutas en cada iteración del sistema de archivos.
- `2026-11-02T05:29:59` **safety.py** (rendimiento): Optimicé el rendimiento de `_get_file_attrs` y `is_protected_path` centralizando la normalización de rutas y reduciendo llamadas redundantes al sistema de archivos, además de reemplazar búsquedas lineales por `any()` con verificaciones directas en el set precalculado `_get_system_root_paths`.
- `2026-11-02T05:28:58` **quarantine.py** (rendimiento): Se optimizó el acceso a atributos de archivo y la gestión del manifiesto mediante la implementación de una caché con tiempo de vida (TTL) más agresivo y una validación de estado más eficiente, evitando llamadas innecesarias al sistema de archivos durante iteraciones de listas largas.
- `2026-11-02T05:21:11` **organizer.py** (rendimiento): Optimizé la búsqueda de archivos basura moviendo la verificación de extensiones a una operación de búsqueda de prefijo/suffix usando un `frozenset` y optimizando el ciclo de `_process_directory` para evitar llamadas redundantes a `Path()` y `exists()` sobre objetos que ya sabemos que son válidos vía `os.scandir`.
- `2026-11-02T05:21:00` **memory.py** (rendimiento): Optimicé el rendimiento de `_iter_process_memory` al pre-asignar el búfer de nombres de módulo fuera del bucle y eliminar la recolección innecesaria de nombres de procesos largos, además de mejorar la eficiencia del filtrado de PIDs usando un conjunto (`Set`) para las comprobaciones de exclusión.
- `2026-11-02T05:09:43` **duplicates.py** (rendimiento): Optimicé el método `_collect_candidates` utilizando un diccionario de `st_size` para evitar redundancias de lectura de disco y asegurar que los archivos se agrupen por tamaño de forma eficiente antes de realizar cualquier operación costosa de hashing.
- `2026-11-02T05:09:35` **diskreport.py** (rendimiento): Optimizé `walk_files` reemplazando la llamada costosa `path.resolve()` por `path.is_relative_to(root_path)` sobre la ruta base, evitando resoluciones de I/O innecesarias en cada iteración y reduciendo la presión sobre el sistema de archivos.
- `2026-11-02T05:09:07` **browser.py** (rendimiento): Optimicé el rendimiento de `detect_profiles` reutilizando una única instancia de `DirectoryScanner` y su `ScanContext` para todos los navegadores bajo una misma ruta base, evitando la re-inicialización innecesaria de estructuras de datos y reduciendo el uso de memoria en escaneos masivos.
- `2026-11-02T05:08:36` **branding.py** (rendimiento): Se optimizó `_get_scaled_poly` reemplazando la generación de tuplas mediante comprensión de listas con una lógica de pre-cálculo basada en constantes `SHIELD_BASE_COORDS`, reduciendo la carga de cómputo en el renderizado de la UI al eliminar el uso de `enumerate` y operaciones matemáticas dentro del loop en cada frame.
- `2026-11-02T04:59:36` **assistant.py** (rendimiento): Optimicé el acceso a los criterios de salud eliminando la reconstrucción de estructuras en tiempo de ejecución, reemplazando el uso de `tuple` y `list` temporales dentro del `cached_property` de `active_problems` por la lectura directa del mapa pre-calculado `_CRITERIOS_MAP`.
