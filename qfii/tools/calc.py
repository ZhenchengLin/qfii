"""Safe arithmetic for agents: numbers, + - * / ** %, parentheses, and a few
functions. Agents never compute numbers themselves; they ask this tool."""

from __future__ import annotations

import ast
import math
import operator
import re

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"abs": abs, "min": min, "max": max, "round": round, "sqrt": math.sqrt,
          "log": math.log, "exp": math.exp}
MAX_LEN = 500


class CalcError(ValueError):
    pass


def calculate(expression: str) -> float:
    if len(expression) > MAX_LEN:
        raise CalcError(f"expression longer than {MAX_LEN} characters")
    try:
        # drop thousands separators (1,234,567) but keep argument commas (max(2, 3))
        cleaned = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", expression).replace("_", "")
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"not an arithmetic expression: {expression!r}") from exc

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            right = ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise CalcError("exponent too large")
            return _BIN[type(node.op)](ev(node.left), right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _FUNCS and not node.keywords:
            return float(_FUNCS[node.func.id](*[ev(a) for a in node.args]))
        raise CalcError(f"unsupported element in expression: {ast.dump(node)[:80]}")

    try:
        value = ev(tree)
    except ZeroDivisionError as exc:
        raise CalcError("division by zero") from exc
    if math.isnan(value) or math.isinf(value):
        raise CalcError("result is not a finite number")
    return value
