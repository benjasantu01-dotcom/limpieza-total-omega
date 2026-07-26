# Misión actual del bucle autónomo

Editá este archivo para cambiar en qué se enfoca Gemini en cada corrida.
El bucle lo lee al principio de cada iteración.

## Objetivo de esta semana

Mejorar `app/` (organizer.py, scanner.py, main.py) de forma continua,
rotando entre 6 enfoques distintos (ver evolve/evolve.py):
manejo de errores, legibilidad, rendimiento, casos límite, seguridad
defensiva, y funcionalidad incremental (donde sí se permite sumar
funciones nuevas, siempre de forma aditiva).

Prioridades funcionales pendientes (para la categoría de funcionalidad
incremental, que también puede investigar en la web cómo lo resuelven
limpiadores/antivirus reales):
1. Selección de disco/carpeta a escanear más flexible (ya existe una
   base en organizer.py: `list_available_drives()` y el parámetro
   `directories` de `scan_for_junk()` — se puede seguir puliendo).
2. Estadísticas resumidas del escaneo (espacio total recuperable, por tipo de archivo).
3. Exclusión de carpetas específicas elegidas por el usuario.

Reglas que nunca cambian, sin importar el enfoque:
- Nada se borra automáticamente. Toda acción destructiva requiere
  confirmación manual del usuario en la app.
- Sin dependencias nuevas.
- Todo cambio debe pasar los tests antes de aceptarse.
