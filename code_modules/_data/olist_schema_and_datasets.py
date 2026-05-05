"""
olist_schema_and_datasets.py — Schema + public dataset loader for the Olist ecommerce data.

Provides:
  - load_schema_context            Parse data_model.json → (data_model, schema_text, tables_meta)
  - load_public_ecommerce_datasets Download CSVs from GitHub → dict of DataFrames
  - init_all                       One-call convenience: schema + datasets + validation
"""

import json
from pathlib import Path

import pandas as pd


# ── Public CSV URLs ──────────────────────────────────────────

OLIST_CSV_URLS: dict[str, str] = {
    "orders":      "https://raw.githubusercontent.com/olist/work-at-olist-data/master/datasets/olist_orders_dataset.csv",
    "products":    "https://raw.githubusercontent.com/olist/work-at-olist-data/master/datasets/olist_products_dataset.csv",
    "reviews":     "https://raw.githubusercontent.com/olist/work-at-olist-data/master/datasets/olist_order_reviews_dataset.csv",
    "customers":   "https://raw.githubusercontent.com/olist/work-at-olist-data/master/datasets/olist_customers_dataset.csv",
    "order_items": "https://raw.githubusercontent.com/olist/work-at-olist-data/master/datasets/olist_order_items_dataset.csv",
    "payments":    "https://raw.githubusercontent.com/olist/work-at-olist-data/master/datasets/olist_order_payments_dataset.csv",
    "sellers":     "https://raw.githubusercontent.com/olist/work-at-olist-data/master/datasets/olist_sellers_dataset.csv",
}


# ── Schema loader ────────────────────────────────────────────

def load_schema_context(schema_path) -> tuple[dict, str, dict]:
    """Load ``data_model.json`` and return ``(data_model, schema_text, tables_meta)``.

    *tables_meta* is a dict keyed by table_name with column lists, descriptions, etc.
    """
    schema_path = Path(schema_path)
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    with schema_path.open("r", encoding="utf-8") as f:
        data_model = json.load(f)

    # Columns may be objects {"column_name":…} or compact arrays [name, type, desc].
    def _col_name(col): return col[0] if isinstance(col, list) else col["column_name"]
    def _col_type(col): return col[1] if isinstance(col, list) else col.get("data_type")
    def _col_desc(col): return col[2] if isinstance(col, list) and len(col) > 2 else (col.get("description", "") if isinstance(col, dict) else "")

    tables_meta: dict[str, dict] = {}
    for tbl in data_model.get("tables", []):
        name = tbl["table_name"]
        tables_meta[name] = {
            "table_name":   name,
            "description":  tbl.get("description", ""),
            "columns": {
                _col_name(col): {
                    "data_type":   _col_type(col),
                    "description": _col_desc(col),
                }
                for col in tbl.get("columns", [])
            },
            "column_names": [_col_name(col) for col in tbl.get("columns", [])],
        }

    # Compact plain-text schema (no JSON punctuation overhead — ~70% fewer tokens)
    _lines = [
        f"DATA MODEL: {data_model.get('name', '')}",
        f"{data_model.get('description', '')}",
        "",
        "Column format per table: name (type) — description",
    ]
    for tbl in data_model.get("tables", []):
        _lines.append(f"\nTABLE {tbl['table_name']}: {tbl.get('description', '')}")
        for col in tbl.get("columns", []):
            _lines.append(f"  {_col_name(col)} ({_col_type(col) or 'object'}) — {_col_desc(col)}")
    schema_text = "\n".join(_lines)

    print(f"Schema loaded: {len(tables_meta)} tables, {len(schema_text):,} chars")

    return data_model, schema_text, tables_meta


# ── Dataset loader ───────────────────────────────────────────

def load_public_ecommerce_datasets(
    urls: dict[str, str] | None = None,
    verbose: bool = True,
) -> dict[str, pd.DataFrame]:
    """Download Olist CSVs and return ``{name: DataFrame}``.

    Pass *urls* to override the defaults (e.g. for a different source).
    """
    urls = urls or OLIST_CSV_URLS
    datasets: dict[str, pd.DataFrame] = {}

    for name, url in urls.items():
        df = pd.read_csv(url)
        datasets[name] = df
        if verbose:
            print(f"  {name:15s} {len(df):>7,} rows  cols={list(df.columns)}")

    return datasets


# ── Convenience init ─────────────────────────────────────────

def init_all(
    schema_path,
    validate_fn=None,
    duckdb_tables: dict[str, pd.DataFrame] | None = None,
) -> dict:
    """Load schema + datasets + optional validation.  Returns a dict with all artefacts.

    *validate_fn* should accept (data_model, tables_dict) and return a list of issues.
    """
    data_model, schema_text, tables_meta = load_schema_context(schema_path)

    print("\nDownloading datasets...")
    datasets = load_public_ecommerce_datasets()

    # Default DuckDB table mapping
    if duckdb_tables is None:
        duckdb_tables = {
            "orders":      datasets["orders"],
            "order_items": datasets["order_items"],
            "products":    datasets["products"],
            "customers":   datasets["customers"],
            "payments":    datasets["payments"],
            "reviews":     datasets["reviews"],
            "sellers":     datasets["sellers"],
        }

    issues = []
    if validate_fn is not None:
        print()
        issues = validate_fn(data_model, duckdb_tables)

    return {
        "data_model":    data_model,
        "SCHEMA":        schema_text,
        "tables_meta":   tables_meta,
        "datasets":      datasets,
        "duckdb_tables": duckdb_tables,
        "issues":        issues,
    }