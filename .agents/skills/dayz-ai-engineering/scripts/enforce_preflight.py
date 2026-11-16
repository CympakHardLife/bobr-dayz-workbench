#!/usr/bin/env python3
"""Проверка формы собственных DayZ Enforce .c без изменения файлов.

Это консервативное правило проекта, не компилятор и не универсальная грамматика.
Проверяет переносы внутри скобок и между бинарными операндами вне общих скобок.
Комментарии, директивы и содержимое литералов не разбирает как код; литералы
считает атомарными операндами. Ничего не переписывает и не устанавливает.

Команда: python enforce_preflight.py [--strict] FILE_OR_DIRECTORY ...
Коды выхода: 0 — проверка выполнена, 1 — находки при --strict, 2 — ошибка входа.
Требуется только стандартная библиотека Python. JSON выводится в stdout UTF-8.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys


POLICY = "Project formatting policy; not an Enforce compiler or a language ban."
BINARY = frozenset(("&&", "||", "+", "-", "|", "&", "*", "/", "%", "^", "=",
                    "+=", "-=", "*=", "/=", "%=", "|=", "&=", "^="))
OPERATORS = tuple(sorted(BINARY | {"==", "!=", "<=", ">=", "++", "--", "<<", ">>"},
                         key=len, reverse=True))
NON_OPERANDS = frozenset(("if", "else", "for", "foreach", "while", "switch", "case",
                         "default", "return", "break", "continue", "class", "enum",
                         "extends", "void", "int", "float", "bool", "string", "vector",
                         "typename", "ref", "autoptr", "static", "const", "private",
                         "protected", "override", "proto", "native", "out", "inout"))
NUMBER = re.compile(r"(?:0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)")
IDENTIFIER = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    start: int
    end: int


def lex(text: str) -> tuple[list[Token], list[int]]:
    """Сохраняет литералы как атомы, учитывает переносы кода и пробелов."""
    tokens: list[Token] = []
    newlines: list[int] = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char.isspace():
            if char == "\n":
                newlines.append(index)
            index += 1
            continue
        if char == "#" and not text[text.rfind("\n", 0, index) + 1:index].strip():
            # Пропускаем директиву вместе с явными продолжениями строк.
            while True:
                end = text.find("\n", index)
                if end < 0:
                    index = size
                    break
                if not text[index:end].rstrip("\r").endswith("\\"):
                    index = end
                    break
                index = end + 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            index = size if end < 0 else end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = size if end < 0 else end + 2
            continue
        if char in ('"', "'"):
            quote = char
            end = index + 1
            while end < size:
                if text[end] == "\\":
                    end += 2
                elif text[end] == quote:
                    end += 1
                    break
                else:
                    end += 1
            end = min(end, size)
            tokens.append(Token("literal", text[index:end], index, end))
            index = end
            continue
        if char.isdigit() or (char == "." and index + 1 < size and text[index + 1].isdigit()):
            match = NUMBER.match(text, index)
            if match:
                tokens.append(Token("number", match.group(), index, match.end()))
                index = match.end()
                continue
        if char.isalpha() or char == "_":
            match = IDENTIFIER.match(text, index)
            assert match is not None
            tokens.append(Token("identifier", match.group(), index, match.end()))
            index = match.end()
            continue
        operator = next((op for op in OPERATORS if text.startswith(op, index)), None)
        value = operator if operator is not None else char
        tokens.append(Token("operator" if operator is not None else "punctuation",
                            value, index, index + len(value)))
        index += len(value)
    return tokens, newlines


def ends_operand(token: Token) -> bool:
    return (token.kind in ("literal", "number")
            or (token.kind == "identifier" and token.value not in NON_OPERANDS and token.value != "new")
            or token.value in (")", "]", "++", "--"))


def starts_operand(token: Token) -> bool:
    return (token.kind in ("literal", "number")
            or (token.kind == "identifier" and token.value not in NON_OPERANDS)
            or token.value in ("(", "+", "-", "!", "~"))


def check_text(text: str, path: str = "<memory>") -> list[dict]:
    """Возвращает находки формы, не меняя и не исполняя проверяемый код."""
    tokens, code_newlines = lex(text)
    physical_starts = [0] + [match.end() for match in re.finditer("\n", text)]

    def location(offset: int) -> tuple[int, int]:
        line_index = bisect_right(physical_starts, offset) - 1
        return line_index + 1, offset - physical_starts[line_index] + 1

    def newline_between(start: int, end: int) -> bool:
        return bisect_left(code_newlines, start) < bisect_left(code_newlines, end)

    def finding(code: str, kind: str, start: int, end: int, message: str, **extra) -> dict:
        line, column = location(start)
        end_line, end_column = location(max(start, end - 1))
        return {"path": path, "code": code, "kind": kind, "line": line,
                "column": column, "end_line": end_line, "end_column": end_column,
                "message": message, **extra}

    findings: list[dict] = []
    stack: list[int] = []
    contexts: list[tuple[int, ...]] = []
    for index, token in enumerate(tokens):
        if token.value == "(":
            stack.append(index)
            contexts.append(tuple(stack))
        elif token.value == ")":
            contexts.append(tuple(stack))
            if stack:
                opening = tokens[stack.pop()]
                if not stack and newline_between(opening.end, token.start):
                    findings.append(finding(
                        "ENF-MULTILINE-PARENS", "parenthesized_expression_or_signature",
                        opening.start, token.end,
                        "Project policy: keep this parenthesized expression/signature on one physical line."))
        else:
            contexts.append(tuple(stack))

    for index in range(1, len(tokens)):
        previous, current = tokens[index - 1:index + 1]
        if not newline_between(previous.end, current.start):
            continue
        if set(contexts[index - 1]).intersection(contexts[index]):
            continue  # Внешние многострочные скобки уже охватывают эту находку.
        operator = None
        kind = None
        if (current.value in BINARY and ends_operand(previous)
                and index + 1 < len(tokens) and starts_operand(tokens[index + 1])):
            operator, kind = current.value, "leading_binary_operator"
        elif (previous.value in BINARY and index >= 2 and ends_operand(tokens[index - 2])
              and starts_operand(current)):
            operator, kind = previous.value, "trailing_binary_operator"
        if operator is not None:
            findings.append(finding(
                "ENF-BINARY-CONTINUATION", kind, previous.start, current.end,
                "Project policy: keep this binary expression on one physical line.", operator=operator))
    return sorted(findings, key=lambda row: (row["line"], row["column"], row["code"]))


def collect(paths: list[str]) -> tuple[list[Path], list[dict]]:
    files: set[Path] = set()
    errors: list[dict] = []
    for name in paths:
        path = Path(name).expanduser()
        try:
            if path.is_dir():
                files.update(item.resolve() for item in path.rglob("*")
                             if item.is_file() and item.suffix.lower() == ".c")
            elif path.is_file() and path.suffix.lower() == ".c":
                files.add(path.resolve())
            else:
                errors.append({"path": str(path), "message": "Expected an existing .c file or directory."})
        except OSError as error:
            errors.append({"path": str(path), "message": str(error)})
    return sorted(files, key=lambda item: str(item).casefold()), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", metavar="PATH", help=".c files or directories to scan recursively")
    parser.add_argument("--strict", action="store_true", help="return exit code 1 when policy findings exist")
    args = parser.parse_args(argv)
    files, errors = collect(args.paths)
    if not files and not errors:
        errors.append({"path": None, "message": "No .c files found in the provided inputs."})
    findings: list[dict] = []
    checked = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            errors.append({"path": str(path), "message": str(error)})
            continue
        checked += 1
        findings.extend(check_text(text, str(path)))
    result = {"tool": "skill_enforce_preflight", "policy": POLICY,
              "read_only": True, "enforce_compiled": False, "strict": args.strict,
              "files_checked": checked, "findings_count": len(findings),
              "ok": not findings and not errors, "findings": findings, "errors": errors}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if errors else (1 if args.strict and findings else 0)


if __name__ == "__main__":
    raise SystemExit(main())
