"""Postgres DDL 类型 → PyArrow 类型的映射（Redshift COPY 的硬约束）。

## 为什么必须精确匹配

Redshift `COPY ... FORMAT AS PARQUET` 走 Spectrum 读文件，**Parquet 的物理类型要与目标
列类型兼容**，不兼容会报：

    Spectrum Scan Error / code 15007
    has an incompatible Parquet schema for column '....user_level'

踩过的具体case：`user_level` 在 Postgres 是 `INT`，Redshift 建成 `INTEGER`（int32），
而 numpy 默认给 int64 → 整表 COPY 失败。金额列同理：`DECIMAL(12,2)` 不接受 Parquet 的
double，必须写成 decimal128(12,2)。

所以类型映射不能靠"差不多"，得从 DDL 的原始类型串精确推导。这里是唯一定义处，
事实表（gen/main.py）和维度表（gen/dims_to_parquet.py）共用。

## SUPER 列的额外要求

JSONB / TEXT[] 在 Redshift 侧是 SUPER，Parquet 里写 JSON 字符串，且 COPY 必须加
`SERIALIZETOJSON`，否则报 `SUPER column in COPY query requires SERIALIZETOJSON option`。
用 `needs_serializetojson()` 判断某张表是否要加这个选项。
"""
from __future__ import annotations

import re


def arrow_type(raw: str):
    """DDL 原始类型串（如 'DECIMAL(12,2)'、'VARCHAR(50)'、'TEXT[]'）→ pyarrow 类型。"""
    import pyarrow as pa

    t = raw.strip()
    up = t.upper()

    if "[]" in up.split()[0] or up.startswith(("JSONB", "JSON")):
        return pa.string()                       # SUPER 列走 JSON 文本
    if up.startswith("BIGSERIAL") or up.startswith("BIGINT"):
        return pa.int64()
    if up.startswith("SMALLINT"):
        return pa.int16()
    if up.startswith("SERIAL") or up.startswith(("INTEGER", "INT")):
        return pa.int32()                        # Redshift INTEGER = int32，不是 int64
    m = re.match(r"(?i)^(DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", t)
    if m:
        return pa.decimal128(int(m.group(2)), int(m.group(3)))
    if up.startswith(("DECIMAL", "NUMERIC")):
        return pa.decimal128(18, 4)
    if up.startswith("DOUBLE"):
        return pa.float64()
    if up.startswith("REAL"):
        return pa.float32()
    if up.startswith("BOOLEAN"):
        return pa.bool_()
    if up.startswith("TIMESTAMP"):
        return pa.timestamp("us")
    if up.startswith("DATE"):
        return pa.date32()
    return pa.string()


def schema_for(cols: list[tuple[str, str, str]]):
    """[(列名, 原始类型, 规约类型)] → pyarrow.schema，顺序即 DDL 顺序。"""
    import pyarrow as pa
    return pa.schema([(name, arrow_type(raw)) for name, raw, _ in cols])


def needs_serializetojson(cols) -> bool:
    """该表是否含 SUPER 列（JSONB / 数组），COPY 时需要 SERIALIZETOJSON。"""
    for item in cols:
        ctype = item[2] if len(item) >= 3 else item[1]
        if ctype in ("JSON", "ARRAY"):
            return True
    return False
