# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **255** (50.6% de aceptación)
- Rechazadas por tests: 13
- Rechazadas por guardia de seguridad: 25
- Sin cambios (nada sustancial que mejorar): 18
- Sin respuesta de la IA (error o límite): 193

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-07-31 | 49 | 0 | 5 | 7 | 41 |
| 2026-08-01 | 166 | 11 | 16 | 10 | 147 |
| 2026-08-02 | 40 | 2 | 4 | 1 | 5 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **66**
- rendimiento: **52**
- manejo de errores y validación de entradas: **47**
- robustez ante casos límite: **46**
- seguridad defensiva: **44**

## Mejoras aceptadas por archivo

- `quarantine.py`: **23**
- `organizer.py`: **20**
- `diskreport.py`: **20**
- `scanner.py`: **20**
- `settings.py`: **20**
- `healthscore.py`: **19**
- `main.py`: **19**
- `assistant.py`: **18**
- `browser.py`: **18**
- `safety.py`: **17**
- `branding.py`: **16**
- `startup.py`: **16**
- `duplicates.py`: **15**
- `memory.py`: **14**

## Últimas 15 mejoras aceptadas

- `2026-08-02T02:10:49` **healthscore.py** (seguridad defensiva): Se reforzó la seguridad defensiva mediante una validación explícita de finitud en `score_security` y `score_startup`, asegurando que cálculos aritméticos con datos de entrada potencialmente malintencionados o corruptos no propaguen valores `NaN` o `Inf` hacia el puntaje final, siguiendo la robustez exigida para un motor de cálculo puro.
- `2026-08-02T02:10:39` **duplicates.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_collect_candidates` y `_scan` implementando una validación explícita mediante `is_protected_path` antes de cualquier procesamiento de entrada, asegurando que incluso rutas maliciosas o enlaces simbólicos inusuales no sean seguidos o procesados erróneamente por el escáner.
- `2026-08-02T02:10:16` **diskreport.py** (seguridad defensiva): Mejoré la seguridad defensiva en `walk_files` y `largest_folders` validando explícitamente que las rutas resueltas residan bajo el directorio base, previniendo el escape del contexto (directory traversal) ante enlaces simbólicos o puntos de reparse que podrían omitir las restricciones de seguridad originales.
- `2026-08-02T02:09:52` **browser.py** (seguridad defensiva): Mejoré la seguridad defensiva de `directory_size` y `_is_valid_cache_path` añadiendo un chequeo explícito de profundidad máxima para prevenir ataques por recursión infinita o rutas excesivamente largas, además de asegurar que cada archivo procesado sea verificado contra `is_protected_path`.
- `2026-08-02T02:00:49` **branding.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `save_logo_svg` reemplazando la verificación directa de existencia por una validación estricta de la ruta destino mediante `ensure_safe_to_modify` antes de cualquier operación de escritura, asegurando que la ruta esté dentro de los permisos permitidos sin depender de la existencia previa del archivo.
- `2026-08-02T02:00:35` **assistant.py** (seguridad defensiva): Mejoré la seguridad defensiva en `_call_gemini` al forzar el truncamiento de la entrada (`question` y `context_text`) antes de enviarla a la API, evitando ataques de inyección de prompts mediante buffers excesivamente largos, y apliqué `_ensure_safe_text` a la entrada original para bloquear cualquier intento de envío de rutas o caracteres de control desde la interfaz de usuario.
- `2026-08-02T01:59:38` **settings.py** (robustez ante casos límite): Se añade una verificación estricta `ruta.is_file()` en la función `load` para asegurar que el archivo de configuración sea realmente un archivo y no un directorio con el mismo nombre, previniendo errores de `OSError` o `IsADirectoryError` durante la lectura.
- `2026-08-02T01:50:17` **scanner.py** (robustez ante casos límite): Se ha mejorado la robustez de `process_entry` ante condiciones de carrera y cambios en el sistema de archivos (Time-of-check to time-of-use), añadiendo validaciones de existencia mediante `exists()` y `stat()` antes de procesar cada entrada, evitando así excepciones por archivos eliminados o inaccesibles entre iteraciones.
- `2026-08-02T01:50:10` **safety.py** (robustez ante casos límite): Se ha añadido un chequeo de existencia de padre (`parent.exists()`) y se validó el caso de rutas no encontradas en `normalize` para prevenir excepciones críticas en sistemas donde las rutas pueden haber sido movidas o eliminadas por otros procesos durante la ejecución del bucle, aumentando la robustez ante condiciones de carrera.
- `2026-08-02T01:49:28` **quarantine.py** (robustez ante casos límite): Mejoré la robustez de `quarantine_file` ante fallos parciales durante la transferencia de archivos, añadiendo un manejo explícito de errores de disco lleno durante la escritura, previniendo estados inconsistentes entre el sistema de archivos y el manifiesto.
- `2026-08-02T01:40:44` **organizer.py** (robustez ante casos límite): Mejoré la robustez de `stage_for_review` al verificar que la ruta de origen y la de destino no sean la misma (evitando errores de bucle) y garantizando que el archivo sea un archivo regular antes de intentar abrirlo para verificar si está en uso.
- `2026-08-02T01:40:13` **main.py** (robustez ante casos límite): Se mejora la robustez ante la interacción del usuario al centralizar la validación de directorios en un método helper `_is_valid_dir` y aplicar esta verificación antes de cualquier operación de escaneo, evitando errores en tiempo de ejecución si el usuario navega a carpetas que luego son eliminadas o modificadas externamente por otros procesos.
- `2026-08-02T01:39:12` **healthscore.py** (robustez ante casos límite): Mejoré la robustez de `score_startup` y `score_security` ante casos límite donde los divisores o factores podrían causar resultados inesperados, asegurando que el cálculo sea siempre determinista incluso con datos de entrada atípicos o escalas no uniformes.
- `2026-08-02T01:30:01` **duplicates.py** (robustez ante casos límite): Se ha mejorado la robustez ante casos límite en la recolección de archivos (`_collect_candidates`) y en las funciones de hash, añadiendo validaciones explícitas de existencia (`exists()`) y manejo de errores ante cambios de estado del sistema de archivos durante la iteración (TOCTOU).
- `2026-08-02T01:29:52` **diskreport.py** (robustez ante casos límite): Se reforzó `walk_files` y `summarize` añadiendo un manejo explícito para `PermissionError` y `OSError` al obtener el tamaño del archivo, evitando que una denegación de acceso en un archivo puntual aborte el recorrido completo o genere un informe incompleto.
