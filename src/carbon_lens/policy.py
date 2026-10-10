"""Strict write-operation classification for CarbonLens.

This module intentionally accepts a small, explicit SQL subset.  It is a
tokenizer plus grammar validator rather than a keyword replacement scheme:
comments, quoted strings, quoted identifiers, CTEs and semicolons are handled
before an operation is admitted.  Anything outside the grammar is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from .beeline import validate_read_only
from .config import CarbonLensError


class WritePolicyError(CarbonLensError):
    """A write request failed a server-side policy check."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class SqlPlan:
    kind: str
    target: str
    source_sql: str | None
    normalized_sql: str
    mutates_data: bool = True
    requires_owned_target: bool = True


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int
    kind: str


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_FORBIDDEN_ANYWHERE = {
    "ADD", "ALTER", "CALL", "DELETE", "EXTERNAL", "FUNCTION", "GRANT",
    "LOAD", "LOCATION", "MERGE", "MSCK", "RENAME", "REPLACE", "REVOKE",
    "SET", "TRUNCATE", "UPDATE", "USE",
}


def _tokens(sql: str) -> list[_Token]:
    result: list[_Token] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise WritePolicyError("UNSUPPORTED_SQL", "unterminated SQL comment")
            index = end + 2
            continue
        if char in "'\"`":
            quote = char
            start = index
            index += 1
            closed = False
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                raise WritePolicyError("UNSUPPORTED_SQL", "unterminated quoted value or identifier")
            result.append(_Token(sql[start:index], start, index, "quoted"))
            continue
        match = _WORD.match(sql, index) or _NUMBER.match(sql, index)
        if match:
            result.append(_Token(match.group(0), index, match.end(), "word"))
            index = match.end()
            continue
        result.append(_Token(char, index, index + 1, "symbol"))
        index += 1
    if not result:
        raise WritePolicyError("UNSUPPORTED_SQL", "SQL cannot be empty")
    return result


def _split_statement(tokens: list[_Token]) -> list[_Token]:
    semicolons = [index for index, token in enumerate(tokens) if token.value == ";"]
    if len(semicolons) > 1 or (semicolons and semicolons[0] != len(tokens) - 1):
        raise WritePolicyError("UNSUPPORTED_SQL", "exactly one SQL statement is required")
    return tokens[:-1] if semicolons else tokens


def _word(token: _Token | None) -> str:
    return token.value.upper() if token is not None and token.kind == "word" else ""


def _identifier(token: _Token | None) -> str:
    if token is None:
        raise WritePolicyError("UNSUPPORTED_SQL", "missing identifier")
    value = token.value
    if token.kind == "quoted":
        if not value.startswith("`") or not value.endswith("`"):
            raise WritePolicyError("UNSUPPORTED_SQL", "only backtick quoted identifiers are supported")
        value = value[1:-1].replace("``", "`")
    if not _IDENTIFIER.fullmatch(value):
        raise WritePolicyError("UNSUPPORTED_SQL", f"invalid identifier: {value}")
    return value


def _qualified_name(tokens: list[_Token], index: int) -> tuple[str, int]:
    parts = [_identifier(tokens[index])]
    index += 1
    while index < len(tokens) and tokens[index].value == ".":
        if index + 1 >= len(tokens):
            raise WritePolicyError("UNSUPPORTED_SQL", "incomplete qualified table name")
        parts.append(_identifier(tokens[index + 1]))
        index += 2
        if len(parts) > 2:
            raise WritePolicyError("UNSUPPORTED_SQL", "catalog-qualified names are not supported")
    if len(parts) != 2:
        raise WritePolicyError("UNSUPPORTED_SQL", "write targets must be schema.table")
    return ".".join(parts), index


def _source_sql(sql: str, tokens: list[_Token], index: int) -> str:
    if index >= len(tokens):
        raise WritePolicyError("UNSUPPORTED_SQL", "write operation requires a SELECT source")
    source = sql[tokens[index].start : tokens[-1].end].strip()
    if source.endswith(";"):
        source = source[:-1].rstrip()
    validate_read_only(source)
    if _word(tokens[index]) not in {"SELECT", "WITH"}:
        raise WritePolicyError("UNSUPPORTED_SQL", "write source must be SELECT or WITH ... SELECT")
    return source


