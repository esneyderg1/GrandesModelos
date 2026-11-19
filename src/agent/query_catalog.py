"""
Base de consultas del agente SQL (la "guía" que usa el modelo).

En lugar de dejar que el modelo construya cada consulta desde cero, se le entrega
esta base de consultas ya probadas contra `Procesos_clean.db`. Este archivo solo
contiene datos, en dos partes:

  1. VIEWS — vistas calculadas.
     Lógica de negocio compleja (el ROI) ya resuelta. El modelo la consulta como
     si fuera una tabla más, así no tiene que reescribir la fórmula.

  2. REFERENCE_QUERIES — consultas de referencia.
     Ejemplos de pregunta y su consulta correcta. El modelo reutiliza el ejemplo
     que responde la pregunta, o lo imita para escribir una consulta nueva.

Para agregar una consulta: añádela a REFERENCE_QUERIES y valida toda la base con

    uv run python -m src.agent.query_catalog
"""

from ..utils.roi_calculator import COSTO_HORA_ROBOT_COP
from .database import execute_query

# ─────────────────────────────────────────────────────────────────────────────
# 1. VISTAS CALCULADAS  {nombre: consulta}
# ─────────────────────────────────────────────────────────────────────────────
# ROI_por_bot aplica la misma fórmula que src/utils/roi_calculator.py, por eso el
# chat y la pestaña "Análisis ROI" entregan exactamente las mismas cifras.

VIEWS = {
    "ROI_por_bot": f"""WITH ejec AS (
  SELECT Automatizacion, COUNT(*) AS Num_Ejecuciones,
         AVG(CASE WHEN Duracion_Ejecucion IS NOT NULL AND Duracion_Ejecucion != '0'
                  THEN (CAST(substr(Duracion_Ejecucion,1,2) AS REAL)*3600
                      + CAST(substr(Duracion_Ejecucion,4,2) AS REAL)*60
                      + CAST(substr(Duracion_Ejecucion,7,2) AS REAL)) / 3600.0 END) AS DuracionHoras
  FROM RegistrosDPA_clean
  GROUP BY Automatizacion
), valor AS (
  SELECT NombreBot, AVG(ValorHoraProyecto) AS ValorHora
  FROM RolesAreas_clean
  GROUP BY NombreBot
), base AS (
  SELECT e.Automatizacion, e.Num_Ejecuciones,
         t.TiempoManualHoras * v.ValorHora * e.Num_Ejecuciones AS Beneficio,
         COALESCE(e.DuracionHoras, t.TiempoManualHoras * 0.1)
           * {COSTO_HORA_ROBOT_COP} * e.Num_Ejecuciones AS Costo
  FROM ejec e
  JOIN TiemposManuales_clean t ON t.NombreBot = e.Automatizacion
  JOIN valor v ON v.NombreBot = e.Automatizacion
)
SELECT Automatizacion,
       Num_Ejecuciones,
       ROUND(Beneficio) AS Beneficio_Bruto_COP,
       ROUND(Costo) AS Costo_Robot_COP,
       ROUND(Beneficio - Costo) AS Ahorro_Neto_COP,
       ROUND((Beneficio - Costo) / Costo * 100, 1) AS ROI_Porcentaje
FROM base
WHERE Costo > 0""",
}

# Lo que se le cuenta al modelo sobre las vistas (qué son y qué columnas tienen).
VIEWS_DESCRIPTION = (
    "ROI_por_bot: Una fila por bot con el ROI ya calculado con la fórmula oficial "
    f"(costo del robot = {COSTO_HORA_ROBOT_COP} COP/hora). Solo incluye bots con "
    "tiempo manual y valor hora registrados.\n"
    "   - Automatizacion TEXT, Num_Ejecuciones INTEGER, Beneficio_Bruto_COP REAL, "
    "Costo_Robot_COP REAL, Ahorro_Neto_COP REAL, ROI_Porcentaje REAL"
)

