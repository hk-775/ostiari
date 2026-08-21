"""AST-based Athena SELECT policy."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .models import AthenaDatasource


MAX_SQL_BYTES = 64 * 1024


class QueryPolicyError(ValueError):
    """Raised when SQL falls outside the read-only query contract."""


@dataclass(frozen=True)
class ValidatedQuery:
    """Canonical SQL and non-sensitive identity used for execution/audit."""

    sql: str
    sha256: str
    table_count: int


def _same_identifier(actual: str, expected: str) -> bool:
    return actual.casefold() == expected.casefold()


def validate_athena_select(
    sql: object,
    datasource: AthenaDatasource,
) -> ValidatedQuery:
    """Return canonical single-statement SQL or reject it before execution."""
    if (
        not isinstance(sql, str)
        or not sql
        or sql != sql.strip()
        or "\x00" in sql
    ):
        raise QueryPolicyError(
            "sql must be a non-empty string without surrounding whitespace"
        )
    try:
        sql_bytes = sql.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise QueryPolicyError(
            "sql must be valid UTF-8 text"
        ) from exc
    if len(sql_bytes) > MAX_SQL_BYTES:
        raise QueryPolicyError("sql exceeds 64 KiB")
    try:
        statements = sqlglot.parse(
            sql,
            read="athena",
            error_level=sqlglot.ErrorLevel.RAISE,
            # sqlglot logs unsupported Command text before returning its AST.
            # Zero context keeps SQL literals out of application logs.
            error_message_context=0,
        )
    except (ParseError, ValueError) as exc:
        raise QueryPolicyError("sql is not valid Athena SQL") from exc
    if len(statements) != 1 or statements[0] is None:
        raise QueryPolicyError("exactly one SQL statement is required")
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise QueryPolicyError("only SELECT queries are supported")
    if any(
        isinstance(node, (exp.DDL, exp.DML, exp.Command, exp.Into))
        for node in statement.walk()
    ):
        raise QueryPolicyError(
            "DDL, DML, commands, and SELECT INTO are not supported"
        )

    tables = list(statement.find_all(exp.Table))
    for table in tables:
        if not isinstance(table.this, exp.Identifier):
            raise QueryPolicyError("table functions are not supported")
        if table.catalog and not _same_identifier(
            table.catalog,
            datasource.catalog,
        ):
            raise QueryPolicyError(
                "query references a catalog outside the datasource"
            )
        if table.db and not _same_identifier(
            table.db,
            datasource.database,
        ):
            raise QueryPolicyError(
                "query references a database outside the datasource"
            )
        if table.catalog and not table.db:
            raise QueryPolicyError(
                "catalog-qualified tables must include the datasource database"
            )

    canonical = statement.sql(dialect="athena", pretty=False)
    try:
        canonical_bytes = canonical.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise QueryPolicyError(
            "canonical SQL must be valid UTF-8 text"
        ) from exc
    if not canonical or len(canonical_bytes) > MAX_SQL_BYTES:
        raise QueryPolicyError("canonical SQL is empty or exceeds 64 KiB")
    return ValidatedQuery(
        sql=canonical,
        sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        table_count=len(tables),
    )
