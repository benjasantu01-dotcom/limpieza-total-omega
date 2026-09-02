# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **250** (49.6% de aceptación)
- Rechazadas por tests: 12
- Rechazadas por guardia de seguridad: 34
- Sin cambios (nada sustancial que mejorar): 17
- Sin respuesta de la IA (error o límite): 191

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-09-01 | 89 | 2 | 12 | 6 | 59 |
| 2026-09-02 | 161 | 10 | 22 | 11 | 132 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **57**
- manejo de errores y validación de entradas: **56**
- seguridad defensiva: **48**
- robustez ante casos límite: **45**
- rendimiento: **44**

## Mejoras aceptadas por archivo

- `assistant.py`: **21**
- `browser.py`: **21**
- `memory.py`: **21**
- `quarantine.py`: **21**
- `safety.py`: **20**
- `settings.py`: **20**
- `organizer.py`: **19**
- `diskreport.py`: **18**
- `duplicates.py`: **18**
- `healthscore.py`: **17**
- `scanner.py`: **17**
- `main.py`: **15**
- `branding.py`: **13**
- `startup.py`: **9**

## Últimas 15 mejoras aceptadas

- `2026-09-02T14:19:47` **quarantine.py** (seguridad defensiva): Se reforzó la seguridad de `_atomic_isolate_file` implementando una validación estricta de permisos de escritura y atributos de sistema en el archivo temporal antes de consolidar el movimiento, previniendo posibles ataques de *Time-of-Check to Time-of-Use* (TOCTOU).
- `2026-09-02T14:19:25` **organizer.py** (seguridad defensiva): Se endureció la validación de seguridad en `stage_for_review` y `delete_reviewed` para asegurar que las operaciones de disco no se ejecuten si la ruta de destino reside accidentalmente dentro de una estructura jerárquica no permitida o si las restricciones de `is_protected_path` fallan en tiempo de ejecución.
- `2026-09-02T14:18:57` **memory.py** (seguridad defensiva): Mejoré la seguridad de `_get_process_path` y `_is_safe_to_trim` para evitar el manejo inseguro de handles y asegurar que la ruta del ejecutable se valide con `is_safe_to_modify` antes de cualquier operación, aplicando el principio de mínima exposición a procesos del sistema.
- `2026-09-02T14:18:27` **main.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `on_stage` y `on_delete_reviewed` al centralizar la validación de seguridad de la ruta mediante el método `_is_safe_path` antes de ejecutar las operaciones de disco, evitando así posibles errores de lógica si el estado de la carpeta de revisión cambiara inesperadamente durante la ejecución asíncrona.
- `2026-09-02T14:08:17` **duplicates.py** (seguridad defensiva): Mejoré la seguridad defensiva en `_collect_candidates` agregando una verificación explícita de `is_protected_path` sobre los directorios antes de ingresar a ellos, evitando así el procesamiento de subárboles restringidos (como puntos de reparse o rutas protegidas a nivel de carpeta) mediante un filtrado preventivo.
- `2026-09-02T14:07:49` **diskreport.py** (seguridad defensiva): Se ha mejorado la robustez defensiva en `walk_files` implementando una validación explícita mediante `is_protected_path` sobre `current_dir` antes de intentar iterar, evitando intentos de acceso a directorios bloqueados que podrían causar excepciones de permisos o recorridos no deseados en estructuras profundas.
- `2026-09-02T14:07:19` **browser.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_sum_directory_recursive` mediante la verificación explícita de `is_safe_to_modify` en cada nivel de la recursión, garantizando que el escaneo no se desvíe a rutas fuera del alcance permitido incluso si la estructura de directorios contiene enlaces o accesos inesperados.
- `2026-09-02T13:58:40` **branding.py** (seguridad defensiva): Se reforzó la seguridad de `save_logo_svg` validando la existencia de la ruta de destino antes de intentar crear directorios o escribir, evitando así posibles errores de acceso en rutas protegidas o mal formadas.
- `2026-09-02T13:58:22` **assistant.py** (seguridad defensiva): Reforcé la seguridad en `_call_gemini` añadiendo un chequeo explícito de la longitud del `payload` y validando que el `api_key` sea una cadena limpia antes de usarlo para construir la URL, evitando posibles errores de inyección o desbordamiento en la solicitud.
- `2026-09-02T13:57:16` **settings.py** (robustez ante casos límite): Mejoré la robustez de `save()` ante condiciones de carrera y fallos de sistema al asegurar que la carpeta de destino sea un directorio real antes de proceder y verificando la atomicidad de la operación en entornos donde el sistema de archivos pueda estar bloqueado o inaccesible temporalmente.
- `2026-09-02T13:48:05` **scanner.py** (robustez ante casos límite): Se ha añadido un robusto manejo de excepciones de lectura en `process_entry` y `scan_file`, asegurando que archivos bloqueados, con metadatos corruptos o nombres que exceden el buffer del sistema no interrumpan el flujo de escaneo, mejorando la resiliencia ante el entorno volátil del disco.
- `2026-09-02T13:47:55` **safety.py** (robustez ante casos límite): Se ha añadido una verificación de "error de acceso" en `_is_file_in_use` y se ha robustecido `_is_system_or_hidden` para manejar correctamente archivos inexistentes o bloqueados, evitando que la validación falle silenciosamente con excepciones no capturadas al intentar obtener atributos de sistemas en archivos con permisos restringidos.
- `2026-09-02T13:47:03` **quarantine.py** (robustez ante casos límite): Se mejora la robustez de `quarantine_file` ante condiciones de carrera y fallos de escritura mediante la verificación de la existencia de la carpeta destino, garantizando que el manifiesto solo se actualice tras la confirmación de persistencia exitosa y la integridad del archivo movido.
- `2026-09-02T13:43:53` **organizer.py** (robustez ante casos límite): Se ha mejorado la robustez de `_is_safe_for_disk_op` y `_can_move_file` al añadir una validación de longitud de ruta (`MAX_PATH`) y manejo de casos donde `resolve()` falla ante rutas inexistentes o inaccesibles, evitando así excepciones no capturadas durante operaciones críticas.
- `2026-09-02T13:41:48` **memory.py** (robustez ante casos límite): Mejoré la robustez de `parse_windows_process_csv` implementando una sanitización estricta de rutas mediante `is_protected_path` antes de procesar cada entrada, evitando que el escaneo de procesos sea engañado por nombres de archivos malformados o rutas sospechosas detectadas por la heurística.