def _reject_forbidden(tokens: Iterable[_Token]) -> None:
    for token in tokens:
        if _word(token) in _FORBIDDEN_ANYWHERE:
            raise WritePolicyError("UNSUPPORTED_SQL", f"unsupported or side-effecting clause: {token.value}")


def parse_write_sql(sql: str, workspace_schema: str, table_prefix: str = "cl_tmp_") -> SqlPlan:
    """Parse one allowed write statement and bind it to one workspace schema."""
    if "\x00" in sql:
        raise WritePolicyError("UNSUPPORTED_SQL", "SQL contains a NUL character")
    tokens = _split_statement(_tokens(sql))
    _reject_forbidden(tokens)
    if not workspace_schema or "." in workspace_schema or not _IDENTIFIER.fullmatch(workspace_schema):
        raise WritePolicyError("WORKSPACE_NOT_READY", "workspace schema is not a single explicit identifier")
    if not table_prefix or not _IDENTIFIER.fullmatch(table_prefix):
        raise WritePolicyError("WORKSPACE_NOT_READY", "table prefix is not a valid identifier prefix")
    first = _word(tokens[0])
    if first == "CREATE":
        if len(tokens) < 5 or _word(tokens[1]) != "TABLE":
            raise WritePolicyError("UNSUPPORTED_SQL", "only CREATE TABLE ... AS SELECT is supported")
        if _word(tokens[2]) in {"IF", "OR"}:
            raise WritePolicyError("UNSUPPORTED_SQL", "CREATE IF NOT EXISTS and CREATE OR REPLACE are forbidden")
        target, index = _qualified_name(tokens, 2)
        if _word(tokens[index]) != "AS":
            raise WritePolicyError("UNSUPPORTED_SQL", "CREATE must use AS SELECT")
        source = _source_sql(sql, tokens, index + 1)
        if target.split(".", 1)[0] != workspace_schema:
            raise WritePolicyError("UNOWNED_TABLE", "CTAS target is outside the configured workspace")
        if not target.split(".", 1)[1].startswith(table_prefix):
            raise WritePolicyError("UNOWNED_TABLE", "CTAS target does not use the managed table prefix")
        return SqlPlan("create_table_as_select", target, source, sql.strip())
    if first == "INSERT":
        if len(tokens) < 4:
            raise WritePolicyError("UNSUPPORTED_SQL", "only INSERT INTO/OVERWRITE TABLE is supported")
        overwrite = _word(tokens[1]) == "OVERWRITE"
        if overwrite:
            if len(tokens) < 5:
                raise WritePolicyError("UNSUPPORTED_SQL", "INSERT OVERWRITE requires TABLE and a SELECT source")
            if _word(tokens[2]) != "TABLE":
                raise WritePolicyError("UNSUPPORTED_SQL", "INSERT OVERWRITE DIRECTORY is forbidden")
            target, index = _qualified_name(tokens, 3)
        else:
            if _word(tokens[1]) != "INTO":
                raise WritePolicyError("UNSUPPORTED_SQL", "only INSERT INTO/OVERWRITE TABLE is supported")
            index = 2
            if _word(tokens[index]) == "TABLE":
                index += 1
            target, index = _qualified_name(tokens, index)
        source = _source_sql(sql, tokens, index)
        if target.split(".", 1)[0] != workspace_schema:
            raise WritePolicyError("UNOWNED_TABLE", "INSERT target is outside the configured workspace")
        return SqlPlan("insert_overwrite" if overwrite else "insert_into", target, source, sql.strip())
    if first == "DROP":
        if len(tokens) < 5 or _word(tokens[1]) != "TABLE":
            raise WritePolicyError("UNSUPPORTED_SQL", "only DROP TABLE <managed table> is supported")
        target, index = _qualified_name(tokens, 2)
        if index != len(tokens) or target.split(".", 1)[0] != workspace_schema:
            raise WritePolicyError("UNOWNED_TABLE", "DROP target is outside the configured workspace")
        return SqlPlan("drop_table", target, None, sql.strip())
    raise WritePolicyError("UNSUPPORTED_SQL", f"write operation is not allowed: {first or '<unknown>'}")