# ─────────────────────────────────────────────────────────────────────────────
# 2. CONSULTAS DE REFERENCIA  [{pregunta, consulta}]
# ─────────────────────────────────────────────────────────────────────────────

REFERENCE_QUERIES = [
    {
        "question": "¿Cuáles son los 5 bots con más ejecuciones?",
        "sql": """SELECT Automatizacion, COUNT(*) AS Num_Ejecuciones
FROM RegistrosDPA_clean
GROUP BY Automatizacion
ORDER BY Num_Ejecuciones DESC
LIMIT 5""",
    },
    {
        "question": "¿Cuántas ejecuciones hubo por año?",
        "sql": """SELECT substr(Fecha_ejecucion, 1, 4) AS Anio, COUNT(*) AS Num_Ejecuciones
FROM RegistrosDPA_clean
GROUP BY Anio
ORDER BY Anio""",
    },
    {
        "question": "¿Qué área tiene mayor tasa de error?",
        "sql": """SELECT Area,
       ROUND(SUM(ErroresNegocio) * 1.0 / NULLIF(SUM(Numero_Transacciones), 0), 4) AS Tasa_Error
FROM RegistrosDPA_clean
GROUP BY Area
ORDER BY Tasa_Error DESC
LIMIT 10""",
    },
    {
        "question": "¿Cuál es el desarrollador con más soluciones?",
        "sql": """SELECT Desarrollador, COUNT(DISTINCT NombreBot) AS Num_Soluciones
FROM TiemposManuales_clean
GROUP BY Desarrollador
ORDER BY Num_Soluciones DESC
LIMIT 10""",
    },
    {
        "question": "¿Cuántos bots hay por tecnología y estado?",
        "sql": """SELECT Tecnologia, Estado, COUNT(DISTINCT NombreBot) AS Num_Bots
FROM TiemposManuales_clean
GROUP BY Tecnologia, Estado
ORDER BY Num_Bots DESC""",
    },
    {
        "question": "¿Cuántas ejecuciones tuvo cada tecnología?",
        "sql": """SELECT t.Tecnologia, COUNT(*) AS Num_Ejecuciones
FROM RegistrosDPA_clean r
JOIN (SELECT DISTINCT NombreBot, Tecnologia FROM TiemposManuales_clean) t
  ON t.NombreBot = r.Automatizacion
GROUP BY t.Tecnologia
ORDER BY Num_Ejecuciones DESC""",
    },
    {
        "question": "¿Cuánto tiempo manual ahorró el bot GestorRemitidos en total?",
        "sql": """SELECT r.Automatizacion,
       COUNT(*) AS Num_Ejecuciones,
       ROUND(COUNT(*) * MAX(t.TiempoManualHoras), 1) AS Horas_Manuales_Ahorradas
FROM RegistrosDPA_clean r
JOIN TiemposManuales_clean t ON t.NombreBot = r.Automatizacion
WHERE r.Automatizacion = 'GestorRemitidos'
GROUP BY r.Automatizacion""",
    },
    {
        "question": "¿Cuáles son los 5 bots con mayor ROI?",
        "sql": """SELECT Automatizacion, ROI_Porcentaje, Ahorro_Neto_COP
FROM ROI_por_bot
ORDER BY ROI_Porcentaje DESC
LIMIT 5""",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Validación:  uv run python -m src.agent.query_catalog
# ─────────────────────────────────────────────────────────────────────────────

def validate_catalog() -> int:
    """Ejecuta cada vista y cada consulta igual que lo hace el agente. Retorna nº de fallos."""
    checks = [(f"vista {name}", f"SELECT * FROM {name}") for name in VIEWS]
    checks += [(query["question"], query["sql"]) for query in REFERENCE_QUERIES]

    failures = 0
    for label, sql in checks:
        try:
            rows = len(execute_query(sql, VIEWS))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FALLA  {label}\n       {exc}")
            continue
        failures += 0 if rows else 1
        print(f"{'OK    ' if rows else 'VACÍA '} {label}  ({rows} filas)")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if validate_catalog() else 0)
