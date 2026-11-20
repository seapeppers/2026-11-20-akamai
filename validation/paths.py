"""Tiny path language + `{{ expr }}` templating used by scenario files. Pure, no I/O.

Path syntax (applied to JSON-like data):
  a.b[0].c      dict keys and list indexes
  [*]           map over every list item (result is a list)
  [id=abc]      first list item whose `id` stringifies to `abc` (None if absent)
  ... | len     pipe functions: len sum min max any all first last count
"""
from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r"\.?([A-Za-z_][\w\-]*)|\[([^\]]*)\]")
_MISSING = object()


class A(dict):
    """dict with attribute access (nested dicts are wrapped lazily)."""

    def __getattr__(self, k: str) -> Any:
        try:
            v = self[k]
        except KeyError as e:
            raise AttributeError(k) from e
        return A(v) if type(v) is dict else v


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def extract(obj: Any, path: str) -> Any:
    path = path.strip()
    pipes: list[str] = []
    if "|" in path:
        path, *rest = path.split("|")
        pipes = [p.strip() for p in rest]
        path = path.strip()
    if path.startswith("$"):
        path = path[1:]
    cur: Any = obj
    multi = False
    pos = 0
    while pos < len(path):
        m = _TOKEN.match(path, pos)
        if not m:
            raise ValueError(f"bad path {path!r} at {pos}")
        pos = m.end()
        name, br = m.group(1), m.group(2)
        if name is not None:
            cur = [_get(x, name) for x in cur] if multi else _get(cur, name)
        elif br == "*":
            if not isinstance(cur, list):
                cur = []
            multi = True
        elif re.fullmatch(r"-?\d+", br):
            i = int(br)
            if multi:
                cur = [x[i] if isinstance(x, list) and -len(x) <= i < len(x) else None for x in cur]
            else:
                cur = cur[i] if isinstance(cur, list) and -len(cur) <= i < len(cur) else None
        elif "=" in br:
            k, v = br.split("=", 1)
            items = cur if isinstance(cur, list) else []
            cur = next((x for x in items if str(_get(x, k.strip())) == v.strip()), None)
        else:
            raise ValueError(f"bad bracket [{br}] in {path!r}")
    for p in pipes:
        cur = _PIPES[p](cur)
    return cur


def _nums(x):
    return [v for v in (x or []) if isinstance(v, (int, float)) and not isinstance(v, bool)]


_PIPES = {
    "len": lambda x: len(x) if x is not None else 0,
    "count": lambda x: len(x) if x is not None else 0,
    "sum": lambda x: sum(_nums(x)),
    "min": lambda x: min(_nums(x)) if _nums(x) else None,
    "max": lambda x: max(_nums(x)) if _nums(x) else None,
    "any": lambda x: any(bool(v) for v in (x or [])),
    "all": lambda x: all(bool(v) for v in (x or [])),
    "first": lambda x: x[0] if x else None,
    "last": lambda x: x[-1] if x else None,
}

# ---------------------------------------------------------------- templating

_FULL = re.compile(r"^\s*\{\{(.*?)\}\}\s*$", re.S)
_ANY = re.compile(r"\{\{(.*?)\}\}", re.S)
_SAFE = {"min": min, "max": max, "int": int, "float": float, "round": round, "len": len,
         "sum": sum, "abs": abs, "sorted": sorted, "list": list, "str": str, "ceil": lambda x: int(-(-x // 1)),
         "True": True, "False": False, "None": None}


def _wrap(ctx: dict) -> dict:
    return {k: (A(v) if type(v) is dict else v) for k, v in ctx.items()}


def eval_expr(expr: str, ctx: dict) -> Any:
    try:
        return eval(expr.strip(), {"__builtins__": {}, **_SAFE}, _wrap(ctx))  # scenario files are trusted
    except Exception as e:
        raise ValueError(f"template error in {{{{{expr.strip()}}}}}: {e}") from e


def render(value: Any, ctx: dict) -> Any:
    """Recursively render `{{ expr }}`. A string that is exactly one template keeps its native type."""
    if isinstance(value, str):
        m = _FULL.match(value)
        if m:
            return eval_expr(m.group(1), ctx)
        return _ANY.sub(lambda mm: str(eval_expr(mm.group(1), ctx)), value)
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    return value
