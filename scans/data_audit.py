#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scans/data_audit.py — 本地日线数据体检/修复验收门

用法:
    python scans/data_audit.py                 # 全市场体检，清单落 stdout
    python scans/data_audit.py --save FILE     # 额外存清单到文件

体检项（近120 bar）:
    1) high < low          脏bar
    2) 单日 |pct| > 25%    脏bar（无除权口径下不可能）
    3) amount = 0 / NaN    脏bar
    4) 连续交易日 amount 跳变 >20x  且日期间隔<=3天（排除停牌复牌）
    5) 末bar 日期 != 交易日
验收标准: dirty=0（或全部可解释为停牌复牌），missing_last=0
"""
import os
import sys
from datetime import datetime

import pandas as pd

DATA = '/opt/quant-trading/data/daily/hfq'


def audit(save=None):
    stats = {'clean': 0, 'dirty': [], 'missing_last': []}
    files = sorted(os.listdir(DATA))
    t0 = datetime.now()
    for i, f in enumerate(files):
        code = f.replace('.parquet', '')
        try:
            df = pd.read_parquet(os.path.join(DATA, f),
                                 columns=['date', 'close', 'high', 'low', 'open', 'amount'])
        except Exception:
            stats['dirty'].append((code, '读取失败'))
            continue
        if len(df) < 130:
            continue
        d = df.tail(120).reset_index(drop=True)
        if str(d['date'].iloc[-1])[:10] != '2026-11-04':
            stats['missing_last'].append((code, str(d['date'].iloc[-1])[:10]))
            continue
        issues = []
        if (d['high'] < d['low']).any():
            issues.append('high<low')
        pct = d['close'].pct_change() * 100
        if (abs(pct) > 25).any():
            issues.append('|pct|>25%')
        am = d['amount']
        if ((am <= 0) | am.isna()).any():
            issues.append('amt0')
        jump = d[(am / am.shift(1) > 20) | (am.shift(1) / am > 20)]
        for j in jump.index:  # 停牌复牌豁免：日期间隔>3天视为正常
            gap = (pd.Timestamp(d['date'].iloc[j]) - pd.Timestamp(d['date'].iloc[j - 1])).days
            if gap <= 3:
                issues.append(f'amt跳变@{str(d["date"].iloc[j])[:10]}')
                break
        if issues:
            stats['dirty'].append((code, ','.join(issues)))
        else:
            stats['clean'] += 1
        if (i + 1) % 1000 == 0:
            print(f"  ...{i+1}/{len(files)} {(datetime.now()-t0).total_seconds():.0f}s", file=sys.stderr)
    n = len(files)
    print(f"体检完成 {n} 只：干净 {stats['clean']}｜脏 {len(stats['dirty'])}｜缺末bar {len(stats['missing_last'])}")
    for c, t in stats['dirty'][:20]:
        print(" ", c, t)
    for c, d in stats['missing_last'][:10]:
        print(" ", c, d)
    if save:
        with open(save, 'w') as fh:
            fh.write(f"体检 {datetime.now()}\n干净 {stats['clean']} 脏 {len(stats['dirty'])} "
                     f"缺末bar {len(stats['missing_last'])}\n\n脏清单:\n")
            for c, t in stats['dirty']:
                fh.write(f"{c}\t{t}\n")
            fh.write("\n缺末bar:\n")
            for c, d in stats['missing_last']:
                fh.write(f"{c}\t{d}\n")
        print(f"清单已存 {save}")
    # 退出码：0=干净（过验收），1=有脏
    return 0 if not stats['dirty'] and not stats['missing_last'] else 1


if __name__ == '__main__':
    save = None
    if '--save' in sys.argv:
        save = sys.argv[sys.argv.index('--save') + 1]
    sys.exit(audit(save))
