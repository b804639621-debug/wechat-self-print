# -*- coding: utf-8 -*-
"""读取生产库 print_history 的记录（按时间倒序前 N 条 + 今天统计）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(r"E:\kaifa\自助打印\dist\printshop.db")
DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-09-10"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

rows = list(con.execute(
    "SELECT id, file_name, category, size_bytes, page_count, status, options_json, error,"
    " created_at, printed_at FROM print_history ORDER BY id"))

print(f"print_history 共 {len(rows)} 条\n")
print("=== 最近 20 条 ===")
for r in rows[-20:]:
    print(f"  #{r['id']:<3} {r['created_at']}  {str(r['file_name'])[:26]:<26} "
          f"{str(r['category']):<7} {r['size_bytes'] or 0:>8}B {r['page_count'] or '-':>3}页 "
          f"status={r['status']:<10} opts={str(r['options_json'])[:40]} err={r['error'] or '-'}")

today = [r for r in rows if str(r["created_at"]).startswith(DAY)]
print(f"\n=== {DAY} 共 {len(today)} 条 ===")
for r in today:
    print(f"  #{r['id']:<3} {r['created_at']}  {str(r['file_name'])[:26]:<26} "
          f"{str(r['category']):<7} {r['size_bytes'] or 0:>8}B {r['page_count'] or '-':>3}页 "
          f"status={r['status']} printed_at={r['printed_at']}")

from collections import Counter
print(f"\n状态分布(今天): {dict(Counter(str(r['status']) for r in today))}")
print(f"状态分布(全部): {dict(Counter(str(r['status']) for r in rows))}")
print(f"任务号区间: #{rows[0]['id']} ~ #{rows[-1]['id']}" if rows else "")
