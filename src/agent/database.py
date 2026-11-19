"""
Acceso a la base de datos que consulta el agente SQL.

Este archivo responde tres preguntas:
  1. ¿Qué tablas conoce el modelo?             → SCHEMA
  2. ¿Qué SQL se permite ejecutar?             → validate_sql()   (candado 1)
  3. ¿Cómo se ejecuta sin riesgo para la base? → execute_query()  (candado 2)
"""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent.parent.parent / "data" / "database" / "Procesos_clean.db"


# ─────────────────────────────────────────────────────────────────────────────
# 1. ESQUEMA: la descripción de las tablas que se le entrega al modelo
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
Base de datos SQLite: Procesos_clean.db
Contiene datos de automatizaciones RPA de una organización de salud en Colombia.

=== TABLAS ===

1. RegistrosDPA_clean (618,875 filas)
   Registros históricos de ejecución de cada bot RPA.
   - Automatizacion     TEXT    Nombre del bot/proceso automatizado
   - Duracion_Ejecucion TEXT    Tiempo que tardó el robot: formato "HH:MM:SS" (90.76% NULL)
   - Numero_Transacciones INTEGER Cantidad de ítems procesados
   - Celula             TEXT    Célula organizacional (ej: "Servicios Organizacionales")
   - Fecha_ejecucion    TEXT    Fecha ISO "YYYY-MM-DD"
   - Hora_Ejecucion     TEXT    Hora "HH:MM:SS AM/PM"
   - Exitosas           REAL    Transacciones completadas exitosamente
   - ErroresNegocio     REAL    Errores de lógica de negocio
   - Area               TEXT    Área organizacional (ej: "Afiliacion")
   - TipoEjecucion      TEXT    Tipo de ejecución
   - IdProceso          INTEGER Identificador del proceso

2. TiemposManuales_clean (95 filas)
   Tiempo que le tomaría a un humano ejecutar cada tarea manualmente.
   - NombreBot              TEXT    Nombre del bot
   - TiempoManualHoras      REAL    Horas que tarda un humano en hacer la misma tarea
   - Tipo                   TEXT    Tipo de proceso
   - Tecnologia             TEXT    Plataforma RPA (UiPath, IRPA, Power Automate)
   - FechaSalidaProduccion  TEXT    Fecha en que el bot salió a producción
   - Estado                 TEXT    Estado actual: "Activo" o "Inactivo"
   - Desarrollador          TEXT    Nombre del desarrollador

3. RolesAreas_clean (91 filas)
   Roles humanos y áreas impactadas por cada bot, con valor económico por hora.
   - Proyecto           TEXT    Nombre del proyecto
   - NombreBot          TEXT    Nombre del bot
   - AreaImpactada      TEXT    Área de negocio afectada
   - Rol_Impactado      TEXT    Perfil del empleado que realizaba la tarea
   - Tipo_Impacto       TEXT    "Ahorrado" (tiempo liberado) o "Salvado" (errores evitados)
   - ValorHoraProyecto  REAL    Costo del rol en COP por hora

=== NOTAS SQL ===
- Para calcular duración en horas desde "HH:MM:SS":
    (CAST(substr(Duracion_Ejecucion,1,2) AS REAL)*3600
    + CAST(substr(Duracion_Ejecucion,4,2) AS REAL)*60
    + CAST(substr(Duracion_Ejecucion,7,2) AS REAL)) / 3600.0
