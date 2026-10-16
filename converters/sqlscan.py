"""Quote/paren-aware lexical helpers for Snowflake DDL (', ", $$, comments)."""
from __future__ import annotations

import re
from typing import Iterator

IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
FQN = rf"{IDENT}(?:\s*\.\s*{IDENT}){{0,2}}"


def scan(text: str, start: int = 0) -> Iterator[tuple[int, int]]:
    """Yield (index, paren_depth) for every character outside quotes and comments."""
    i, n, depth = start, len(text), 0
    while i < n:
        c = text[i]
        if c == "'":
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        if c == '"':
            j = text.find('"', i + 1)
            i = n if j < 0 else j + 1
            continue
        if text.startswith("$$", i):
            j = text.find("$$", i + 2)
            i = n if j < 0 else j + 2
            continue
        if text.startswith("--", i) or text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "(":
            yield i, depth
            depth += 1
            i += 1
            continue
        if c == ")":
            depth -= 1
            yield i, depth
            i += 1
            continue
        yield i, depth
        i += 1


def matching_paren(text: str, open_idx: int) -> int:
    for i, d in scan(text, open_idx):
        if text[i] == ")" and d == 0:
            return i
    raise ValueError("unbalanced parentheses")


def split_top(text: str, sep: str = ",") -> list[str]:
    parts, last = [], 0
    for i, d in scan(text):
        if d == 0 and text[i] == sep:
            parts.append(text[last:i])
            last = i + 1
    parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]


def find_top_kw(text: str, kw: str, start: int = 0) -> int:
    """Index of a whole-word keyword at paren depth 0 outside quotes, or -1."""
    rx = re.compile(rf"\b{kw}\b", re.I)
    L = len(kw)
    for i, d in scan(text, start):
        if d == 0 and text[i].lower() == kw[0].lower():
            if rx.match(text, i) and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_$")):
                if i + L >= len(text) or not (text[i + L].isalnum() or text[i + L] in "_$"):
                    return i
    return -1


def unquote(ident: str) -> str:
    """Snowflake identifier resolution: quoted keeps case, unquoted uppercases."""
    ident = ident.strip()
    if ident.startswith('"') and ident.endswith('"'):
        return ident[1:-1].replace('""', '"')
    return ident.upper()


def split_fqn(fqn: str) -> list[str]:
    return [unquote(p) for p in re.findall(IDENT, fqn)]


def sql_string(lit: str) -> str:
    """Decode a Snowflake '...' literal or $$...$$ body."""
    lit = lit.strip()
    if lit.startswith("$$") and lit.endswith("$$"):
        return lit[2:-2]
    if lit.startswith("'") and lit.endswith("'"):
        return lit[1:-1].replace("''", "'").replace("\\'", "'").replace("\\\\", "\\")
    return lit


def strip_comments(sql: str) -> str:
    """Remove --, // and /* */ comments; string literals and $$ bodies are kept verbatim."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
        elif sql.startswith("$$", i):
            j = sql.find("$$", i + 2)
            j = n if j < 0 else j + 2
            out.append(sql[i:j])
            i = j
        elif c == '"':
            j = sql.find('"', i + 1)
            j = n if j < 0 else j + 1
            out.append(sql[i:j])
            i = j
        elif sql.startswith("--", i) or sql.startswith("//", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)
