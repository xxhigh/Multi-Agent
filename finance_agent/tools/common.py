"""도구 공용 헬퍼: 금액 표기, 안전한 계산기."""

import ast
import operator

from langchain_core.tools import tool


def _krw(value) -> str:
    """원 단위 금액을 조/억 단위로 표기."""
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1e12:
        return f"{sign}{v / 1e12:,.2f}조"
    if v >= 1e8:
        return f"{sign}{v / 1e8:,.0f}억"
    return f"{sign}{v:,.0f}원"


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.UAdd: operator.pos, ast.Mod: operator.mod,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("허용되지 않는 표현식")


@tool
def calculator(expression: str) -> str:
    """사칙연산/거듭제곱 수식을 계산한다. 예: '(120.5 - 98.2) / 98.2 * 100'"""
    try:
        return str(_eval(ast.parse(expression, mode="eval").body))
    except Exception as e:  # noqa: BLE001
        return f"계산 오류: {e}"
