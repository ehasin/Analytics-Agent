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


# ── Column allowlist ─────────────────────────────────────────

def build_column_allowlist(data_model: dict) -> "dict[str, frozenset[str]]":
    """Build {table_name: frozenset(lowercase_column_names)} from a data model dict.

    Use this when the data model gains 'restricted' column annotations —
    pass the result to guard_sql's column_allowlist parameter to enforce
    schema-level access controls before execution.

    For standard use, execute_queries builds the allowlist automatically from
    the registered DataFrames (equivalent, since validate_tables() ensures
    DataFrames and schema are in sync at startup).

    Args:
        data_model: parsed data_model.json dict (from olist_schema_and_datasets.init_all)
    """
    allowlist: dict[str, frozenset[str]] = {}
    for table_def in data_model.get("tables", []):
        tbl = table_def["table_name"]
        cols: list[str] = []
        for col in table_def.get("columns", []):
            col_name = col[0] if isinstance(col, list) else col.get("column_name", "")
            if col_name:
                cols.append(col_name.lower())
        allowlist[tbl] = frozenset(cols)
    return allowlist


def _validate_columns(
    stmt,
    allowlist: "dict[str, frozenset[str]]",
) -> None:
    """Check qualified column references in *stmt* against *allowlist*.

    Only validates explicitly table-qualified references (alias.col or table.col).
    Unqualified columns are skipped — they cannot be reliably resolved without
    full type analysis. CTE aliases and subquery-derived tables are also skipped.

    Raises ValueError if a qualified column is definitively not in the table's schema.
    """
    # Build alias → real table name map
    alias_map: dict[str, str] = {}
    for table_node in stmt.find_all(_sg_exp.Table):
        tbl_name = (table_node.name or "").lower()
        alias = (table_node.alias or tbl_name).lower()
        if tbl_name:
            alias_map[alias] = tbl_name
            alias_map[tbl_name] = tbl_name  # table name maps to itself

    # Collect CTE aliases — virtual tables not in the schema
    cte_aliases: set[str] = set()
    for cte in stmt.find_all(_sg_exp.CTE):
        if cte.alias:
            cte_aliases.add(cte.alias.lower())

    # Check each qualified column reference
    for col_node in stmt.find_all(_sg_exp.Column):
        if not col_node.table:
            continue  # unqualified — cannot safely resolve

        tbl_alias = col_node.table.lower()
        col_name = col_node.name.lower()

        if tbl_alias in cte_aliases:
            continue  # CTE column — skip

        resolved_table = alias_map.get(tbl_alias)
        if not resolved_table or resolved_table not in allowlist:
            continue  # subquery alias or unknown table — skip conservatively

        if col_name == "*":
            continue  # wildcard — always valid

        if col_name not in allowlist[resolved_table]:
            raise ValueError(
                f"Blocked: column '{tbl_alias}.{col_name}' does not exist in "
                f"table '{resolved_table}'. Check data_model.json for valid columns."
            )


# ── SQL guard ────────────────────────────────────────────────

# Statement types that are never permitted
_BLOCKED_TYPES = (
    "Insert", "Update", "Delete", "Drop", "Create", "AlterTable",
    "Truncate", "Command",  # covers ATTACH, COPY, LOAD, INSTALL in sqlglot
)

def guard_sql(
    sql: str,
    column_allowlist: "dict[str, frozenset[str]] | None" = None,
) -> None:
    """Raise ValueError if *sql* is not a safe SELECT / WITH…SELECT statement.

    Blocks:
      - Any statement type other than SELECT or WITH…SELECT
      - References to system. or information_schema tables (checked on raw SQL)
      - Qualified column references to columns not in *column_allowlist* (when provided)

    If sqlglot is not installed or fails to parse the SQL, a RuntimeError with
    the message 'sqlglot_parse_failed' is raised so the caller can attach a
    guard_warning without blocking execution.

    Args:
        sql:              SQL string to validate.
        column_allowlist: Optional {table: frozenset(columns)} index. When provided,
                          qualified column references are checked against the schema.
                          Built automatically by execute_queries from live DataFrames.

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

    # ── Column allowlist check ───────────────────────────────
    if column_allowlist and _SQLGLOT_AVAILABLE:
        for stmt in statements:
            if stmt is None:
                continue
            try:
                _validate_columns(stmt, column_allowlist)
            except ValueError:
                raise  # propagate column-not-found errors as guard blocks
            except Exception:
                pass   # AST traversal errors are silent — don't block execution


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
    # Build column allowlist from registered DataFrames.
    # Equivalent to building from data_model.json because validate_tables()
    # ensures DataFrames and schema are in sync at startup. When restricted
    # column annotations are added to data_model.json, use build_column_allowlist()
    # and pass the result explicitly via guard_sql's column_allowlist param instead.
    _col_allowlist: dict[str, frozenset[str]] = {
        name: frozenset(col.lower() for col in df.columns)
        for name, df in tables.items()
    }

    con = build_connection(tables)
    try:
        for q in queries:
            if q.get("result") is not None:
                continue

            sql = normalize_sql(q["code"])

            # ── SQL guard ────────────────────────────────────
            try:
                guard_sql(sql, column_allowlist=_col_allowlist)
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