- Filtrar duraciones nulas con: Duracion_Ejecucion IS NOT NULL AND Duracion_Ejecucion != '0'
- ROI estimado por bot = TiempoManualHoras * ValorHoraProyecto * Num_Ejecuciones
"""


def get_schema() -> str:
    return SCHEMA


# ─────────────────────────────────────────────────────────────────────────────
# 2. CANDADO 1: revisar el texto de la consulta antes de ejecutarla
# ─────────────────────────────────────────────────────────────────────────────

# Sentencias que el agente nunca puede ejecutar. `REPLACE INTO` se revisa aparte
# porque replace() es una función de texto legítima dentro de un SELECT.
FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX",
)
_FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b|\bREPLACE\s+INTO\b", re.IGNORECASE
)
# Comentarios (-- y /* */) y textos entre comillas simples.
_COMMENTS_AND_STRINGS_RE = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", re.DOTALL)


class ForbiddenSQLError(ValueError):
    """La consulta no es una única sentencia de solo lectura."""


def validate_sql(sql: str) -> str:
    """
    Deja pasar la consulta solo si cumple las tres reglas; si no, lanza ForbiddenSQLError:
      1. Es UNA sola sentencia (sin ';' en la mitad).
      2. Empieza por SELECT o WITH.
      3. No contiene ninguna palabra de FORBIDDEN_KEYWORDS.

    Retorna la consulta sin el punto y coma final.
    """
    sql = sql.strip().rstrip(";").strip()
    # Las reglas se revisan sobre el código sin comentarios ni textos entre comillas:
    # así  WHERE Estado = 'Delete'  no se bloquea por error, y un comentario no puede
    # esconder una sentencia.
    code_only = _COMMENTS_AND_STRINGS_RE.sub(" ", sql).strip()

    if not code_only:
        raise ForbiddenSQLError("La consulta está vacía.")
    if ";" in code_only:
        raise ForbiddenSQLError("Solo se permite una sentencia por consulta.")
    if not re.match(r"(SELECT|WITH)\b", code_only, re.IGNORECASE):
        raise ForbiddenSQLError("Solo se permiten consultas SELECT.")
    forbidden = _FORBIDDEN_RE.search(code_only)
    if forbidden:
        raise ForbiddenSQLError(f"Sentencia no permitida: {forbidden.group(0).upper()}.")
    return sql


# ─────────────────────────────────────────────────────────────────────────────
# 3. CANDADO 2: ejecutar con una conexión de solo lectura
# ─────────────────────────────────────────────────────────────────────────────

def execute_query(sql: str, views: dict[str, str] | None = None) -> pd.DataFrame:
    """
    Ejecuta una consulta escrita por el modelo y retorna el resultado como tabla.

    Dos candados independientes protegen la base:
      1. validate_sql() solo deja pasar un SELECT.
      2. La conexión se abre en modo solo lectura (mode=ro): aunque algo se
         escapara del candado 1, SQLite se niega a escribir.

    `views` son consultas con nombre ({nombre: SELECT}) que el modelo puede usar
    como si fueran tablas. Se crean como vistas temporales: existen solo mientras
    dura esta conexión y no modifican el archivo de la base.
    """
    sql = validate_sql(sql)
    read_only_uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(read_only_uri, uri=True)) as conn:
        for name, select_sql in (views or {}).items():
            conn.execute(f"CREATE TEMP VIEW {name} AS {select_sql}")
        return pd.read_sql_query(sql, conn)


# ─────────────────────────────────────────────────────────────────────────────
# Estadísticas para la barra lateral de la app (no pasan por el modelo)
# ─────────────────────────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def get_db_stats() -> dict:
    """Retorna estadísticas básicas de la BD para mostrar en el sidebar."""
    with get_connection() as conn:
        stats = {}
        for table in ["RegistrosDPA_clean", "TiemposManuales_clean", "RolesAreas_clean"]:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            stats[table] = count

        bots_activos = conn.execute(
            "SELECT COUNT(*) FROM TiemposManuales_clean WHERE Estado = 'Activo'"
        ).fetchone()[0]
        stats["bots_activos"] = bots_activos

        fecha_min, fecha_max = conn.execute(
            "SELECT MIN(Fecha_ejecucion), MAX(Fecha_ejecucion) FROM RegistrosDPA_clean"
        ).fetchone()
        stats["fecha_inicio"] = fecha_min
        stats["fecha_fin"] = fecha_max

    return stats
