"""
Validates a client-supplied ReportSpec against report_catalog.EXPLORES and
turns it into safe SQL. This is what makes the Custom Reports builder safe:
the client can only ever reference catalog KEYS (validated here) — every
actual SQL fragment traces back to a static string in report_catalog.py,
and every client-supplied VALUE goes through %s params, never string
interpolation.
"""
from datetime import datetime

from fastapi import HTTPException

from .report_catalog import EXPLORES
from .report_scope import scope_for

MAX_PREVIEW_ROWS = 500
HARD_MAX_PREVIEW_ROWS = 5000
EXCEL_ROW_CAP = 50000


def validate_spec(spec: dict, *, for_excel: bool = False) -> dict:
    entity = spec.get("entity")
    if entity not in EXPLORES:
        raise HTTPException(400, f"Unknown entity: {entity!r}")
    explore = EXPLORES[entity]

    raw_mode = bool(spec.get("raw_mode", False))
    dimensions = spec.get("dimensions") or []
    measures = spec.get("measures") or []
    filters = spec.get("filters") or []
    sort = spec.get("sort") or None

    if not raw_mode:
        for d in dimensions:
            if d not in explore["dimensions"]:
                raise HTTPException(400, f"Unknown dimension {d!r} for entity {entity!r}")
        if not measures:
            raise HTTPException(400, "At least one measure is required (or set raw_mode=true)")
        for m in measures:
            key = m.get("key") if isinstance(m, dict) else m
            if key not in explore["measures"]:
                raise HTTPException(400, f"Unknown measure {key!r} for entity {entity!r}")

    for f in filters:
        key, op = f.get("key"), f.get("op")
        if key not in explore["filters"]:
            raise HTTPException(400, f"Unknown filter {key!r} for entity {entity!r}")
        fdef = explore["filters"][key]
        if op not in fdef["ops"]:
            raise HTTPException(400, f"Filter {key!r} does not support op {op!r}")
        _validate_filter_value(fdef, op, f.get("value"))

    if sort:
        skey = sort.get("key")
        valid_keys = set(dimensions) | {m.get("key") if isinstance(m, dict) else m for m in measures}
        if not raw_mode and skey not in valid_keys:
            raise HTTPException(400, f"sort.key {skey!r} must be a selected dimension or measure")
        if sort.get("dir") not in ("asc", "desc"):
            raise HTTPException(400, "sort.dir must be 'asc' or 'desc'")

    cap = EXCEL_ROW_CAP if for_excel else HARD_MAX_PREVIEW_ROWS
    default_limit = EXCEL_ROW_CAP if for_excel else MAX_PREVIEW_ROWS
    try:
        limit = int(spec["limit"]) if spec.get("limit") else default_limit
    except (TypeError, ValueError):
        raise HTTPException(400, "limit must be an integer")
    limit = max(1, min(limit, cap))

    return {
        "entity": entity, "dimensions": dimensions, "measures": measures,
        "filters": filters, "sort": sort, "limit": limit, "raw_mode": raw_mode,
    }


def _validate_filter_value(fdef, op, value):
    ftype = fdef["type"]
    if op == "in":
        if not isinstance(value, list) or not value:
            raise HTTPException(400, "op 'in' requires a non-empty list value")
        for v in value:
            _validate_scalar(ftype, fdef, v)
    elif op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise HTTPException(400, "op 'between' requires a 2-element [from, to] value")
        for v in value:
            _validate_scalar(ftype, fdef, v)
    else:
        _validate_scalar(ftype, fdef, value)


def _validate_scalar(ftype, fdef, value):
    if ftype == "date":
        try:
            datetime.fromisoformat(str(value))
        except Exception:
            raise HTTPException(400, f"Invalid date value: {value!r}")
    elif ftype == "enum":
        options = fdef.get("options")
        if options and value not in options:
            raise HTTPException(400, f"Invalid value {value!r}, must be one of {options}")
    elif ftype == "number":
        try:
            float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"Invalid numeric value: {value!r}")
    elif ftype == "string":
        if not isinstance(value, str):
            raise HTTPException(400, f"Invalid string value: {value!r}")


def _filter_clause(col_sql, op, value):
    if op == "eq":
        return f"{col_sql} = %s", [value]
    if op == "gte":
        return f"{col_sql} >= %s", [value]
    if op == "lte":
        return f"{col_sql} <= %s", [value]
    if op == "in":
        placeholders = ", ".join(["%s"] * len(value))
        return f"{col_sql} IN ({placeholders})", list(value)
    if op == "between":
        return f"{col_sql} BETWEEN %s AND %s", list(value)
    raise HTTPException(400, f"Unsupported op: {op!r}")


def build_query(spec: dict, user: dict):
    """
    Returns (sql, params, columns_meta) — columns_meta is
    [{"key","label","type"}, ...] in SELECT order.
    """
    entity = spec["entity"]
    explore = EXPLORES[entity]
    role, uid = user["role"], user["sub"]

    extra_joins = []  # de-duplicated, first-seen order

    def _add_join(join_sql):
        if join_sql and join_sql not in extra_joins:
            extra_joins.append(join_sql)

    select_parts, group_by_parts, columns_meta = [], [], []

    if spec["raw_mode"]:
        for label, col_sql in explore["raw_columns"]:
            select_parts.append(f'{col_sql} AS "{label}"')
            columns_meta.append({"key": label, "label": label, "type": "string"})
    else:
        for dim_key in spec["dimensions"]:
            d = explore["dimensions"][dim_key]
            _add_join(d.get("extra_join"))
            select_parts.append(f'{d["sql"]} AS "{dim_key}"')
            group_by_parts.append(d["sql"])
            columns_meta.append({"key": dim_key, "label": d["label"], "type": d["type"]})
        for m in spec["measures"]:
            mkey = m.get("key") if isinstance(m, dict) else m
            mdef = explore["measures"][mkey]
            _add_join(mdef.get("extra_join"))
            select_parts.append(f'{mdef["sql"]} AS "{mkey}"')
            columns_meta.append({"key": mkey, "label": mdef["label"], "type": "number"})

    where_parts, params = [], []
    if explore.get("default_where"):
        where_parts.append(explore["default_where"])

    scope_join, scope_where, scope_jp, scope_wp = scope_for(role, uid)
    if scope_join:
        _add_join(scope_join)
        params.extend(scope_jp)
    if scope_where:
        clause = scope_where.strip()
        if clause.upper().startswith("AND "):
            clause = clause[4:]
        where_parts.append(clause)
        params.extend(scope_wp)

    for f in spec["filters"]:
        fdef = explore["filters"][f["key"]]
        _add_join(fdef.get("extra_join"))
        clause, fparams = _filter_clause(fdef["sql"], f["op"], f["value"])
        where_parts.append(clause)
        params.extend(fparams)

    sql = f"SELECT {', '.join(select_parts)} FROM {explore['from_sql']}"
    for j in extra_joins:
        sql += f" {j}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if group_by_parts:
        sql += " GROUP BY " + ", ".join(group_by_parts)

    if spec.get("sort"):
        direction = "DESC" if spec["sort"]["dir"] == "desc" else "ASC"
        sql += f' ORDER BY "{spec["sort"]["key"]}" {direction}'
    elif not spec["raw_mode"] and spec["measures"]:
        first_key = spec["measures"][0].get("key") if isinstance(spec["measures"][0], dict) else spec["measures"][0]
        sql += f' ORDER BY "{first_key}" DESC'

    sql += f" LIMIT {int(spec['limit'])}"

    return sql, params, columns_meta
