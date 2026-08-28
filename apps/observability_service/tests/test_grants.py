"""Test 11 and its converse: the isolation claim, on the real roles.

v1.2 §1.3 says the detector has no permission, API, import, or timing signal
from the injector. This is the permission half, and it is the reason the
scenario tables exist on Day 1 at all -- a grant test needs a real table to be
denied on.

Isolation runs both ways. A detector that cannot read ground truth is only half
the claim; a scenario controller that cannot read telemetry is the other half.
"""

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

DETECTOR_URL = os.environ.get(
    "DATABASE_URL_DETECTOR",
    "postgresql+asyncpg://blastradius_detector:detector@postgres:5432/blastradius",
)
SCENARIO_URL = os.environ.get(
    "DATABASE_URL_SCENARIO",
    "postgresql+asyncpg://blastradius_scenario:scenario@postgres:5432/blastradius",
)
APP_URL = os.environ.get(
    "DATABASE_URL_APP",
    "postgresql+asyncpg://blastradius_app:app@postgres:5432/blastradius",
)


async def _select(url: str, table: str):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.execute(sa.text(f'SELECT * FROM "{table}" LIMIT 1'))
    finally:
        await engine.dispose()


#: SQLSTATE for insufficient_privilege. The assertion is written against this
#: rather than an exception class because SQLAlchemy's asyncpg dialect re-wraps
#: the driver error twice, and the wrapper type is an implementation detail.
INSUFFICIENT_PRIVILEGE = "42501"


def _chain(exc: BaseException):
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = getattr(exc, "orig", None) or exc.__cause__


async def _assert_denied(url: str, table: str) -> None:
    from asyncpg.exceptions import InsufficientPrivilegeError

    with pytest.raises(Exception) as excinfo:
        await _select(url, table)

    causes = list(_chain(excinfo.value))
    denied = any(
        isinstance(c, InsufficientPrivilegeError)
        or getattr(c, "sqlstate", None) == INSUFFICIENT_PRIVILEGE
        or "InsufficientPrivilegeError" in str(c)
        for c in causes
    )
    assert denied, f"expected permission denial on {table}, got {causes[-1]!r}"


# --- test 11 --------------------------------------------------------------
@pytest.mark.parametrize("table", ["ground_truth", "scenario_run", "order"])
async def test_detector_cannot_read_scenario_or_order_state(table):
    await _assert_denied(DETECTOR_URL, table)


@pytest.mark.parametrize("table", ["span", "trace"])
async def test_scenario_cannot_read_telemetry(table):
    await _assert_denied(SCENARIO_URL, table)


@pytest.mark.parametrize("table", ["span", "trace", "ground_truth"])
async def test_app_cannot_read_telemetry_or_scenario_state(table):
    await _assert_denied(APP_URL, table)


@pytest.mark.parametrize("table", ["span", "trace", "ingest_state", "service", "domain"])
async def test_detector_can_read_what_it_owns(table):
    result = await _select(DETECTOR_URL, table)
    assert result is not None


async def test_telemetry_tables_carry_no_scenario_reference():
    """v1.2 §26: the telemetry side has no scenario reference.

    `incident` is a Day 2 table. What Day 1 can assert is the weaker but still
    meaningful form: no detector-owned table has acquired a scenario column.
    """
    engine = create_async_engine(DETECTOR_URL)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT table_name, column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' "
                        "AND table_name IN ('span','trace','ingest_state') "
                        "AND (column_name LIKE '%scenario%' "
                        "     OR column_name LIKE '%ground_truth%' "
                        "     OR column_name LIKE '%reveal%')"
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    assert rows == []


def test_observability_service_never_imports_the_injector():
    """v1.2 §21.4.4, brought forward.

    No detector source file may reference the scenario controller or its tables.
    The import lint is cheap and it catches the failure mode the grants cannot:
    a developer reaching for ground truth in Python rather than in SQL.
    """
    import ast
    from pathlib import Path

    forbidden = ("scenario_controller", "ground_truth", "scenario_run", "reveal")
    root = Path(__file__).resolve().parents[1] / "app"

    def code_tokens(tree: ast.AST) -> set[str]:
        """Identifiers and string literals, excluding docstrings.

        Prose has to be excluded or the lint fires on its own explanation of
        what the detector may not read. What it must catch is a reference the
        interpreter would act on: an import, an attribute, or SQL text.
        """
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        tokens: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                tokens.add(node.id)
            elif isinstance(node, ast.Attribute):
                tokens.add(node.attr)
            elif isinstance(node, ast.alias):
                tokens.update(filter(None, (node.name, node.asname)))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    tokens.add(node.value)
        return tokens

    offenders = [
        (path.name, term)
        for path in root.rglob("*.py")
        for tokens in [code_tokens(ast.parse(path.read_text(encoding="utf-8")))]
        for term in forbidden
        if any(term in token for token in tokens)
    ]
    assert offenders == [], f"detector source references injector concepts: {offenders}"
