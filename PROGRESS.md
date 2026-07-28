# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **260** (51.6% de aceptación)
- Rechazadas por tests: 20
- Rechazadas por guardia de seguridad: 30
- Sin cambios (nada sustancial que mejorar): 7
- Sin respuesta de la IA (error o límite): 187

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-07-27 | 132 | 14 | 16 | 3 | 95 |
| 2026-07-28 | 128 | 6 | 14 | 4 | 92 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **64**
- seguridad defensiva: **60**
- manejo de errores y validación de entradas: **47**
- robustez ante casos límite: **47**
- rendimiento: **42**

## Mejoras aceptadas por archivo

- `assistant.py`: **25**
- `settings.py`: **23**
- `diskreport.py`: **22**
- `organizer.py`: **21**
- `browser.py`: **20**
- `scanner.py`: **20**
- `main.py`: **20**
- `healthscore.py`: **19**
- `duplicates.py`: **19**
- `quarantine.py`: **18**
- `safety.py`: **16**
- `startup.py`: **16**
- `memory.py`: **12**
- `branding.py`: **9**

## Últimas 15 mejoras aceptadas

- `2026-07-28T09:54:47` **settings.py** (seguridad defensiva): Se endureció la validación de seguridad en `settings_path` y `_validate_str` para evitar inyecciones de rutas o acceso a directorios prohibidos mediante la resolución absoluta de la ruta antes de cualquier operación de I/O.
- `2026-07-28T09:54:22` **scanner.py** (seguridad defensiva): Se añadió la validación de integridad mediante `resolve()` y `is_relative_to` en las funciones de escaneo, garantizando que no se procesen rutas que hayan escapado del contexto de seguridad o que contengan manipulaciones de directorio (traversal).
- `2026-07-28T09:44:37` **quarantine.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `quarantine_file` validando que la ruta de origen, una vez normalizada, no se encuentre ya dentro del directorio de cuarentena, evitando así posibles bucles o intentos de autocuarentena malintencionada.
- `2026-07-28T09:44:11` **organizer.py** (seguridad defensiva): Se reforzó `stage_for_review` para prevenir colisiones de rutas y ataques de salto de directorio, asegurando mediante `relative_to` que el destino resuelto sea efectivamente un hijo de la carpeta de revisión y evitando que archivos de sistema sean movidos incluso si `is_safe_to_modify` pasara.
- `2026-07-28T09:43:49` **memory.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `trim_working_set` validando explícitamente el rango de PIDs contra IDs de sistema conocidos y añadiendo un filtrado preventivo antes de intentar abrir un handle de proceso, mitigando riesgos de manipulación accidental de procesos críticos o inválidos.
- `2026-07-28T09:35:05` **main.py** (seguridad defensiva): Se ha añadido un filtro de seguridad en `on_stage` y `on_quarantine_duplicates` para asegurar que las rutas candidatas sean validadas explícitamente mediante `safety.is_safe_to_modify` antes de proceder, previniendo operaciones sobre directorios críticos que podrían haber sido ignorados previamente.
- `2026-07-28T09:34:20` **healthscore.py** (seguridad defensiva): Se reforzó la robustez defensiva de `compute_score` validando explícitamente que los pesos en `WEIGHTS` sumen exactamente 100 y que los `ratios` procesados coincidan con las claves esperadas, evitando cálculos silenciosamente incorrectos si se modifica la configuración.
- `2026-07-28T09:33:56` **duplicates.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_collect_candidates` para prevenir el seguimiento de puntos de reparse (junctions o symlinks a directorios), evitando bucles infinitos o el escaneo accidental fuera del ámbito de las carpetas seleccionadas por el usuario.
- `2026-07-28T09:33:33` **diskreport.py** (seguridad defensiva): Se reforzó la seguridad en `walk_files` mediante la validación explícita de que la ruta raíz (`base`) sea un directorio absoluto y seguro antes de iniciar el escaneo, evitando seguimientos innecesarios de enlaces que podrían escapar a la estructura esperada.
- `2026-07-28T09:24:17` **browser.py** (seguridad defensiva): Se ha mejorado la robustez defensiva de `directory_size` y `detect_profiles` implementando `is_symlink()` de forma más estricta para evitar la recursión en enlaces simbólicos y puntos de reparse, asegurando que las rutas procesadas sean tratadas como archivos o carpetas reales antes de cualquier operación de I/O.
- `2026-07-28T09:24:09` **branding.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `save_logo_svg` al reemplazar el manejo genérico de excepciones por una validación explícita mediante `is_safe_to_modify` antes de cualquier operación de escritura, evitando además la creación de directorios innecesarios si la ruta ya es inválida.
- `2026-07-28T09:23:41` **assistant.py** (seguridad defensiva): Se reforzó la seguridad defensiva en `_call_gemini` limitando el tamaño del texto de respuesta y restringiendo estrictamente los caracteres de control para evitar inyecciones en el flujo de interfaz de la app.
- `2026-07-28T09:13:47` **settings.py** (robustez ante casos límite): Se ha mejorado la resiliencia de `load` y `save` ante situaciones de acceso concurrente al disco (como bloqueos de archivo o cambios de permisos súbitos) mediante la adición de un bloque de control más robusto y el manejo explícito de errores de E/S, asegurando que la app nunca quede en estado inconsistente.
- `2026-07-28T09:13:37` **scanner.py** (robustez ante casos límite): Se reforzó la robustez de `scan_directory` al manejar explícitamente posibles errores de acceso y metadatos inconsistentes al iterar sobre el sistema de archivos, asegurando que la recolección de sospechas continúe incluso si un archivo individual es bloqueado o eliminado durante el escaneo.
- `2026-07-28T09:04:29` **quarantine.py** (robustez ante casos límite): Mejoré la robustez de `quarantine_file` ante la posible falta de consistencia en el estado del disco, añadiendo una limpieza explícita del archivo temporal (si llegara a quedar huérfano) y verificando que el hash generado sea válido antes de confirmar el movimiento en el manifiesto.
