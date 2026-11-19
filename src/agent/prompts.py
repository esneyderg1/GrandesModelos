"""
Todo el texto que lee el modelo (los prompts) está en este archivo.

El agente le habla al modelo en tres momentos:

  1. PROMPT DEL SISTEMA   → build_system_prompt()
     Va al inicio de cada conversación. Le dice quién es, qué tablas existen,
     qué puede y qué no puede hacer, y le muestra las consultas de referencia.

  2. INTERPRETATION_PROMPT
     Después de ejecutar la consulta, se le entregan los resultados para que
     los explique en español.

  3. RETRY_PROMPT
     Si la consulta falla, se le muestra el error de SQLite para que la corrija.

Para ver el prompt del sistema completo, tal como lo recibe el modelo:

    uv run python -m src.agent.prompts
"""

from .database import FORBIDDEN_KEYWORDS, get_schema
from .query_catalog import REFERENCE_QUERIES, VIEWS_DESCRIPTION

# ─────────────────────────────────────────────────────────────────────────────
# 1. PROMPT DEL SISTEMA — se arma con cuatro partes, en este orden
# ─────────────────────────────────────────────────────────────────────────────

# Parte 1 · Rol, esquema de la base ({schema} viene de database.SCHEMA) e instrucciones.
ROLE_AND_INSTRUCTIONS = """Eres un experto en análisis de datos de procesos RPA (Robotic Process Automation)
para una organización de salud en Colombia. Puedes consultar una base de datos SQLite
con el historial de ejecuciones de bots, tiempos manuales y ROI estimado.

{schema}

=== INSTRUCCIONES ===
- Si la pregunta requiere consultar datos, genera UNA sola query SQL válida para SQLite,
  envuelta exactamente en ```sql ... ```.
- Si la pregunta NO requiere SQL (saludos, conceptos generales), responde directamente.
- Nunca inventes datos; basa las respuestas SOLO en los resultados de la query.
- Responde SIEMPRE en español, de forma clara y concisa.
- Para ROI usa: Beneficio = TiempoManualHoras × ValorHoraProyecto × Num_Ejecuciones.
- Si el usuario pide un ranking, limita con LIMIT 10 salvo que indique otro número.
"""

# Parte 2 · Sentencias prohibidas. Aquí solo se le INFORMAN al modelo: quien las hace
# cumplir es database.execute_query (validación + conexión de solo lectura).
READ_ONLY_RULES = (
    "=== SENTENCIAS PERMITIDAS ===\n"
    "- Solo se ejecuta UNA sentencia de lectura: SELECT (o WITH ... SELECT).\n"
    f"- Están PROHIBIDAS y serán rechazadas: {', '.join(FORBIDDEN_KEYWORDS)}, REPLACE INTO.\n"
    "- Si el usuario pide modificar, crear o borrar datos, no generes SQL: responde\n"
    "  que solo puedes consultar información.\n"
)

# Parte 3 · Vistas calculadas (su descripción viene de query_catalog.VIEWS_DESCRIPTION).
VIEWS_SECTION = (
    "=== VISTAS CALCULADAS (consúltalas como una tabla más) ===\n"
    "{views_description}\n"
    "Para ROI, ahorro neto, beneficio o costo del robot consulta SIEMPRE ROI_por_bot; "
    "no recalcules la fórmula.\n"
)

# Parte 4 · Consultas de referencia (los ejemplos vienen de query_catalog.REFERENCE_QUERIES).
REFERENCE_QUERIES_INTRO = (
    "=== CONSULTAS DE REFERENCIA (validadas contra la base) ===\n"
    "Son ejemplos de pregunta y su consulta. Si uno responde la pregunta del usuario,\n"
    "reutilízalo cambiando solo filtros, orden o LIMIT. Si ninguno aplica, escribe una\n"
    "consulta nueva con las mismas tablas, columnas y uniones; no inventes columnas.\n"
)
REFERENCE_QUERY_FORMAT = "\nPregunta: {question}\n```sql\n{sql}\n```\n"


def build_system_prompt() -> str:
    """Une las cuatro partes del prompt del sistema."""
    examples = "".join(REFERENCE_QUERY_FORMAT.format(**query) for query in REFERENCE_QUERIES)
    return "\n".join([
        ROLE_AND_INSTRUCTIONS.format(schema=get_schema()),
        READ_ONLY_RULES,
        VIEWS_SECTION.format(views_description=VIEWS_DESCRIPTION),
        REFERENCE_QUERIES_INTRO + examples,
    ])


# ─────────────────────────────────────────────────────────────────────────────
# 2. PROMPT DE INTERPRETACIÓN — después de ejecutar la consulta
# ─────────────────────────────────────────────────────────────────────────────

INTERPRETATION_PROMPT = (
    "Los resultados de la consulta son:\n\n{results}\n\n"
    "Interpreta estos resultados de forma clara y concisa en español. "
    "No repitas el SQL. Resalta los puntos más relevantes."
)

# ─────────────────────────────────────────────────────────────────────────────
# 3. PROMPT DE REINTENTO — cuando la consulta falló en SQLite
# ─────────────────────────────────────────────────────────────────────────────

RETRY_PROMPT = (
    "La consulta falló en SQLite con este error: {error}\n\n"
    "Corrígela usando SOLO las tablas y columnas del esquema y las consultas "
    "de referencia. Responde únicamente con el bloque ```sql ... ``` corregido."
)


if __name__ == "__main__":
    print(build_system_prompt())
