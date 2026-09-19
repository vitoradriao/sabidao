"""
db.py - Acesso direto ao PostgreSQL via psycopg com pool opcional.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import config

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - coberto indiretamente pelo fallback de runtime
    psycopg = None
    dict_row = None
    Jsonb = None

try:
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - fallback para conexoes diretas
    ConnectionPool = None

logger = logging.getLogger(__name__)

_pool = None

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ARRAY_PARAM_NAMES = {
    "filter_doc_types",
    "filter_modules",
    "filter_section_ids",
    "feedback_item_ids",
    "base_sources",
    "p_feedback_item_ids",
    "p_base_sources",
    "p_section_ids",
}


def _quote_identifier(identifier: str) -> str:
    if not _IDENTIFIER_RE.match(identifier or ""):
        raise ValueError(f"Identificador SQL invalido: {identifier!r}")
    return f'"{identifier}"'


def _quote_qualified_name(name: str) -> str:
    parts = [part.strip() for part in str(name or "").split(".") if part.strip()]
    if not parts:
        raise ValueError("Nome SQL vazio.")
    return ".".join(_quote_identifier(part) for part in parts)


def _normalize_json_value(field_name: str, value: Any) -> Any:
    if Jsonb is None:
        return value
    if isinstance(value, dict):
        return Jsonb(_json_safe(value))
    if isinstance(value, list) and field_name not in _ARRAY_PARAM_NAMES:
        return Jsonb(_json_safe(value))
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=str)]
    return value


def _placeholder(field_name: str) -> str:
    if field_name in {"feedback_item_ids", "p_feedback_item_ids", "filter_section_ids", "p_section_ids"}:
        return "%s::uuid[]"
    if "embedding" in field_name.lower():
        return "%s::vector"
    return "%s"


def _prepare_value(field_name: str, value: Any) -> Any:
    return _normalize_json_value(field_name, value)


def get_database_url() -> str | None:
    return os.getenv("DATABASE_URL")


def validate_database_config() -> None:
    if not get_database_url():
        raise EnvironmentError(
            "Variavel de ambiente obrigatoria nao definida: DATABASE_URL. Verifique seu arquivo .env"
        )


def _connection_options() -> str:
    options: list[str] = []
    timeouts = (
        ("statement_timeout", config.DB_STATEMENT_TIMEOUT_MS),
        ("lock_timeout", config.DB_LOCK_TIMEOUT_MS),
        (
            "idle_in_transaction_session_timeout",
            config.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS,
        ),
    )
    for name, raw_value in timeouts:
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            options.append(f"-c {name}={value}")
    return " ".join(options)


def _connect_kwargs(*, autocommit: bool) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"autocommit": autocommit}
    if dict_row is not None:
        kwargs["row_factory"] = dict_row
    if config.DB_APPLICATION_NAME:
        kwargs["application_name"] = config.DB_APPLICATION_NAME
    options = _connection_options()
    if options:
        kwargs["options"] = options
    return kwargs


def _create_direct_connection(*, autocommit: bool):
    if psycopg is None:
        raise RuntimeError(
            "psycopg nao esta instalado. Instale as dependencias do projeto antes de usar o banco."
        )

    database_url = get_database_url()
    if not database_url:
        raise RuntimeError("DATABASE_URL nao configurada.")

    return psycopg.connect(database_url, **_connect_kwargs(autocommit=autocommit))


def _get_pool():
    global _pool

    if ConnectionPool is None:
        return None
    if psycopg is None:
        raise RuntimeError(
            "psycopg nao esta instalado. Instale as dependencias do projeto antes de usar o banco."
        )

    database_url = get_database_url()
    if not database_url:
        raise RuntimeError("DATABASE_URL nao configurada.")

    if _pool is None:
        min_size = max(1, int(config.DB_POOL_MIN_SIZE))
        max_size = max(min_size, int(config.DB_POOL_MAX_SIZE))
        _pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            timeout=float(config.DB_POOL_TIMEOUT_SECONDS),
            kwargs=_connect_kwargs(autocommit=True),
            open=True,
        )
    return _pool


def close_connection() -> None:
    global _pool
    if _pool is None:
        return
    try:
        _pool.close()
    except Exception:
        logger.debug("Erro ao fechar pool do PostgreSQL.", exc_info=True)
    finally:
        _pool = None


atexit.register(close_connection)


@contextmanager
def _acquire_connection(connection=None, *, autocommit: bool = True):
    if connection is not None:
        yield connection
        return

    pool = _get_pool()
    if pool is not None:
        with pool.connection() as pooled_connection:
            yield pooled_connection
        return

    direct_connection = _create_direct_connection(autocommit=autocommit)
    try:
        yield direct_connection
    finally:
        try:
            direct_connection.close()
        except Exception:
            logger.debug("Erro ao fechar conexao direta com PostgreSQL.", exc_info=True)


@contextmanager
def db_transaction():
    with _acquire_connection(autocommit=False) as conn:
        with conn.transaction():
            yield conn


def db_advisory_xact_lock(key: str, *, connection) -> None:
    """Serializa uma operacao pela chave durante a transacao atual."""
    if connection is None:
        raise ValueError("db_advisory_xact_lock exige uma conexao transacional.")
    _execute_fetch(
        connection,
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0)) AS locked",
        [key],
    )


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def _parse_filter(column: str, raw_value: Any) -> tuple[str, list[Any]]:
    quoted_column = _quote_identifier(column)
    if not isinstance(raw_value, str):
        return f"{quoted_column} = %s", [_prepare_value(column, raw_value)]

    operators = (
        ("eq.", "="),
        ("neq.", "!="),
        ("gt.", ">"),
        ("gte.", ">="),
        ("lt.", "<"),
        ("lte.", "<="),
        ("like.", "LIKE"),
        ("ilike.", "ILIKE"),
    )
    for prefix, operator in operators:
        if raw_value.startswith(prefix):
            return f"{quoted_column} {operator} %s", [_parse_scalar(raw_value[len(prefix):])]

    if raw_value.startswith("is."):
        scalar = _parse_scalar(raw_value[3:])
        if scalar is None:
            return f"{quoted_column} IS NULL", []
        if scalar is True:
            return f"{quoted_column} IS TRUE", []
        if scalar is False:
            return f"{quoted_column} IS FALSE", []
        return f"{quoted_column} = %s", [scalar]

    if raw_value.startswith("in.(") and raw_value.endswith(")"):
        items = [item.strip() for item in raw_value[4:-1].split(",") if item.strip()]
        if not items:
            raise ValueError(f"Filtro IN vazio para {column}.")
        placeholders = ", ".join(["%s"] * len(items))
        return f"{quoted_column} IN ({placeholders})", [_parse_scalar(item) for item in items]

    return f"{quoted_column} = %s", [_parse_scalar(raw_value)]


def _parse_columns(columns: str) -> str:
    if not columns or columns.strip() == "*":
        return "*"
    parsed: list[str] = []
    for column in columns.split(","):
        clean = column.strip()
        if not clean:
            continue
        parsed.append(_quote_identifier(clean))
    if not parsed:
        raise ValueError("Lista de colunas vazia.")
    return ", ".join(parsed)


def _parse_order(order_by: str | None) -> str:
    if not order_by:
        return ""
    parsed: list[str] = []
    for raw_item in str(order_by).split(","):
        item = raw_item.strip()
        if not item:
            continue
        parts = item.split(".")
        column = parts[0].strip()
        direction = parts[1].strip().upper() if len(parts) > 1 else "ASC"
        if direction not in {"ASC", "DESC"}:
            raise ValueError(f"Direcao ORDER BY invalida: {direction!r}")
        parsed.append(f"{_quote_identifier(column)} {direction}")
    if not parsed:
        return ""
    return " ORDER BY " + ", ".join(parsed)


def _split_filters(filters: dict[str, Any] | None) -> tuple[list[str], list[Any], str | None, int | None]:
    where_clauses: list[str] = []
    params: list[Any] = []
    order_by: str | None = None
    limit: int | None = None

    for key, value in (filters or {}).items():
        if key == "order":
            order_by = str(value)
            continue
        if key == "limit":
            limit = int(value)
            continue
        clause, clause_params = _parse_filter(key, value)
        where_clauses.append(clause)
        params.extend(clause_params)

    return where_clauses, params, order_by, limit


def _execute_fetch(
    conn,
    query: str,
    params: Iterable[Any] | None = None,
) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(query, params or [])
        if cur.description is None:
            return []
        return list(cur.fetchall())


def _fetch_rows(
    query: str,
    params: Iterable[Any] | None = None,
    *,
    connection=None,
) -> list[dict]:
    if connection is not None:
        return _execute_fetch(connection, query, params)

    with _acquire_connection() as conn:
        try:
            return _execute_fetch(conn, query, params)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                logger.debug("Rollback falhou apos erro de query.", exc_info=True)
            raise


def db_select(
    table: str,
    columns: str = "*",
    filters: dict[str, Any] | None = None,
    order_by: str | None = None,
    limit: int | None = None,
    *,
    connection=None,
) -> list[dict]:
    where_clauses, params, filters_order_by, filters_limit = _split_filters(filters)
    effective_order = order_by or filters_order_by
    effective_limit = limit if limit is not None else filters_limit

    query = f"SELECT {_parse_columns(columns)} FROM {_quote_qualified_name(table)}"
    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)
    query += _parse_order(effective_order)
    if effective_limit is not None:
        query += " LIMIT %s"
        params.append(int(effective_limit))
    return _fetch_rows(query, params, connection=connection)


def db_insert(
    table: str,
    data: dict | list[dict],
    returning: str = "*",
    *,
    connection=None,
) -> list[dict]:
    rows = data if isinstance(data, list) else [data]
    if not rows:
        return []

    columns = list(rows[0].keys())
    quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
    placeholder_row = "(" + ", ".join(_placeholder(column) for column in columns) + ")"
    values_sql = ", ".join([placeholder_row] * len(rows))
    params: list[Any] = []
    for row in rows:
        missing = [column for column in columns if column not in row]
        if missing:
            raise ValueError(f"Linhas com colunas inconsistentes em INSERT: faltando {missing}")
        for column in columns:
            params.append(_prepare_value(column, row[column]))

    query = f"INSERT INTO {_quote_qualified_name(table)} ({quoted_columns}) VALUES {values_sql}"
    if returning:
        query += f" RETURNING {_parse_columns(returning)}"
    return _fetch_rows(query, params, connection=connection)


def db_update(
    table: str,
    data: dict[str, Any],
    filters: dict[str, Any],
    returning: str = "*",
    *,
    connection=None,
) -> list[dict]:
    if not data:
        return []
    set_parts: list[str] = []
    params: list[Any] = []
    for column, value in data.items():
        set_parts.append(f"{_quote_identifier(column)} = {_placeholder(column)}")
        params.append(_prepare_value(column, value))

    where_clauses, where_params, _order, _limit = _split_filters(filters)
    if not where_clauses:
        raise ValueError("UPDATE sem filtros nao e permitido.")
    params.extend(where_params)

    query = f"UPDATE {_quote_qualified_name(table)} SET " + ", ".join(set_parts)
    query += " WHERE " + " AND ".join(where_clauses)
    if returning:
        query += f" RETURNING {_parse_columns(returning)}"
    return _fetch_rows(query, params, connection=connection)


def db_delete(table: str, filters: dict[str, Any], *, connection=None) -> None:
    where_clauses, params, _order, _limit = _split_filters(filters)
    if not where_clauses:
        raise ValueError("DELETE sem filtros nao e permitido.")
    query = f"DELETE FROM {_quote_qualified_name(table)} WHERE " + " AND ".join(where_clauses)
    _fetch_rows(query, params, connection=connection)


def db_call(
    function_name: str,
    params: dict[str, Any],
    expect_rows: bool = True,
    *,
    connection=None,
) -> list[dict]:
    ordered_items = list((params or {}).items())
    if ordered_items:
        args_sql = ", ".join(
            f"{_quote_identifier(key)} => {_placeholder(key)}" for key, _value in ordered_items
        )
        query_params = [_prepare_value(key, value) for key, value in ordered_items]
    else:
        args_sql = ""
        query_params = []

    qualified_name = _quote_qualified_name(function_name)
    if expect_rows:
        query = f"SELECT * FROM {qualified_name}({args_sql})"
    else:
        query = f"SELECT {qualified_name}({args_sql})"
    return _fetch_rows(query, query_params, connection=connection)


def is_missing_function_error(error: Exception) -> bool:
    if psycopg is None:
        return False
    return isinstance(error, getattr(psycopg.errors, "UndefinedFunction", tuple()))
