# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **197** (39.1% de aceptación)
- Rechazadas por tests: 35
- Rechazadas por guardia de seguridad: 56
- Sin cambios (nada sustancial que mejorar): 21
- Sin respuesta de la IA (error o límite): 195

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-10-11 | 125 | 21 | 33 | 15 | 126 |
| 2026-10-12 | 72 | 14 | 23 | 6 | 69 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **46**
- seguridad defensiva: **45**
- robustez ante casos límite: **44**
- manejo de errores y validación de entradas: **33**
- rendimiento: **29**

## Mejoras aceptadas por archivo

- `browser.py`: **20**
- `memory.py`: **20**
- `diskreport.py`: **18**
- `safety.py`: **17**
- `organizer.py`: **16**
- `branding.py`: **15**
- `healthscore.py`: **15**
- `settings.py`: **15**
- `scanner.py`: **13**
- `assistant.py`: **13**
- `duplicates.py`: **11**
- `main.py`: **9**
- `startup.py`: **9**
- `quarantine.py`: **6**

## Últimas 15 mejoras aceptadas

- `2026-10-12T06:52:43` **settings.py** (seguridad defensiva): Se reforzó la seguridad del método `_is_file_secure_to_read` para prevenir ataques de denegación de servicio o manipulación mediante enlaces simbólicos, añadiendo una verificación explícita mediante `os.path.islink` sobre la ruta resuelta para evitar que el archivo de configuración sea un punto de reparse que apunte fuera del directorio seguro.
- `2026-10-12T06:41:53` **organizer.py** (seguridad defensiva): Se ha mejorado la robustez de `_is_safe_for_disk_op` al integrar explícitamente `is_protected_path` sobre la ruta de destino resuelta, evitando que el movimiento a directorios críticos sea posible incluso si el usuario configura mal la carpeta de revisión.
- `2026-10-12T06:41:05` **memory.py** (seguridad defensiva): Se ha mejorado la robustez del manejo de handles de procesos al incluir un chequeo de `INVALID_HANDLE_VALUE` explícito y asegurarse de que `CloseHandle` solo se invoque sobre handles válidos, evitando posibles errores de acceso a memoria o estados indefinidos en la API de Win32.
- `2026-10-12T06:32:05` **healthscore.py** (seguridad defensiva): Se endureció la seguridad defensiva de `_sanitize_msg` para prevenir el escape de información sensible mediante la inyección de caracteres de control o secuencias de escape ANSI, y se mejoró la resiliencia del `PipelineEntry` asegurando que los resultados numéricos de las reglas de diagnóstico no propaguen errores de precisión aritmética.
- `2026-10-12T06:31:34` **duplicates.py** (seguridad defensiva): Se reforzó la seguridad defensiva centralizando la validación de archivos en `_is_valid_candidate` para incluir explícitamente `is_protected_path(path)`, asegurando que ningún archivo del sistema sea siquiera procesado durante la fase de hash o escaneo, incluso si los permisos del SO permitirían la lectura.
- `2026-10-12T06:30:53` **diskreport.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_is_excluded_path` añadiendo un chequeo explícito de puntos de reparse (Junctions/Symlinks de sistema) para asegurar que el escaneo no escape accidentalmente del directorio objetivo ni siga enlaces cíclicos o fuera de los límites permitidos.
- `2026-10-12T06:22:50` **browser.py** (seguridad defensiva): Se ha restringido el acceso a directorios durante el escaneo para evitar la evaluación de rutas que excedan el límite de profundidad del sistema de archivos (`MAX_PATH_LEN`), mejorando la seguridad defensiva al prevenir el procesamiento de rutas maliciosamente profundas o cíclicas.
- `2026-10-12T06:22:20` **branding.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `save_logo_svg` consolidando el orden de validación de rutas y añadiendo un chequeo preventivo de `target.exists()` para asegurar que las operaciones de manipulación de archivos sigan estrictamente el flujo de seguridad, evitando efectos secundarios si el sistema de archivos es inconsistente.
- `2026-10-12T06:21:41` **assistant.py** (seguridad defensiva): Reforcé la seguridad en `_call_gemini` validando la integridad de la URL antes de ejecutar la petición, previniendo el riesgo de que una configuración maliciosa inyecte parámetros o apunte a un endpoint no autorizado.
- `2026-10-12T06:12:04` **scanner.py** (robustez ante casos límite): Se ha mejorado la robustez de las heurísticas ante nombres de archivos con caracteres inválidos o Unicode inesperado, añadiendo un manejo de excepciones más granular en `check_double_extension` y `check_system_lookalike` para evitar que un nombre de archivo malformado detenga el procesamiento completo del directorio.
- `2026-10-12T06:11:28` **safety.py** (robustez ante casos límite): Se ha añadido `_is_volume_busy_or_invalid` para validar proactivamente si una ruta reside en un volumen que el sistema operativo considera inaccesible (ej. dispositivo de solo lectura por hardware o volumen con error de E/S), mejorando la robustez frente a bloqueos inesperados.
- `2026-10-12T06:04:52` **organizer.py** (robustez ante casos límite): Se reforzó la robustez de `_is_file_locked` para manejar adecuadamente casos de permisos denegados o archivos inaccesibles en sistemas Windows, evitando excepciones no controladas durante el escaneo y garantizando que el uso de `ctypes` no interrumpa el bucle principal.
- `2026-10-12T06:04:05` **memory.py** (robustez ante casos límite): Mejora la robustez de `top_memory_processes` añadiendo un manejo de excepciones más granular ante procesos que terminan durante la iteración (`ERROR_ACCESS_DENIED` o `OSError` al consultar), evitando que el bucle completo falle ante la desaparición de un proceso efímero.
- `2026-10-12T05:50:46` **diskreport.py** (robustez ante casos límite): Mejoré la robustez de `walk_files` y `largest_folders` ante casos límite mediante la validación estricta de rutas relativas y la captura de excepciones durante la resolución de `Path`, evitando el procesamiento de nodos huérfanos o con rutas resueltas fuera del árbol objetivo.
- `2026-10-12T05:50:18` **browser.py** (robustez ante casos límite): Se reforzó la robustez ante errores de acceso a archivos al añadir una validación de `os.access` con `os.R_OK` antes de intentar realizar operaciones de lectura, evitando excepciones innecesarias en carpetas protegidas por el SO o con permisos denegados.
