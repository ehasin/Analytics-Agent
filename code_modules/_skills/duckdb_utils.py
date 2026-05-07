"""
duckdb_utils.py — DuckDB helpers for the Analytics Agent.

Requires sqlglot for SQL statement validation (pip install sqlglot).

Provides:
  - normalize_sql        Clean raw LLM-generated SQL
  - guard_sql            Raise ValueError if SQL is not a safe SELECT statement
  - build_connection     Create a DuckDB connection with optional DataFrame registration
  - run_query            Execute SQL → DataFrame
  - execute_queries      Batch-execute a list of query dicts (agent pipeline)
  - validate_tables      Check registered tables/columns against a schema definition
"""

import duckdb
import pandas as pd

try:
    import sqlglot
    import sqlglot.expressions as _sg_exp
    _SQLGLOT_AVAILABLE = True
except ImportError:
    _SQLGLOT_AVAILABLE = False


# ── SQL cleanup ──────────────────────────────────────────────

def normalize_sql(sql: str) -> str:
    """Strip markdown fences, trailing semicolons, and whitespace."""
    return sql.replace("```sql", "").replace("```", "").strip().rstrip(";").strip()


# ── SQL guard ────────────────────────────────────────────────

# Statement types that are never permitted
_BLOCKED_TYPES = (
    "Insert", "Update", "Delete", "Drop", "Create", "AlterTable",
    "Truncate", "Command",  # covers ATTACH, COPY, LOAD, INSTALL in sqlglot
)

def guard_sql(sql: str) -> None:
    """Raise ValueError if *sql* is not a safe SELECT / WITH…SELECT statement.

    Blocks:
      - Any statement type other than SELECT or WITH…SELECT
      - References to system. or information_schema tables (checked on raw SQL)

    If sqlglot is not installed or fails to parse the SQL, a RuntimeError with
    the message 'sqlglot_parse_failed' is raised so the caller can attach a
    guard_warning without blocking execution.

    Requires: sqlglot (pip install sqlglot)
    """
    if not _SQLGLOT_AVAILABLE:
        raise RuntimeError("sqlglot_parse_failed")

    try:
        statements = sqlglot.parse(sql)
    except Exception:
        raise RuntimeError("sqlglot_parse_failed")

    if not statements or all(s is None for s in statements):
        raise RuntimeError("sqlglot_parse_failed")

    for stmt in statements:
        if stmt is None:
            continue
        stmt_type = type(stmt).__name__
        if not isinstance(stmt, (_sg_exp.Select, _sg_exp.With)):
            raise ValueError(
                f"Blocked SQL statement type '{stmt_type}': "
                "only SELECT / WITH…SELECT are permitted."
            )

    sql_lower = sql.lower()
    if "system." in sql_lower or "information_schema" in sql_lower:
        raise ValueError(
            "Blocked: SQL references system or information_schema tables."
        )


# ── Connection management ────────────────────────────────────

def build_connection(
    tables: dict[str, pd.DataFrame] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Return an in-memory DuckDB connection with *tables* registered as views."""
    con = duckdb.connect()
    if tables:
        for name, df in tables.items():
            con.register(name, df)
    return con


# ── Query execution ──────────────────────────────────────────

def run_query(
    sql: str,
    tables: dict[str, pd.DataFrame] | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> pd.DataFrame:
    """Execute *sql* and return a DataFrame.

    If *con* is provided it is reused (and left open).
    Otherwise a throwaway connection is created from *tables*.
    """
    own = con is None
    if own:
        con = build_connection(tables)
    try:
        return con.execute(normalize_sql(sql)).df()
    finally:
        if own:
            con.close()


def execute_queries(
    queries: list[dict],
    tables: dict[str, pd.DataFrame],
    stringify_results: bool = True,
) -> list[dict]:
    """Run every query dict in *queries* (agent pipeline format).

    Each dict must have a ``"code"`` key. After execution it gains either
    ``"result"`` (DataFrame or string) or ``"error"`` (str).
    A ``"guard_warning"`` key is added when sqlglot cannot parse the SQL
    (execution proceeds in that case — the guard is skipped, not the query).
    A single shared connection is used for the whole batch.
    """
    con = build_connection(tables)
    try:
        for q in queries:
            if q.get("result") is not None:
                continue

            sql = normalize_sql(q["code"])

            # ── SQL guard ────────────────────────────────────
            try:
                guard_sql(sql)
            except ValueError as e:
                # Blocked statement — do not execute
                q["error"] = str(e)
                q.pop("result", None)
                continue
            except RuntimeError:
                # sqlglot could not parse — warn but allow execution
                q["guard_warning"] = "sqlglot parse failed"

            # ── Execute ──────────────────────────────────────
            try:
                df = con.execute(sql).df()
                q["result"] = df.to_string(index=False) if stringify_results else df
                q.pop("error", None)
            except Exception as e:
                q["error"] = str(e)
                q.pop("result", None)

        return queries
    finally:
        con.close()


# ── Schema validation (DuckDB-native) ───────────────────────

def validate_tables(
    data_model: dict,
    tables: dict[str, pd.DataFrame],
) -> list[str]:
    """Compare a JSON schema definition against the actual DataFrames.

    Returns a list of human-readable issue strings (empty = all ok).
    No database queries needed — we inspect the DataFrames directly.
    Uses logging instead of print() to stay compatible with Streamlit Cloud
    and other non-interactive environments.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    issues: list[str] = []

    for table_def in data_model.get("tables", []):
        tbl = table_def["table_name"]

        if tbl not in tables:
            issues.append(f"[{tbl}] Listed in schema but not registered as a table")
            continue

        df_cols = set(tables[tbl].columns)
        schema_cols = {(col[0] if isinstance(col, list) else col["column_name"]) for col in table_def.get("columns", [])}

        for col in sorted(schema_cols - df_cols):
            issues.append(f"[{tbl}] Column '{col}' in schema but not in DataFrame")
        for col in sorted(df_cols - schema_cols):
            issues.append(f"[{tbl}] Column '{col}' in DataFrame but not in schema (undocumented)")

    for name in sorted(set(tables) - {t["table_name"] for t in data_model.get("tables", [])}):
        issues.append(f"[{name}] Registered table has no schema definition")

    if not issues:
        _log.info("Schema validation: all OK")
    else:
        _log.warning("Schema validation: %d issue(s): %s", len(issues), "; ".join(issues))

    return issues
