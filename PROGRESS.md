# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **190** (37.7% de aceptación)
- Rechazadas por tests: 11
- Rechazadas por guardia de seguridad: 78
- Sin cambios (nada sustancial que mejorar): 21
- Sin respuesta de la IA (error o límite): 204

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-11-21 | 34 | 0 | 11 | 3 | 34 |
| 2026-11-22 | 120 | 11 | 53 | 17 | 149 |
| 2026-11-23 | 36 | 0 | 14 | 1 | 21 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **48**
- robustez ante casos límite: **41**
- manejo de errores y validación de entradas: **38**
- rendimiento: **33**
- seguridad defensiva: **30**

## Mejoras aceptadas por archivo

- `diskreport.py`: **22**
- `quarantine.py`: **20**
- `assistant.py`: **19**
- `memory.py`: **19**
- `safety.py`: **19**
- `healthscore.py`: **15**
- `scanner.py`: **15**
- `settings.py`: **15**
- `branding.py`: **12**
- `duplicates.py`: **12**
- `organizer.py`: **10**
- `startup.py`: **7**
- `browser.py`: **5**

## Últimas 15 mejoras aceptadas

- `2026-11-23T02:59:55` **scanner.py** (robustez ante casos límite): Se ha añadido un chequeo de disponibilidad de archivo en `_run_file_heuristics` utilizando `os.access` y capturando excepciones de sistema para evitar caídas ante archivos bloqueados por otros procesos (sharing violations).
- `2026-11-23T02:59:41` **safety.py** (robustez ante casos límite): Se ha añadido un chequeo de integridad en `_validate_high_level_constraints` para detectar archivos que se encuentran en el directorio raíz de un volumen (como `C:\`) o rutas que son solo el nombre de la unidad (ej. `C:`), evitando operaciones peligrosas o mal definidas sobre la jerarquía raíz del sistema de archivos.
- `2026-11-23T02:58:08` **quarantine.py** (robustez ante casos límite): Se mejora la robustez de `quarantine_file` agregando una validación previa de existencia del directorio destino (`quarantine_dir`) antes de cualquier operación de I/O, evitando excepciones innecesarias en entornos donde el sistema de archivos pueda haber cambiado su estado entre la inicialización de la app y la ejecución del comando.
- `2026-11-23T02:49:24` **memory.py** (robustez ante casos límite): Se mejora la robustez en `_iter_process_memory` para manejar fallos en la obtención de nombres de procesos (donde `GetModuleBaseNameW` puede fallar aunque el handle sea válido) y se asegura que el bucle de escaneo no se detenga prematuramente ante procesos con permisos restringidos.
- `2026-11-23T02:47:39` **healthscore.py** (robustez ante casos límite): Se reforzó la robustez del cálculo de `compute_score` ante valores inesperados en el pipeline y posibles inconsistencias en los pesos, asegurando que `total_score` nunca dependa de una división por cero y que el desglose sea resiliente.
- `2026-11-23T02:38:37` **duplicates.py** (robustez ante casos límite): Se ha mejorado la resiliencia de `is_file_locked` para manejar correctamente archivos inexistentes o inaccesibles sin propagar excepciones inesperadas, asegurando que el proceso de escaneo no se interrumpa ante cambios de estado concurrentes en el sistema de archivos.
- `2026-11-23T02:38:28` **diskreport.py** (robustez ante casos límite): Se ha añadido un manejo robusto de excepciones (bloques try-except granulares) en las funciones `largest_files`, `usage_by_extension`, `largest_folders` y `total_size` para evitar que el programa colapse ante rutas que se vuelven inaccesibles durante el escaneo o errores de permisos temporales en tiempo de ejecución.
- `2026-11-23T02:37:54` **browser.py** (robustez ante casos límite): Se reforzó la robustez ante errores de E/S en la jerarquía de archivos mediante la validación proactiva de la existencia de `target.parent` antes de realizar operaciones de resolución de ruta, evitando excepciones `FileNotFoundError` en entornos con perfiles de navegador incompletos o parcialmente desinstalados.
- `2026-11-23T02:29:42` **assistant.py** (robustez ante casos límite): Se reforzó la robustez de la ingesta de métricas al añadir un chequeo explícito de tipos y límites durante la deserialización de `SystemContext`, asegurando que valores inesperados (como `NaN`, `Inf` o tipos inválidos) no contaminen el estado interno del asistente y provoquen fallos en los cálculos de salud.
- `2026-11-23T02:28:42` **startup.py** (rendimiento): Optimizamos la consolidación de entradas en `list_startup_entries` sustituyendo la lógica de filtrado manual por una estructura de datos `dict` que utiliza el comando como clave, eliminando la necesidad de iteraciones extras y reduciendo la complejidad algorítmica de O(N) a O(1) por cada inserción.
- `2026-11-23T02:28:07` **settings.py** (rendimiento): Optimicé el rendimiento de `load()` y `save()` eliminando el cálculo redundante y costoso del hash SHA256 durante la lectura rutinaria, moviendo la verificación de integridad de contenido a una comparación de metadatos de archivo (st_ino, st_mtime, st_size) y usando el hash solo cuando los metadatos no son concluyentes o en el commit de escritura.
- `2026-11-23T02:27:03` **scanner.py** (rendimiento): Se optimizó el rendimiento del escaneo reemplazando la creación repetitiva de una tupla en `_is_valid_file_path` por la consulta directa al `frozenset` `SUSPICIOUS_ALL_EXTS` y se eliminó la conversión redundante a `Path` en el bucle principal de `process_entry`, evitando instancias de objetos innecesarias durante la iteración sobre miles de archivos.
- `2026-11-23T02:18:39` **safety.py** (rendimiento): Se ha optimizado la validación de rutas mediante la implementación de un mecanismo de "corta-fuego" de caché estructural, evitando la resolución costosa de rutas (Path.resolve) en bucles mediante el uso del caché `_STRUCTURAL_CACHE` y el filtrado preventivo en `is_safe_to_modify`.
- `2026-11-23T02:17:39` **quarantine.py** (rendimiento): Se optimizó el acceso al sistema de archivos en `purge_all` y en la carga del manifiesto mediante la consolidación de llamadas a `stat()`, evitando múltiples syscalls redundantes al cachear la información del `DirEntry` o del objeto existente.
- `2026-11-23T02:16:51` **organizer.py** (rendimiento): Optimizé la búsqueda de archivos basura evitando llamadas redundantes a `is_safe_to_modify` y `lstat` dentro de `_process_directory`, mediante una pre-validación del nombre y una reorganización de los chequeos para realizar las operaciones costosas (como el acceso a disco) solo cuando es estrictamente necesario.
