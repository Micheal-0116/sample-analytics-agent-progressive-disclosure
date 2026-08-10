"""从 database/*.sql 解析 CREATE TABLE，拿到列名与类型。

为什么解析 DDL 而不是在生成器里重写一遍列定义：DDL 是表结构的真源
（`knowledge/README.md` 明确写了「表结构以 database/*.sql 的 DDL 为准」）。
生成器从它读列，就不可能出现「加了列忘了改生成器」这类漂移；反过来 spec 里
写了 DDL 里不存在的列会立刻报错。

只做够用的解析：CREATE TABLE 块内逐行取「列名 + 类型」，跳过表级约束行
（PRIMARY KEY (...) / FOREIGN KEY / UNIQUE (...) / CHECK）。不解析 CREATE INDEX。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DDL_DIR = ROOT / "database"

# 只吃原始 35 张表的 DDL（01-08）。09_mart 与 10_derived 是 CTAS，不需要生成。
SOURCE_GLOB = "0[1-8]_*.sql"

_CREATE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)\s*\((.*?)\n\)\s*;",
                     re.IGNORECASE | re.DOTALL)
# 表级约束行（不是列）
_TABLE_CONSTRAINT = re.compile(
    r"^\s*(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|CONSTRAINT|EXCLUDE)\b", re.IGNORECASE)


def _split_columns(body: str) -> list[str]:
    """按顶层逗号切分列定义（括号内的逗号不算，如 DECIMAL(12,2)）。"""
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)


def _normalize_type(raw: str) -> str:
    """把 DDL 类型规约成生成器认的几个大类。"""
    t = raw.strip().upper()
    if t.startswith(("BIGSERIAL", "SERIAL")):
        return "INT"           # 序列列在 CSV 里就是整数
    if t.startswith(("BIGINT", "INTEGER", "INT", "SMALLINT")):
        return "INT"
    if t.startswith(("DECIMAL", "NUMERIC", "REAL", "DOUBLE")):
        return "DECIMAL"
    if t.startswith("BOOLEAN"):
        return "BOOL"
    if t.startswith("TIMESTAMP"):
        return "TIMESTAMP"
    if t.startswith("DATE"):
        return "DATE"
    if t.startswith(("JSONB", "JSON")):
        return "JSON"
    if "[]" in t:
        return "ARRAY"
    if t.startswith(("VARCHAR", "CHAR", "TEXT")):
        return "TEXT"
    return "TEXT"


def _extract_type(rest: str) -> str:
    """从列定义的「类型及其后」串里精确取出类型，保留长度/精度。

    不能靠「按空白分词、遇到约束关键字停」：`DECIMAL(12,2)` 里的逗号一旦被当作分隔符
    就会被截成 `DECIMAL(12`，精度丢失后落到兜底类型，Redshift DDL 里静默变成
    DECIMAL(18,4)。这里改成从头逐段匹配：基础类型名 → 可选 (n) 或 (n,m) → 可选 []。
    """
    m = re.match(r"(?i)^\s*([A-Za-z_]+)", rest)
    if not m:
        return rest.strip()
    base, pos = m.group(1), m.end()
    if base.upper() in ("DOUBLE", "CHARACTER"):          # DOUBLE PRECISION / CHARACTER VARYING
        m2 = re.match(r"(?i)\s+([A-Za-z_]+)", rest[pos:])
        if m2:
            base += " " + m2.group(1)
            pos += m2.end()
    m3 = re.match(r"\s*(\(\s*\d+(?:\s*,\s*\d+)?\s*\))", rest[pos:])
    if m3:
        base += re.sub(r"\s+", "", m3.group(1))
        pos += m3.end()
    if re.match(r"\s*\[\s*\]", rest[pos:]):
        base += "[]"
    return base


def parse_all_raw() -> dict[str, list[tuple[str, str, str]]]:
    """→ {表名: [(列名, 原始类型串, 规约类型), ...]}。

    原始类型串保留 VARCHAR(50) / DECIMAL(12,2) 这类长度精度信息，
    Redshift 方言转换需要它（见 redshift_ddl.py）。
    """
    out: dict[str, list[tuple[str, str, str]]] = {}
    for f in sorted(DDL_DIR.glob(SOURCE_GLOB)):
        sql = _strip_comments(f.read_text(encoding="utf-8"))
        for m in _CREATE.finditer(sql):
            name, body = m.group(1), m.group(2)
            cols: list[tuple[str, str, str]] = []
            for piece in _split_columns(body):
                line = piece.strip()
                if not line or _TABLE_CONSTRAINT.match(line):
                    continue
                bits = line.split(None, 1)
                if len(bits) < 2:
                    continue
                col, rest = bits[0].strip('"'), bits[1]
                raw_type = _extract_type(rest)
                first = raw_type.split()[0]
                ctype = "ARRAY" if "[]" in first else _normalize_type(raw_type)
                cols.append((col, raw_type, ctype))
            if cols:
                out[name] = cols
    return out


def parse_all() -> dict[str, list[tuple[str, str]]]:
    """→ {表名: [(列名, 规约类型), ...]}，顺序即 DDL 里的 ordinal_position。"""
    tables: dict[str, list[tuple[str, str]]] = {}
    files = sorted(DDL_DIR.glob(SOURCE_GLOB))
    if not files:
        raise FileNotFoundError(f"没找到 DDL：{DDL_DIR}/{SOURCE_GLOB}")
    for f in files:
        sql = _strip_comments(f.read_text(encoding="utf-8"))
        for m in _CREATE.finditer(sql):
            name, body = m.group(1), m.group(2)
            cols: list[tuple[str, str]] = []
            for piece in _split_columns(body):
                line = piece.strip()
                if not line or _TABLE_CONSTRAINT.match(line):
                    continue
                bits = line.split(None, 1)
                if len(bits) < 2:
                    continue
                col, rest = bits[0].strip('"'), bits[1]
                # ARRAY 判定要看原文（TEXT[]），normalize 前先探一眼
                raw_type = rest.split(",")[0]
                ctype = "ARRAY" if "[]" in raw_type.split()[0] else _normalize_type(raw_type)
                cols.append((col, ctype))
            if cols:
                tables[name] = cols
    return tables


if __name__ == "__main__":
    import sys

    t = parse_all()
    only = sys.argv[1:] or sorted(t)
    print(f"解析到 {len(t)} 张表\n")
    for name in only:
        if name not in t:
            print(f"（无此表：{name}）")
            continue
        print(f"## {name}  ({len(t[name])} 列)")
        for col, ct in t[name]:
            print(f"   {col:<24} {ct}")
        print()
