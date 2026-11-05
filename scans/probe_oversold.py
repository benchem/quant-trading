# B 路线第二腿：超跌回归探针（口径先验：20日跌>=20% + 当日收盘位置>=0.5，非ST，30~300亿）
import sys, glob, os
sys.path.insert(0, "/root/workspaces/quant-trading/scans")
import pandas as pd
from scan_prebreak import load_basic, COST

files = sorted(glob.glob("/opt/quant-trading/data/daily/hfq/*.parquet"))
universe = load_basic()
dates = pd.concat([pd.read_parquet(f, columns=["date"])["date"] for f in files[:300]])
trade_dates = sorted(set(pd.to_datetime(dates)))
weekly, seen = [], None
for dt in reversed(trade_dates):
    wk = dt.isocalendar()[:2]
    if wk != seen:
        weekly.append(dt); seen = wk
weekly = sorted(weekly)[-52:]

sig_recs, mkt_recs = [], []
for f in files:
    code = os.path.basename(f).replace(".parquet","")
    if code not in universe: continue
    try: df = pd.read_parquet(f, columns=["date","close","high","low"])
    except Exception: continue
    df["date"]=pd.to_datetime(df["date"]); df=df.sort_values("date").reset_index(drop=True)
    c=df["close"].values; h=df["high"].values; l=df["low"].values
    dmap = {d:i for i,d in enumerate(df["date"])}
    for sd in weekly:
        i = dmap.get(sd)
        if i is None or i<60: continue
        # 市场基线样本
        if c[i]>0 and i+10 < len(c):
            mkt_recs.append(dict(t5=c[i+5]/c[i]-1-COST if i+5<len(c) else None,
                                 t10=c[i+10]/c[i]-1-COST))
        # 超跌信号
        ret20 = c[i]/c[i-20]-1
        rngi = h[i]-l[i]
        cpos = (c[i]-l[i])/rngi if rngi>0 else 0
        if ret20 <= -0.20 and cpos >= 0.5:
            if i+10 < len(c):
                sig_recs.append(dict(t5=c[i+5]/c[i]-1-COST if i+5<len(c) else None,
                                     t10=c[i+10]/c[i]-1-COST, code=code, wk=str(sd.date())))
for label, recs in [("超跌回归信号", sig_recs), ("市场基线(等权全universe)", mkt_recs)]:
    d = pd.DataFrame(recs)
    print(f"—— {label}: N={len(d)} ——")
    for col, tag in [("t5","T+5"), ("t10","T+10")]:
        s = d[col].dropna()
        print(f"  {tag}: 中位 {s.median():+.2%} 均值 {s.mean():+.2%} 胜率 {(s>0).mean():.1%} "
              f"[Q25 {s.quantile(.25):+.1%}~Q75 {s.quantile(.75):+.1%}] 最差 {s.min():+.1%}")
if sig_recs:
    pd.DataFrame(sig_recs).to_csv("/root/workspaces/quant-trading/oversold_probe_detail.csv", index=False)
    print("明细: oversold_probe_detail.csv")
