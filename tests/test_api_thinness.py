"""The "thin API" requirement, enforced rather than asserted.

"Keep the logic in the database" is the kind of rule that holds for about three
weeks. Someone adds a quick sort, someone else inlines a SELECT because it was
faster than writing a function, and six months later the ranking behaviour lives
in two places that disagree.

So it is a test. These walk the AST of everything under `backend/app/api/` and
`backend/app/main.py` and fail on the specific things that mean logic has leaked
back into the transport layer.

What is deliberately allowed: parameter marshalling, SSE framing, and calling
one DB function or one graph node per handler. What is not: ranking arithmetic,
cost arithmetic, and hand-written tenant filtering that RLS already does.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API_FILES = [
    ROOT / "backend" / "app" / "main.py",
    *(ROOT / "backend" / "app" / "api").glob("*.py"),
]


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("path", API_FILES, ids=lambda p: p.name)
def test_no_tenant_filtering_in_sql(path: Path):
    """Tenant isolation is RLS's job.

    A hand-written `WHERE tenant_id = $1` in a handler is not a second layer of
    safety -- it is a second place to forget, and it hides whether the policy is
    doing anything. The RLS assertions in scripts/verify_rls.sql are the proof;
    duplicating the predicate here would let a broken policy pass unnoticed.
    """
    src = path.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in src.splitlines()
        if "tenant_id =" in line.replace("  ", " ")
        and "WHERE" in line.upper()
        and "--" not in line
    ]
    assert not offenders, f"{path.name}: RLS already filters by tenant:\n" + "\n".join(
        offenders
    )


@pytest.mark.parametrize("path", API_FILES, ids=lambda p: p.name)
def test_no_ranking_or_cost_arithmetic(path: Path):
    """No sorting by score, and no re-deriving cost in Python.

    Cost is a generated column; ranking is SQL. Recomputing either here creates
    a second source of truth that will eventually disagree with the database,
    and the disagreement surfaces as an unreconcilable total months later.
    """
    tree = _parse(path)
    problems: list[str] = []

    for node in ast.walk(tree):
        # `sorted(..., key=lambda x: x["score"])` and friends.
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in {"sorted", "sort"}:
                for kw in node.keywords:
                    if kw.arg != "key":
                        continue
                    # Sorting config filenames for display is transport, not
                    # ranking. Only flag sorts keyed on something score-shaped,
                    # which is what "ranking in Python" actually looks like.
                    key_src = ast.dump(kw.value)
                    if any(
                        t in key_src
                        for t in ("score", "rank", "cost", "similarity", "relevance")
                    ):
                        problems.append(
                            f"line {node.lineno}: sorting by a score/rank/cost key "
                            "-- ranking belongs in SQL"
                        )

        # Division by a token scale is the signature of inlined cost maths.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            right = node.right
            if isinstance(right, ast.Constant) and right.value in (
                1_000_000,
                1000000.0,
            ):
                problems.append(
                    f"line {node.lineno}: dividing by 1e6 -- cost arithmetic "
                    "belongs in the cost_ledger generated column"
                )

    assert not problems, f"{path.name}:\n" + "\n".join(problems)


def test_ask_handler_delegates():
    """The /ask handler should marshal and delegate, not orchestrate.

    Bounded rather than exact, because the SSE envelope legitimately lives here:
    framing events is transport, not business logic. The bound is loose enough
    not to be brittle and tight enough to catch a pipeline being reimplemented
    in the handler.
    """
    tree = _parse(ROOT / "backend" / "app" / "main.py")
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "ask"
        ),
        None,
    )
    assert fn is not None, "no ask() handler found"
    body_lines = (fn.end_lineno or fn.lineno) - fn.lineno
    assert body_lines < 30, f"ask() is {body_lines} lines; it should delegate"


def test_every_sql_string_is_a_function_call_or_single_statement():
    """Handlers may call a function or read a view -- not compute.

    A GROUP BY or a window function in a route handler means an aggregation was
    written in Python's string space instead of as a view, where the CLI, the
    eval harness and a human with psql could all have shared it.
    """
    offenders: list[str] = []
    for path in API_FILES:
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            sql = node.value.upper()
            if "SELECT" not in sql:
                continue
            for banned in (" GROUP BY ", " OVER (", " PERCENTILE_"):
                if banned in sql:
                    offenders.append(
                        f"{path.name}:{node.lineno}: '{banned.strip()}' in a handler "
                        "-- aggregation belongs in a view"
                    )
    assert not offenders, "\n".join(offenders)
