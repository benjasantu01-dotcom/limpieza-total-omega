# Limpieza Total Omega

Proyecto de demostración: una app de escritorio para Windows 11 que
organiza archivos basura y hace un escaneo heurístico de seguridad,
combinada con un bucle autónomo en la nube que mejora el código solo,
usando la API gratuita de Gemini (Google AI Studio).

## Estructura

```
app/          → la app de escritorio (la corrés vos, manualmente, en tu PC)
  organizer.py  → busca y organiza archivos basura (nunca borra sin confirmación)
  scanner.py    → escaneo heurístico + integración con Windows Defender real
  main.py       → interfaz gráfica (customtkinter)

evolve/       → el bucle autónomo (corre en GitHub Actions, en la nube)
  evolve.py     → llama a Gemini, propone un cambio, corre tests, commitea si pasa
  budget.py     → controla que no se pase del límite diario gratis
  tests/        → tests que un cambio debe pasar para ser aceptado

MISSION.md    → la "guía" que le decís al bucle en qué enfocarse esta semana
evolve_log.md → se genera solo, historial de qué aceptó/rechazó el bucle
```

## Diseño de seguridad (léelo antes de la demo)

1. **El bucle autónomo solo edita código dentro del repo.** Nunca ejecuta
   comandos de limpieza reales ni toca tu PC directamente.
2. **Ningún cambio se acepta sin pasar los tests** en `evolve/tests/`.
   Si Gemini propone algo que rompe el comportamiento, se descarta y
   se revierte solo — queda logueado en `evolve_log.md`.
3. **Presupuesto diario controlado** (`evolve/budget.py`): apunta a
   ~1000 peticiones/día con un tope duro de 1500, para no pasarse del
   límite gratuito de Google AI Studio.
4. **La app de escritorio nunca borra nada directamente.** Mueve
   candidatos a una carpeta `_Para_Revisar`; el borrado real es un
   botón aparte que el usuario aprieta a propósito.

## Cómo arrancar

### 1. La app de escritorio
```bash
cd app
pip install customtkinter
python main.py
```

### 2. El bucle autónomo
1. Crea un repo en GitHub y subí todo esto.
2. En Google AI Studio, generá una `GEMINI_API_KEY`.
3. En GitHub: Settings → Secrets and variables → Actions → agregá el
   secreto `GEMINI_API_KEY`.
4. El workflow `.github/workflows/evolve.yml` ya está configurado para
   correr cada 30 minutos automáticamente. También lo podés disparar
   a mano desde la pestaña Actions ("Run workflow").
5. Editá `MISSION.md` cuando quieras cambiar el foco de las mejoras.

### 3. Para la demo con tus compañeros
Después de dejarlo corriendo un tiempo, mostrá `evolve_log.md`: ahí
queda el historial completo de qué propuso Gemini, qué se aceptó y
qué se rechazó y por qué — es mucho más convincente que decir "corrió
solo", porque se ve el criterio real detrás de cada decisión.
