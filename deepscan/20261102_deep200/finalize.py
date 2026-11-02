#!/usr/bin/env python3
"""终审聚合：R2 分数去极值均值+加权，输出 final_scores.json + 终审榜"""
import json, re, glob, csv, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
DIMS = ['M', 'V', 'S', 'F', 'R']
W = {'M': 0.25, 'V': 0.20, 'S': 0.20, 'F': 0.20, 'R': 0.15}

def load_round(prefix):
    out = {}
    for f in sorted(glob.glob(f'agents/{prefix}-*.md')):
        school = os.path.basename(f)[3:-3]  # strip 'R1-'/'R2-' and '.md'
        txt = open(f, encoding='utf-8').read()
        d = json.loads(re.search(r'```json\s*(\{.*?\})\s*```', txt, re.S).group(1))
        out[school] = {x['code']: x for x in d['scores']}
    return out

r1, r2 = load_round('R1'), load_round('R2')
schools = sorted(r2)
assert sorted(r1) == schools, 'R1/R2 流派集合不一致'
print(f'五流派: {schools} | 各家 {sorted(set(len(v) for v in r2.values()))} 只')

# ±2 校验（我的独立复核，不信子代理自报）
viol = [(s, c, dm, r1[s][c][dm], r2[s][c][dm])
        for s in schools for c in r2[s] for dm in DIMS
        if abs(r2[s][c][dm] - r1[s][c][dm]) > 2]
print('±2 违规:', viol if viol else '0 处 ✓')

# 股票集合一致性
codes = set(r2[schools[0]])
for s in schools[1:]:
    assert set(r2[s]) == codes, f'{s} 股票集合不一致'
print(f'股票集合一致: {len(codes)} 只 ✓')

names = {r['code']: r['name'] for r in csv.DictReader(open('candidates_top30.csv'))}
meta = {r['code']: r for r in csv.DictReader(open('candidates_top30.csv'))}

def agg(c):
    return {dm: sum(sorted(r2[s][c][dm] for s in schools)[1:-1]) / 3 for dm in DIMS}

def total(c):
    return sum(W[dm] * agg(c)[dm] for dm in DIMS)

def spread(c):
    return max(max(r2[s][c][dm] for s in schools) - min(r2[s][c][dm] for s in schools) for dm in DIMS)

rows = sorted(codes, key=lambda c: -total(c))
result = []
for i, c in enumerate(rows, 1):
    a = agg(c)
    notes = ' / '.join(dict.fromkeys(r2[s][c].get('note', '') for s in schools if r2[s][c].get('note')))
    result.append({
        'rank': i, 'code': c, 'name': names[c],
        'board': meta[c]['board_name'], 'pct': round(float(meta[c]['pct']) * 100, 2),
        'signal': meta[c]['signal'], 'float_mv_yi': round(float(meta[c]['float_mv_yi']), 1),
        **{dm: round(a[dm], 2) for dm in DIMS}, 'total': round(total(c), 2),
        'spread': spread(c), 'notes': notes,
        'school_scores': {s: {dm: r2[s][c][dm] for dm in DIMS} for s in schools},
    })
    flag = ' ⚠' if spread(c) > 4 else ''
    print(f"{i:2}. {c} {names[c]:6s} {total(c):.2f}  M{a['M']:.1f} V{a['V']:.1f} S{a['S']:.1f} F{a['F']:.1f} R{a['R']:.1f}  极差{spread(c)}{flag}")

json.dump(result, open('final_scores.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('\nfinal_scores.json 已写（含逐流派分数）')
