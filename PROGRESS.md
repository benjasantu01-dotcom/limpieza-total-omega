# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **230** (45.6% de aceptación)
- Rechazadas por tests: 13
- Rechazadas por guardia de seguridad: 33
- Sin cambios (nada sustancial que mejorar): 15
- Sin respuesta de la IA (error o límite): 213

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-08-28 | 106 | 7 | 15 | 6 | 114 |
| 2026-08-29 | 124 | 6 | 18 | 9 | 99 |

## Mejoras aceptadas por enfoque

- manejo de errores y validación de entradas: **52**
- legibilidad y documentación: **52**
- rendimiento: **45**
- robustez ante casos límite: **42**
- seguridad defensiva: **39**

## Mejoras aceptadas por archivo

- `assistant.py`: **21**
- `settings.py`: **21**
- `scanner.py`: **20**
- `duplicates.py`: **19**
- `memory.py`: **19**
- `diskreport.py`: **18**
- `quarantine.py`: **18**
- `branding.py`: **17**
- `browser.py`: **17**
- `healthscore.py`: **16**
- `main.py`: **12**
- `organizer.py`: **11**
- `safety.py`: **11**
- `startup.py`: **10**

## Últimas 15 mejoras aceptadas

- `2026-08-29T10:51:34` **settings.py** (robustez ante casos límite): Se ha añadido una validación robusta para prevenir la escritura en dispositivos de solo lectura (como unidades ópticas o sistemas de archivos bloqueados) y para manejar el caso límite donde `os.fsync` falla en sistemas de archivos que no soportan la operación, garantizando la integridad sin bloquear la ejecución.
- `2026-08-29T10:51:05` **scanner.py** (robustez ante casos límite): Se ha mejorado la robustez de `_is_reparse_point` incorporando un manejo defensivo contra `FileNotFoundError` (común al escanear procesos dinámicos o archivos temporales que desaparecen entre el `os.scandir` y el `stat`) y se corrigió la lógica de retorno para asegurar que, ante cualquier falla de acceso a atributos, el método asuma conservadoramente que la ruta es un punto de reparse para evitar la recursión infinita o errores de acceso.
- `2026-08-29T10:41:30` **quarantine.py** (robustez ante casos límite): Se introdujo una validación de "espacio disponible" más robusta en `quarantine_file` y una protección contra condiciones de carrera en el manifiesto al asegurar que la lectura y escritura se realicen sobre el estado más reciente después de posibles cambios en el filesystem.
- `2026-08-29T10:40:30` **memory.py** (robustez ante casos límite): Se mejoró la robustez de `parse_windows_process_csv` ante entradas malformadas o PIDs inexistentes (valores negativos/cero) que podrían causar errores inesperados al procesar la salida de PowerShell.
- `2026-08-29T10:32:04` **main.py** (robustez ante casos límite): Se reforzó la robustez del bucle principal (`_on_closing`) y la gestión de tareas asíncronas para prevenir condiciones de carrera durante el cierre de la aplicación, garantizando que el `ThreadPoolExecutor` no intente manipular widgets destruidos y que el estado de la UI sea consistente en situaciones de salida abrupta.
- `2026-08-29T10:31:06` **healthscore.py** (robustez ante casos límite): Reforcé la robustez de `compute_score` ante posibles cambios en la estructura de `_SCORERS` o errores de acceso en `ratios`, evitando fallos de ejecución si una clave no está presente y garantizando que las métricas sean siempre tratadas como finitas antes de procesar el cálculo.
- `2026-08-29T10:21:57` **browser.py** (robustez ante casos límite): Se reforzó la robustez de `directory_size` y `_sum_directory_recursive` ante archivos bloqueados o en uso (casos límite comunes al acceder a caché de navegadores abiertos) mediante la captura explícita de `OSError` con códigos de error específicos de Windows (32: en uso, 5: acceso denegado).
- `2026-08-29T10:20:52` **assistant.py** (robustez ante casos límite): Se reforzó la robustez del motor local al añadir validación de tipos y rangos en las funciones de manejo de métricas, evitando errores de ejecución ante entradas inesperadas (`NaN`, `inf`, o tipos erróneos) que podrían surgir tras análisis fallidos o corruptos.
- `2026-08-29T10:10:54` **settings.py** (rendimiento): Optimicé el rendimiento de `load()` y `_read_disk()` sustituyendo el cálculo repetitivo del `mtime` del archivo en cada llamada por un mecanismo de validación condicional que minimiza las consultas al sistema de archivos mediante `lru_cache`, evitando lecturas redundantes de disco.
- `2026-08-29T10:10:40` **scanner.py** (rendimiento): Optimizamos `Scanner.process_entry` reemplazando la creación de objetos `Path` pesados por operaciones directas sobre `entry.name` y `entry.path`, evitando llamadas innecesarias al sistema de archivos al pre-filtrar por extensiones antes de instanciar rutas o resolverlas.
- `2026-08-29T10:01:35` **quarantine.py** (rendimiento): Optimicé el rendimiento de `load_manifest` mediante el uso de `lru_cache` con un `maxsize` ajustado y la validación de existencia del archivo antes de intentar el parsing JSON, evitando operaciones de I/O redundantes y bloqueantes en llamadas frecuentes.
- `2026-08-29T10:00:53` **memory.py** (rendimiento): Se optimizó `top_memory_processes` reemplazando la lectura innecesaria de 20 procesos para filtrar solo 10, y se mejoró el rendimiento de `parse_windows_process_csv` utilizando una estructura de datos `list.append` eficiente con pre-filtrado de errores para evitar ciclos o lógica redundante.
- `2026-08-29T09:50:44` **healthscore.py** (rendimiento): Optimicé el cálculo del puntaje pre-calculando los resultados de las funciones de puntuación en un diccionario local, evitando múltiples recorridos y llamadas redundantes durante la generación de recomendaciones.
- `2026-08-29T09:50:32` **duplicates.py** (rendimiento): Se optimizó el proceso de recolección de archivos `_collect_candidates` evitando llamadas redundantes a `stat()` y `is_file()` mediante el uso de `os.scandir` (vía `path.iterdir()` en Python 3.5+) y almacenando el `st_size` junto a la ruta para evitar un `stat()` adicional al agrupar, reduciendo drásticamente las operaciones de E/S.
- `2026-08-29T09:49:38` **browser.py** (rendimiento): Implementé la persistencia del diccionario `memo` en `detect_profiles` para evitar el re-cálculo de tamaños de subcarpetas comunes (como las compartidas bajo "User Data") durante el escaneo de múltiples navegadores, optimizando significativamente el tiempo de ejecución en sistemas con muchos perfiles.
