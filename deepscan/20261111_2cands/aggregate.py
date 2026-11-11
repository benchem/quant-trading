#!/usr/bin/env python
"""deepscan 2026-11-11 两股超短专项：聚合各流派 JSON 计分 → 加权总分 + 冲突点检测
用法: python aggregate.py <wave>   # wave = R1|R2|R3
读 agents/<wave>-*.md 末尾 JSON 块，写 scores_<wave>.json + 打印榜单与冲突点
"""
import sys, os, json, re, glob
from collections import defaultdict

W = {'M':0.25, 'V':0.20, 'S':0.20, 'F':0.20, 'R':0.15}
CODES = ['300310.SZ', '002425.SZ']
NAMES = {'300310.SZ':'宜通世纪', '002425.SZ':'凯撒文化'}

def parse_file(path):
    txt = open(path, encoding='utf-8').read()
    blocks = re.findall(r'```json\s*(\{.*?\})\s*```', txt, re.S)
    cands = blocks + re.findall(r'(\{"school".*?\})\s*$', txt, re.S)
    for c in cands:
        try:
            j = json.loads(c)
            if 'scores' in j: return j
        except Exception: pass
    m = re.search(r'\{"school".*\}', txt, re.S)
    if m:
        for end in range(len(m.group(0)), 20, -1):
            try:
                j = json.loads(m.group(0)[:end])
                if 'scores' in j: return j
            except Exception: continue
    return None

def main(wave):
    files = sorted(glob.glob(f'agents/{wave}-*.md'))
    assert files, f'no files for {wave}'
    per_school, parsed = defaultdict(dict), {}
    for f in files:
        school = os.path.basename(f)[len(wave)+1:-3]
        j = parse_file(f)
        assert j, f'JSON 解析失败: {f}'
        parsed[school] = j
        for s in j['scores']:
            m = re.match(r'(\d{6}\.(?:SH|SZ))', s['code'].strip())
            assert m, f"{f}: 非法 code {s['code']!r}"
            per_school[m.group(1)][school] = {k: float(s[k]) for k in 'MVSFR'}
    # 掉桶断言: 每只股票收到的流派家数必须等于派单家数
    n_schools = len(parsed)
    for code in CODES:
        got = len(per_school[code])
        assert got == n_schools, f'掉桶: {code} 只收到 {got}/{n_schools} 家'
    result, clashes = {}, []
    for code in CODES:
        dim_scores, dim_spread = {}, {}
        for dim in 'MVSFR':
            vals = sorted(per_school[code][sc][dim] for sc in per_school[code])
            core = vals[1:-1] if len(vals) >= 3 else vals
            dim_scores[dim] = sum(core)/len(core)
            dim_spread[dim] = (max(vals)-min(vals), vals)
            if max(vals)-min(vals) >= 4:
                clashes.append(f"{NAMES[code]} {dim}维极差{max(vals)-min(vals):.0f}分 {vals}")
        total = sum(dim_scores[d]*W[d] for d in W)
        result[code] = {'name': NAMES[code], 'dims': {d: round(dim_scores[d],2) for d in W},
                        'total': round(total,3),
                        'by_school': {sc: per_school[code][sc] for sc in per_school[code]}}
    rank = sorted(result.items(), key=lambda kv: -kv[1]['total'])
    out = {'wave': wave, 'schools': list(parsed), 'result': {k: v for k, v in rank}, 'clashes': clashes}
    json.dump(out, open(f'scores_{wave}.json','w'), ensure_ascii=False, indent=1)
    print(f"══ {wave} 榜单（{len(parsed)} 家：{', '.join(parsed)}）══")
    for i,(code, r) in enumerate(rank,1):
        print(f"{i}. {NAMES[code]} {r['total']:.2f} | " + ' '.join(f"{d}{r['dims'][d]:.1f}" for d in 'MVSFR'))
    if clashes:
        print("\n── 冲突点（极差≥4分，R2 对抗重点）──")
        for c in clashes: print(" *", c)
    return out

if __name__ == '__main__':
    main(sys.argv[1])
