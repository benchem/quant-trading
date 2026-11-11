#!/usr/bin/env python
"""2026-11-11 两股终审：聚合 R1-R3 全部轮次产物 → 终局记分板 JSON（报告的事实源）
- 分数：R3 终锁五家加权（去极值）
- 轮次统计：agents/ 下全部文件计数
"""
import json, re, glob, os, sys
sys.path.insert(0, '.')
from aggregate import parse_file, W, CODES, NAMES

os.chdir('/root/workspaces/quant-trading/deepscan/20261111_2cands')

final = {}
# 1) R3 终锁分数
r3 = json.load(open('scores_R3.json'))
final['scores'] = r3['result']

# 2) 轮次统计
files = sorted(glob.glob('agents/*.md'))
waves = {}
for f in files:
    w = os.path.basename(f).split('-')[0]
    waves.setdefault(w, []).append(os.path.basename(f))
final['rounds'] = {'files': len(files), 'by_wave': {k: len(v) for k, v in sorted(waves.items())},
                   'detail': {k: v for k, v in sorted(waves.items())}}

# 3) R1→R3 漂移检测（收敛信号）
r1 = json.load(open('scores_R1.json')); r2 = json.load(open('scores_R2.json'))
drift = {}
for code in CODES:
    drift[code] = {'R1': r1['result'][code]['total'], 'R2': r2['result'][code]['total'],
                   'R3': r3['result'][code]['total'],
                   'moved_R1_to_R3': round(r3['result'][code]['total'] - r1['result'][code]['total'], 3)}
final['drift'] = drift

json.dump(final, open('FINAL.json','w'), ensure_ascii=False, indent=1)

print("══ 终局记分板 ══")
for code, r in r3['result'].items():
    print(f"{r['name']} {code}: {r['total']:.2f} | " + ' '.join(f"{d}{r['dims'][d]:.1f}" for d in 'MVSFR'))
print(f"\n轮次文件: {len(files)} 个 / {waves}")
print(f"漂移 R1→R3: " + ', '.join(f"{NAMES[c]} {d['moved_R1_to_R3']:+.2f}" for c, d in drift.items()))
