# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **181** (35.9% de aceptación)
- Rechazadas por tests: 11
- Rechazadas por guardia de seguridad: 52
- Sin cambios (nada sustancial que mejorar): 26
- Sin respuesta de la IA (error o límite): 234

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-10-21 | 18 | 1 | 7 | 2 | 50 |
| 2026-10-22 | 131 | 9 | 35 | 19 | 156 |
| 2026-10-23 | 32 | 1 | 10 | 5 | 28 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **42**
- seguridad defensiva: **40**
- manejo de errores y validación de entradas: **38**
- robustez ante casos límite: **33**
- rendimiento: **28**

## Mejoras aceptadas por archivo

- `diskreport.py`: **20**
- `browser.py`: **16**
- `memory.py`: **16**
- `quarantine.py`: **16**
- `safety.py`: **16**
- `branding.py`: **15**
- `settings.py`: **15**
- `assistant.py`: **15**
- `healthscore.py`: **11**
- `scanner.py`: **11**
- `duplicates.py`: **9**
- `startup.py`: **8**
- `organizer.py`: **8**
- `main.py`: **5**

## Últimas 15 mejoras aceptadas

- `2026-10-23T03:15:39` **safety.py** (robustez ante casos límite): Se ha añadido una validación estricta en `_validate_structural_safety` para prevenir el uso de caracteres de control y secuencias Unicode de inversión de texto (RTL/LTR) en nombres de archivos, protegiendo contra ataques de spoofing visual donde una extensión `.exe` parece otra.
- `2026-10-23T03:13:52` **organizer.py** (robustez ante casos límite): Se mejora la robustez de `stage_for_review` y `delete_reviewed` al validar que las rutas destino no sean puntos de reparse (junctions) o enlaces simbólicos antes de realizar operaciones de E/S, evitando que la lógica de limpieza siga punteros fuera de la carpeta de destino segura.
- `2026-10-23T03:03:22` **healthscore.py** (robustez ante casos límite): Mejoré la robustez ante estados inesperados en `_calculate_area_contribution` y `_compute_cached` mediante la validación explícita de `weights` y el manejo preventivo de posibles divisiones por cero o desbordamientos durante el cálculo de puntos.
- `2026-10-23T02:54:06` **diskreport.py** (robustez ante casos límite): Se ha mejorado la robustez de `walk_files` ante rutas extremadamente largas o inválidas (que pueden disparar `OSError` fuera del alcance de los bloques `try` actuales) y se ha añadido un mecanismo de validación de caracteres de control en las rutas procesadas para prevenir posibles inyecciones de rutas o fallos en el sistema de archivos subyacente.
- `2026-10-23T02:53:31` **branding.py** (robustez ante casos límite): Se reforzó la robustez de `save_logo_svg` ante errores de entrada (`Path` con caracteres inválidos o tipos inesperados) y condiciones de carrera en el sistema de archivos, envolviendo la creación de directorios y la escritura en un bloque de control mejorado que asegura la integridad de la ruta antes de intentar cualquier operación de disco.
- `2026-10-23T02:52:56` **assistant.py** (robustez ante casos límite): Mejoré la robustez de `AssistantConfig` y `_call_gemini` para manejar correctamente casos donde la configuración sea inesperadamente `None` o presente valores parciales, evitando que la app falle ante archivos de configuración corruptos o incompletos, manteniendo siempre la seguridad del motor local como fallback.
- `2026-10-23T02:44:33` **startup.py** (rendimiento): Optimizé `list_startup_entries` mediante el uso de `itertools.chain` para evitar iteradores anidados y una gestión de memoria más eficiente en el escaneo de registros y carpetas.
- `2026-10-23T02:44:04` **settings.py** (rendimiento): Se optimizó el acceso a las configuraciones mediante la implementación de un caché de instancia (`self._cached_settings`) que evita lecturas repetitivas y validaciones costosas de disco, refrescándolo solo cuando el sistema detecta cambios mediante el monitoreo de metadatos del archivo.
- `2026-10-23T02:43:06` **safety.py** (rendimiento): Optimicé el rendimiento de `is_protected_path` eliminando la llamada a `.resolve()` (que realiza llamadas costosas al sistema de archivos) en favor de una comparación de strings y partes (`parts`), manteniendo la integridad mediante el uso de `pathlib.Path` y normalización previa.
- `2026-10-23T02:33:47` **quarantine.py** (rendimiento): Optimicé el cálculo del hash SHA256 implementando un sistema de caché basado en el `mtime` y tamaño del archivo (stat), evitando lecturas de disco innecesarias durante la validación de integridad cuando el archivo no ha sido modificado.
- `2026-10-23T02:32:31` **memory.py** (rendimiento): Optimicé el rendimiento de `_iter_process_memory` al pre-asignar el búfer de PIDs y reducir las llamadas a `ctypes` dentro del bucle, evitando el uso de iteradores pesados y mejorando la eficiencia en la enumeración de procesos.
- `2026-10-23T02:22:08` **diskreport.py** (rendimiento): Optimizé la función `walk_files` para reducir drásticamente el número de llamadas a `path.relative_to(root)` y las operaciones de resolución de rutas en el hot-path, utilizando un filtro de exclusión más directo y eficiente.
- `2026-10-23T02:13:31` **browser.py** (rendimiento): Optimicé el rendimiento de `_sum_directory_recursive` evitando llamadas repetitivas a `os.path.normcase` y `str(Path)` dentro del bucle, y reemplacé la validación de `NEVER_TOUCH` por una búsqueda más eficiente utilizando el conjunto normalizado en `ScanContext`.
- `2026-10-23T02:12:49` **assistant.py** (rendimiento): Optimicé el acceso a los criterios de salud mediante `lru_cache` y una tupla inmutable de problemas, evitando el cálculo redundante de cadenas formateadas y la iteración sobre `_CRITERIOS_SALUD` en cada acceso a la propiedad `active_problems`.
- `2026-10-23T02:12:08` **startup.py** (legibilidad y documentación): Se introdujeron type hints más precisos (específicamente en el uso de `Optional` y `Sequence`) y se añadieron docstrings detallados en las funciones de procesamiento de carpetas y registro para clarificar el propósito de las validaciones de seguridad aplicadas, mejorando la legibilidad técnica del código sin alterar su lógica.
