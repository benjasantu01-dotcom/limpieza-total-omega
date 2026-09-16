# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **222** (44.0% de aceptación)
- Rechazadas por tests: 14
- Rechazadas por guardia de seguridad: 36
- Sin cambios (nada sustancial que mejorar): 12
- Sin respuesta de la IA (error o límite): 220

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-09-14 | 59 | 2 | 9 | 5 | 63 |
| 2026-09-15 | 154 | 12 | 26 | 5 | 153 |
| 2026-09-16 | 9 | 0 | 1 | 2 | 4 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **54**
- manejo de errores y validación de entradas: **46**
- robustez ante casos límite: **46**
- seguridad defensiva: **39**
- rendimiento: **37**

## Mejoras aceptadas por archivo

- `healthscore.py`: **21**
- `browser.py`: **20**
- `diskreport.py`: **20**
- `memory.py`: **19**
- `quarantine.py`: **19**
- `safety.py`: **17**
- `assistant.py`: **16**
- `settings.py`: **16**
- `duplicates.py`: **15**
- `scanner.py`: **14**
- `organizer.py`: **13**
- `branding.py`: **13**
- `main.py`: **11**
- `startup.py`: **8**

## Últimas 15 mejoras aceptadas

- `2026-09-16T00:33:48` **main.py** (seguridad defensiva): Se ha añadido una validación explícita mediante `is_safe_to_modify` dentro de la lógica del bucle en `on_stage` y `on_quarantine_findings` para garantizar que los elementos individuales de una lista de archivos procesados se filtren correctamente antes de cualquier intento de operación, evitando así errores de ejecución en el bucle si una ruta específica dentro de una lista fuera insegura.
- `2026-09-16T00:32:54` **healthscore.py** (seguridad defensiva): Se reforzó la integridad del pipeline de cálculo mediante la validación estricta de las métricas después de su procesamiento individual, asegurando que cualquier entrada maliciosa o corrupta no propague valores fuera de rango al score final.
- `2026-09-16T00:32:04` **diskreport.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_is_excluded_path` añadiendo un chequeo explícito de bits de reparse/puntos de montaje para prevenir la salida del volumen raíz y se mejoró la resiliencia contra errores de acceso al manejar `OSError` de forma más granular durante la recolección de atributos de archivos.
- `2026-09-16T00:29:29` **browser.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_sum_directory_recursive` mediante la verificación estricta de la ruta real antes de realizar cualquier operación de escaneo, garantizando que el escáner nunca escape del sandbox de `LOCALAPPDATA` incluso si se encuentran enlaces simbólicos o redirecciones maliciosas durante la recursión.
- `2026-09-16T00:29:18` **branding.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `save_logo_svg` reemplazando la verificación secuencial por una única llamada a `ensure_safe_to_modify` antes de cualquier operación de disco, evitando así condiciones de carrera y validando la integridad del destino antes de intentar crear directorios o escribir archivos.
- `2026-09-16T00:23:17` **startup.py** (robustez ante casos límite): Mejoré la robustez de `StartupEntry._resolve_and_cache_path` añadiendo un manejo explícito de rutas que contienen caracteres no válidos para el sistema de archivos de Windows (como `:` fuera de la unidad) que podrían causar excepciones al instanciar `Path`, asegurando que el bucle de escaneo no se interrumpa ante entradas de registro malformadas.
- `2026-09-16T00:12:48` **settings.py** (robustez ante casos límite): Mejoré la robustez de `save()` ante condiciones de carrera y sistemas de archivos con bloqueos (típicos en Windows) añadiendo una verificación explícita de `is_safe_to_modify` sobre el archivo destino antes de la escritura, asegurando que la ruta no sea un enlace de reparse o un objeto protegido, y centralizando la validación de integridad previa a la persistencia.
- `2026-09-16T00:12:33` **scanner.py** (robustez ante casos límite): Se ha mejorado la robustez de `_is_reparse_point` y `_safe_stat` al añadir un manejo explícito de errores para casos donde el archivo está en uso exclusivo o los permisos son insuficientes, evitando la propagación de excepciones durante el escaneo.
- `2026-09-16T00:12:07` **safety.py** (robustez ante casos límite): Se ha mejorado la robustez ante casos límite en la validación de integridad al agregar un chequeo de 'Long Path' (más allá del límite de la API estándar de Windows) mediante el prefijo `\\?\` en las llamadas a `GetFileAttributesW` y `CreateFileW`, previniendo errores de `OSError` cuando la IA encuentre rutas que excedan los 260 caracteres pero que sean válidas.
- `2026-09-15T15:09:20` **browser.py** (robustez ante casos límite): Se ha mejorado la robustez ante rutas corruptas o inexistentes en la composición de `_resolve_browser_path`, añadiendo un chequeo preventivo contra caracteres de control y longitudes excesivas antes de intentar instanciar `Path`.
- `2026-09-15T14:58:25` **scanner.py** (rendimiento): Optimizé el método `_is_safe_entry` eliminando llamadas costosas a `Path(entry.path).resolve()` dentro del bucle principal, reemplazándolas por una validación de prefijo de cadena basada en `self.base_root_str`, lo que reduce drásticamente las llamadas al sistema de archivos durante el recorrido.
- `2026-09-15T14:49:32` **quarantine.py** (rendimiento): Optimicé el acceso a metadatos y la lógica de `list_items` y `purge_all` transformando búsquedas de complejidad O(N) en O(1) mediante el uso de diccionarios (`set` y `dict`), reduciendo drásticamente las iteraciones redundantes y mejorando el rendimiento en escenarios con múltiples archivos en cuarentena.
- `2026-09-15T14:40:58` **memory.py** (rendimiento): Se ha optimizado la función `parse_windows_process_csv` reemplazando la creación de una lista intermedia y el uso de `strip()` repetitivo por una estructura de generador para reducir la huella de memoria y el tiempo de procesamiento al analizar la salida de PowerShell.
- `2026-09-15T14:37:42` **duplicates.py** (rendimiento): Optimizé el rendimiento de `_collect_candidates` utilizando un conjunto (`set`) para registrar las rutas visitadas antes de procesar, eliminando la necesidad de realizar `resolve()` repetitivos y llamadas adicionales a `stat` en el bucle principal, reduciendo drásticamente las operaciones de E/S.
- `2026-09-15T14:29:24` **browser.py** (rendimiento): Implementé la persistencia del diccionario `perf_cache` en el flujo principal de `detect_profiles` para reutilizar los resultados de cálculo de tamaño de directorios hijos comunes (como subcarpetas dentro de `User Data`), evitando escaneos redundantes y mejorando significativamente la performance en sistemas con múltiples navegadores basados en Chromium.
