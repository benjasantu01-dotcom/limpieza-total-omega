# Misión actual del bucle autónomo

Editá este archivo para cambiar en qué se enfoca Gemini en cada corrida.
El bucle lo lee al principio de cada iteración.

## Objetivo de esta semana

Mejorar la robustez, legibilidad y manejo de errores del código en
`app/` (organizer.py, scanner.py, main.py), SIN cambiar la funcionalidad
observable ni agregar dependencias nuevas ni nada destructivo.

Prioridades, en orden:
1. Manejo de errores más prolijo (paths inexistentes, permisos denegados).
2. Comentarios y docstrings más claros.
3. Pequeñas mejoras de performance que no cambien el comportamiento.
