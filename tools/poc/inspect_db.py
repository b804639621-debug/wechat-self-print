# -*- coding: utf-8 -*-
"""只读查看生产库 dist/printshop.db 的结构与今天的任务记录。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(r"E:\kaifa\自助打印\dist\printshop.db")
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print(f"库: {DB}  表: {tables}")

for t in tables:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"\n--- {t}  ({n} 行)  列={cols}")

# 打印 job 相关表内容
for t in tables:
    if "job" not in t.lower():
        continue
    print(f"\n=== {t} 全部记录 ===")
    for row in con.execute(f"SELECT * FROM {t} ORDER BY rowid"):
        d = dict(row)
        print("  " + "  ".join(f"{k}={v}" for k, v in d.items()))
