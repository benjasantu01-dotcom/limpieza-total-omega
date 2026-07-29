# Progreso del bucle autónomo

Este archivo se regenera solo en cada corrida a partir de
`evolve/metrics.jsonl`. No lo edites a mano.

## Resumen general

- Iteraciones totales: **504**
- Mejoras aceptadas: **258** (51.2% de aceptación)
- Rechazadas por tests: 17
- Rechazadas por guardia de seguridad: 25
- Sin cambios (nada sustancial que mejorar): 8
- Sin respuesta de la IA (error o límite): 196

## Por día

| Día | Aceptadas | Rechazadas (tests) | Rechazadas (guardia) | Sin cambios | Sin respuesta |
|---|---|---|---|---|---|
| 2026-07-28 | 109 | 8 | 10 | 2 | 87 |
| 2026-07-29 | 149 | 9 | 15 | 6 | 109 |

## Mejoras aceptadas por enfoque

- legibilidad y documentación: **66**
- manejo de errores y validación de entradas: **55**
- rendimiento: **47**
- robustez ante casos límite: **47**
- seguridad defensiva: **43**

## Mejoras aceptadas por archivo

- `browser.py`: **23**
- `assistant.py`: **22**
- `settings.py`: **22**
- `quarantine.py`: **21**
- `scanner.py`: **21**
- `main.py`: **20**
- `diskreport.py`: **19**
- `duplicates.py`: **19**
- `organizer.py`: **19**
- `healthscore.py`: **18**
- `memory.py`: **17**
- `safety.py`: **15**
- `branding.py`: **13**
- `startup.py`: **9**

## Últimas 15 mejoras aceptadas

- `2026-07-29T12:12:51` **safety.py** (robustez ante casos límite): Se ha mejorado la robustez ante casos límite en `is_within_directory` y `is_protected_path` al asegurar que las rutas no existentes o con permisos denegados no se evalúen erróneamente como "seguras" o "inseguras" de forma impredecible, centralizando la validación de existencia en un try-except más estricto.
- `2026-07-29T12:12:23` **quarantine.py** (robustez ante casos límite): Mejoré la robustez de `quarantine_file` para manejar el caso límite donde la ruta de origen contiene caracteres inválidos para el sistema de archivos de destino o nombres con longitudes que excedan los límites del sistema operativo antes de intentar el movimiento.
- `2026-07-29T12:11:58` **organizer.py** (robustez ante casos límite): Mejoré la robustez de `stage_for_review` implementando un chequeo preventivo de concurrencia y espacio en disco, evitando excepciones innecesarias y asegurando que las rutas de destino mantengan la integridad del sistema incluso ante estados de archivos bloqueados.
- `2026-07-29T12:02:12` **healthscore.py** (robustez ante casos límite): Se ha robustecido el cálculo de `score_security` para prevenir comportamientos inesperados ante valores extremos, asegurando que el ratio nunca sea negativo y manejando la posibilidad de que los parámetros de entrada sean extremadamente altos, manteniendo la estabilidad del cálculo global.
- `2026-07-29T11:52:33` **diskreport.py** (robustez ante casos límite): Se reforzó la robustez de `all_drives_usage` ante la presencia de unidades de red (UNC) o unidades mapeadas que fallan al resolverse, evitando que una sola ruta inaccesible interrumpa la detección global del sistema.
- `2026-07-29T11:52:23` **browser.py** (robustez ante casos límite): Mejoré la robustez de `directory_size` para manejar posibles errores al consultar `stat()` en archivos bloqueados durante el escaneo, evitando que el proceso se interrumpa ante errores de E/S inesperados.
- `2026-07-29T11:51:34` **assistant.py** (robustez ante casos límite): Mejora la robustez del motor de consulta remota incluyendo validaciones explícitas de estado de red y integridad de respuesta para evitar fallos por respuestas vacías, truncadas o con formato JSON inválido, asegurando que el asistente siempre tenga una salida segura ante errores de red o API.
- `2026-07-29T11:42:11` **startup.py** (rendimiento): Optimicé el rendimiento de `entries_from_registry` consolidando el parseo de CSV: en lugar de llamar a `parse_registry_csv` por cada línea (lo que generaba múltiples listas y recorridos innecesarios), ahora proceso el buffer de una sola vez, reduciendo la carga de CPU y la creación de objetos intermedios.
- `2026-07-29T11:42:02` **settings.py** (rendimiento): Se optimizó el rendimiento del proceso de carga mediante la implementación de una caché de validación (`_validated_cache`) que evita recalcular la estructura completa del diccionario de configuración cuando el archivo en disco no ha cambiado, reduciendo la carga de CPU y la redundancia lógica.
- `2026-07-29T11:41:36` **scanner.py** (rendimiento): Optronicé la función `scan_directory` reemplazando la creación repetitiva de objetos `Path` por el uso directo de `entry.path` (string) para el chequeo de seguridad y recursión, reduciendo drásticamente la sobrecarga de instanciación de objetos en directorios grandes.
- `2026-07-29T11:41:14` **safety.py** (rendimiento): Se implementó un cache LRU en `is_sensitive_file` y se optimizó `is_protected_path` evitando la regeneración constante de conjuntos en cada llamada, mejorando el rendimiento en recorridos de disco masivos.
- `2026-07-29T11:31:48` **quarantine.py** (rendimiento): Optimizé `list_items` y `summarize` para aprovechar la caché existente en lugar de recargar el manifiesto desde disco en cada llamado, reduciendo drásticamente las operaciones de I/O redundantes durante la navegación por la UI.
- `2026-07-29T11:31:22` **organizer.py** (rendimiento): Se optimizó el escaneo de directorios reemplazando el uso recursivo de `os.scandir` por una implementación que pre-filtra extensiones mediante el uso eficiente de `str.endswith` con tuplas, y se evitó la instanciación innecesaria de objetos `Path` dentro del bucle crítico, reduciendo la carga sobre el sistema de archivos durante el escaneo.
- `2026-07-29T11:30:59` **memory.py** (rendimiento): Optimizé `parse_windows_process_csv` reemplazando la creación y filtrado de listas intermedias por un generador eficiente, reduciendo el consumo de memoria al procesar la salida del comando de procesos.
- `2026-07-29T11:22:19` **main.py** (rendimiento): Se implementó un cacheo más eficiente en el método `on_full_analysis` utilizando el diccionario `_cache` existente para evitar re-escaneos redundantes de directorios de descarga y registros de arranque, reduciendo significativamente la latencia de la pestaña Salud al consolidar mediciones costosas.
