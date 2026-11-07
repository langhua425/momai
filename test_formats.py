#!/usr/bin/env python3
"""在新解析器下逐格式验证：真实样本 → (text长度, kind, note)"""
import sys, os
sys.path.insert(0, "/opt/wenmai")
import extract as X
import db

TARGETS = [".doc", ".xls", ".ppt", ".wps", ".et", ".mht", ".mhtml", ".rtf", ".dbf",
           ".docm", ".xlsm", ".odt", ".ods", ".dps", None]

with db.cursor(commit=False) as con:
    for ext in TARGETS:
        if ext is None:
            rows = con.run("""SELECT real_path, name FROM files
                              WHERE ext='' OR ext IS NULL ORDER BY size DESC LIMIT 2""")
            label = "(无扩展名)"
        else:
            rows = con.run("""SELECT real_path, name FROM files
                              WHERE ext=:e ORDER BY size DESC LIMIT 2""", e=ext)
            label = ext
        if not rows:
            continue
        print(f"\n=== {label} ===")
        for rpath, name in rows:
            if not rpath or not os.path.exists(rpath):
                print(f"  {name[:40]} → 文件不在盘上")
                continue
            try:
                txt, kind, note = X.extract(rpath)
                head = txt[:60].replace("\n", " ") if txt else ""
                print(f"  {name[:36]:<36} → {len(txt):>6} 字  kind={kind:<10} {note[:38]}")
                if head:
                    print(f"      「{head}」")
            except Exception as e:
                print(f"  {name[:36]} → ✗ {type(e).__name__}: {e}")
