#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
engine200.py — 超短动量 walk-forward 推演引擎（向量化）

用法:
    python engine200.py --rounds 200 --topn 10 --asof 2026-11-02

口径铁律（与既有超短扫描一致）:
1. pct 自算 = close/prev_close - 1（禁用 pct_chg 列）
2. hfq 后复权：跨股绝对价不可比；同股内均线/比值有效
3. amount 质量差 → amount_est = volume*100*close*latest_price/close_latest
4. 量比 = 当日 volume / 前5日 volume 均值；MA250/60/20 在 hfq close 上
5. 交易日历 = 数据集 date 去重升序；N 轮 = 截至并含 asof 的最后 N 个交易日

选股（信号日 D 收盘后）:
 非 ST/*ST/退市、D 有 bar、流通市值 30~300 亿、D 成交额(估) >= 2 亿、
 pct >= 5%、量比 >= 2、收盘位置 >= 0.70 且 (high-low)/prev_close >= 3%、
 结构突破(8a: D-1收<=MA250 且 D收>MA250; 8b: D收=近60日收盘最高)、
 近20日累计涨幅 <= +30%、板块(行业)平均自算 pct >= +1.5%（无行业不剔除）
 得分 = 量比*收盘位置*板块平均涨幅, TopN

执行（T 日）: 开盘买入；low<=买价*0.96 止损@0.96；high>=买价*1.06 止盈@1.06
（同日止损优先）；否则收盘卖出。T 无 bar=停牌；open=high=low=一字。均不计胜率。

前视偏差控制: 所有因子窗口仅用 <= 信号日 数据；T 日 OHLC 只出现在执行模型。
"""
import argparse
import json
import os
import time

import numpy as np
import pandas as pd

DATA_DIR = '/opt/quant-trading/data'
DAILY = os.path.join(DATA_DIR, 'dataset/daily_hfq.parquet')
BASIC = os.path.join(DATA_DIR, 'dataset/stock_basic.parquet')
BOARD = os.path.join(DATA_DIR, 'board_members.parquet')


def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


def load_all():
    log('读取 daily_hfq.parquet ...')
    df = pd.read_parquet(DAILY,
                         columns=['date', 'code', 'name', 'open', 'high', 'low',
                                  'close', 'volume', 'amount'])
    basic = pd.read_parquet(BASIC)
    board = pd.read_parquet(BOARD)
    board = board[board['board_type'] == 'industry'][['code', 'board_code', 'board_name']]
    return df, basic, board


def build_factors(df, basic, board):
    """全量向量化计算所有因子（按 code 分组，滚动窗口只用历史数据）。"""
    df = df.sort_values(['code', 'date'], kind='mergesort').reset_index(drop=True)
    g = df.groupby('code', sort=False)

    log('计算 prev_close / pct / 收盘位置 / 振幅...')
    df['prev_close'] = g['close'].shift(1)
    df['pct'] = df['close'] / df['prev_close'] - 1.0
    rng = df['high'] - df['low']
    df['close_pos'] = np.where(rng > 0, (df['close'] - df['low']) / rng, 1.0)
    df['ampl_prev'] = rng / df['prev_close']  # (high-low)/prev_close

    log('计算 MA20/60/250 与 20日累计涨幅...')
    for w in (20, 60, 250):
        df[f'ma{w}'] = g['close'].transform(lambda s: s.rolling(w, min_periods=w).mean())
    df['pct20'] = df['close'] / g['close'].shift(20) - 1.0

    log('计算量比（前5日均量）...')
    vol5_prev = g['volume'].transform(lambda s: s.rolling(5, min_periods=5).mean().shift(1))
    df['vol_ratio'] = df['volume'] / vol5_prev

    log('计算 8a（上穿 MA250）与 8b（60日收盘新高）...')
    prev_close_series = g['close'].shift(1)
    ma250_prev = g['ma250'].shift(1)
    df['break_8a'] = (prev_close_series <= ma250_prev) & (df['close'] > df['ma250'])
    # 60日收盘最高（含当日），且当日为严格新高 vs 前59日最大值
    roll60_max_prev = g['close'].transform(lambda s: s.rolling(60, min_periods=1).max().shift(1))
    df['is_60d_high'] = df['close'] >= roll60_max_prev
    df['break_8b'] = df['is_60d_high']

    log('估算成交额 amount_est...')
    latest = basic[['code', 'latest_price']].drop_duplicates('code')
    df = df.merge(latest, on='code', how='left')
    # 每只股票最后一个 close（锚定换算）
    last_close = df.groupby('code')['close'].transform('last').astype(float)
    df['amount_est'] = df['volume'] * 100.0 * df['close'] * (
        df['latest_price'].astype(float) / last_close)

    log('合并流通市值与行业...')
    df = df.merge(basic[['code', 'float_mv']], on='code', how='left')
    df = df.merge(board, on='code', how='left')

    # 板块平均自算 pct（行业内、当日有数的股票平均）
    log('计算板块当日平均 pct...')
    df['board_avg_pct'] = df.groupby(['board_code', 'date'])['pct'].transform('mean')

    log('因子计算完成。')
    return df


def select_day(day_df, topn=10):
    """对单个交易日的切片应用策略，返回带得分的 picks。"""
    d = day_df
    m = (
        ~d['name'].str.contains('ST|退', na=False)
        & d['pct'].notna()
        & d['float_mv'].notna()
        & (d['float_mv'] >= 30e8) & (d['float_mv'] <= 300e8)
        & (d['amount_est'] >= 2e8)
        & (d['pct'] >= 0.05)
        & (d['vol_ratio'] >= 2.0)
        & (d['close_pos'] >= 0.70)
        & (d['ampl_prev'] >= 0.03)
        & (d['break_8a'] | d['break_8b'])
        & (d['pct20'].notna()) & (d['pct20'] <= 0.30)
    )
    # 板块效应：有行业的才检查
    has_board = d['board_avg_pct'].notna()
    m = m & (~has_board | (d['board_avg_pct'] >= 0.015))
    picks = d[m].copy()
    if picks.empty:
        picks['score'] = pd.Series(dtype=float)
        picks['signal'] = pd.Series(dtype=object)
        return picks
    picks['score'] = picks['vol_ratio'] * picks['close_pos'] * picks['board_avg_pct'].fillna(0)
    picks['signal'] = np.where(picks['break_8b'], '8b', '8a')
    picks = picks.sort_values('score', ascending=False).head(topn)
    return picks


def execute(t_df, picks, buy_col='open'):
    """T 日执行模型。t_df 索引为 code 的当日切片。返回逐 pick 结果 DataFrame。"""
    rows = []
    for _, p in picks.iterrows():
        code = p['code']
        r = t_df.loc[t_df['code'] == code]
        if r.empty:
            rows.append(dict(code=code, ret=np.nan, exit_='untradeable_suspended'))
            continue
        r = r.iloc[0]
        o, h, l, c = r['open'], r['high'], r['low'], r['close']
        if pd.isna(o) or o <= 0:
            rows.append(dict(code=code, ret=np.nan, exit_='untradeable_suspended'))
            continue
        if o == h and o == l:
            rows.append(dict(code=code, ret=np.nan, exit_='untradeable_limit'))
            continue
        stop_px = o * 0.96
        tp_px = o * 1.06
        hit_stop = (not pd.isna(l)) and l <= stop_px
        hit_tp = (not pd.isna(h)) and h >= tp_px
        if hit_stop:
            ret, exit_ = stop_px / o - 1.0, 'stop'
        elif hit_tp:
            ret, exit_ = tp_px / o - 1.0, 'tp'
        else:
            ret, exit_ = c / o - 1.0, 'close'
        rows.append(dict(code=code, ret=ret, exit_=exit_,
                         sig_close=float(p['close']), t_close=float(c)))
    if not rows:
        return pd.DataFrame(columns=['code', 'ret', 'exit_'])
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=200)
    ap.add_argument('--topn', type=int, default=10)
    ap.add_argument('--asof', type=str, default='2026-11-02')
    args = ap.parse_args()
    asof = pd.Timestamp(args.asof)

    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          f'{asof.strftime("%Y%m%d")}_deep200')
    os.makedirs(outdir, exist_ok=True)

    df, basic, board = load_all()
    df = build_factors(df, basic, board)

    cal = np.sort(df['date'].unique())
    cal = cal[cal <= asof]
    assert len(cal) >= args.rounds + 1, '交易日不足'
    rounds_cal = cal[-args.rounds:]
    log(f'{len(cal)} 个交易日 <= asof；推演 {args.rounds} 轮：{pd.Timestamp(rounds_cal[0]).date()} -> {pd.Timestamp(rounds_cal[-1]).date()}')

    # 按日期建立切片索引，避免每轮全表扫描
    dates_index = {d: sub for d, sub in df.groupby('date', sort=False)}

    all_round_rows = []
    all_pick_rows = []
    for t_date in rounds_cal:
        i = np.searchsorted(cal, t_date)
        sig_date = cal[i - 1]
        sig_df = dates_index.get(sig_date)
        if sig_df is None or sig_df.empty:
            continue
        picks = select_day(sig_df, topn=args.topn)
        t_df = dates_index.get(t_date, pd.DataFrame())
        ex = execute(t_df, picks)
        detail = picks[['code', 'name', 'pct', 'vol_ratio', 'close_pos', 'pct20',
                        'float_mv', 'amount_est', 'signal', 'board_avg_pct', 'score',
                        'board_name']].reset_index(drop=True)
        detail['signal_date'] = sig_date
        detail['t_date'] = t_date
        detail = detail.merge(ex, on='code', how='left')
        all_pick_rows.append(detail)

        trad = ex[ex['ret'].notna()]
        wr = (trad['ret'] > 0).mean() if len(trad) else np.nan
        all_round_rows.append(dict(
            t_date=t_date, signal_date=sig_date, n_picks=len(picks),
            n_tradeable=len(trad), win_rate=wr,
            avg_ret=trad['ret'].mean() if len(trad) else np.nan))
    picks_all = pd.concat(all_pick_rows, ignore_index=True) if all_pick_rows else pd.DataFrame()
    rounds_df = pd.DataFrame(all_round_rows)

    # ---- engine_summary.json ----
    trad = picks_all[picks_all['ret'].notna()]
    n_stop = int((trad['exit_'] == 'stop').sum())
    n_tp = int((trad['exit_'] == 'tp').sum())
    n_close = int((trad['exit_'] == 'close').sum())
    n_susp = int((picks_all['exit_'] == 'untradeable_suspended').sum())
    n_limit = int((picks_all['exit_'] == 'untradeable_limit').sum())
    rounds_df['month'] = pd.to_datetime(rounds_df['t_date']).dt.strftime('%Y-%m')
    monthly = rounds_df.groupby('month').apply(
        lambda g: (g['win_rate'] * g['n_tradeable']).sum() / g['n_tradeable'].sum()
        if g['n_tradeable'].sum() > 0 else np.nan)
    best_i = rounds_df['avg_ret'].idxmax()
    worst_i = rounds_df['avg_ret'].idxmin()
    summary = dict(
        rounds=int(len(rounds_df)),
        total_picks=int(len(picks_all)),
        tradeable=int(len(trad)),
        win_rate=float((trad['ret'] > 0).mean()),
        avg_ret=float(trad['ret'].mean()),
        t_up_rate=float((trad['t_close'] / trad['sig_close'] - 1 > 0).mean()),
        tp_rate=float(n_tp / len(trad)) if len(trad) else None,
        stop_rate=float(n_stop / len(trad)) if len(trad) else None,
        close_exit_rate=float(n_close / len(trad)) if len(trad) else None,
        untradeable_suspended=n_susp,
        untradeable_limit=n_limit,
        best_round=dict(t_date=str(rounds_df.loc[best_i, 't_date']),
                        avg_ret=float(rounds_df.loc[best_i, 'avg_ret'])),
        worst_round=dict(t_date=str(rounds_df.loc[worst_i, 't_date']),
                         avg_ret=float(rounds_df.loc[worst_i, 'avg_ret'])),
        monthly_win_rate={k: (None if pd.isna(v) else round(float(v), 4))
                          for k, v in monthly.items()},
    )
    with open(os.path.join(outdir, 'engine_summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # ---- rounds_detail.csv ----
    rounds_out = rounds_df.copy()
    rounds_out['t_date'] = pd.to_datetime(rounds_out['t_date']).dt.strftime('%Y-%m-%d')
    rounds_out['signal_date'] = pd.to_datetime(rounds_out['signal_date']).dt.strftime('%Y-%m-%d')
    rounds_out.to_csv(os.path.join(outdir, 'rounds_detail.csv'), index=False)

    # ---- per_stock_stats.csv ----
    def per_stock(g):
        t = g[g['ret'].notna()]
        return pd.Series(dict(
            name=g['name'].iloc[0],
            times_picked=len(g),
            avg_ret_when_picked=float(t['ret'].mean()) if len(t) else 0.0,
            win_rate_when_picked=float((t['ret'] > 0).mean()) if len(t) else 0.0,
            last_picked_date=pd.to_datetime(g['t_date']).max().strftime('%Y-%m-%d'),
        ))
    per_stock_df = picks_all.groupby('code').apply(per_stock).reset_index()
    per_stock_df.to_csv(os.path.join(outdir, 'per_stock_stats.csv'), index=False)

    # ---- candidates_top30.csv（asof 日收盘后选股，供下一交易日买入）----
    asof_df = dates_index.get(asof)
    cand = select_day(asof_df, topn=30).copy()
    cand['rank'] = range(1, len(cand) + 1)
    cand['float_mv_yi'] = cand['float_mv'] / 1e8
    cand['amount_est_yi'] = cand['amount_est'] / 1e8
    # room60: 60日新高上方剩余空间 = (60日最高价 - close)/close —— 此处以收盘新高口径给 0 上方空间参考
    cand = cand.merge(per_stock_df[['code', 'times_picked', 'avg_ret_when_picked',
                                    'win_rate_when_picked', 'last_picked_date']],
                      on='code', how='left')
    for c in ('times_picked', 'avg_ret_when_picked', 'win_rate_when_picked'):
        cand[c] = cand[c].fillna(0)
    cand['last_picked_date'] = cand['last_picked_date'].fillna('')
    out_cols = ['code', 'name', 'board_name', 'pct', 'vol_ratio', 'close_pos',
                'pct20', 'float_mv_yi', 'amount_est_yi', 'signal',
                'board_avg_pct', 'score', 'rank',
                'times_picked', 'avg_ret_when_picked', 'win_rate_when_picked',
                'last_picked_date']
    # room60: 8b 免检空间要求 — 用 (近60日最高high - close)/close 估计上方套牢空间为 0，此处输出
    # 以 60 日收盘新高距 60 日最高价的下方距离作为 room60（负值=收盘尚在60日最高价下方）
    g60 = df[df['date'] <= asof].groupby('code')['close']
    # 高成本滚动，仅对 30 只候选计算
    rooms = {}
    for code in cand['code']:
        s = df.loc[df['code'] == code, ['date', 'close', 'high']]
        s = s[s['date'] <= asof].tail(60)
        if len(s):
            rooms[code] = (s['close'].iloc[-1] / s['high'].max()) - 1.0
    cand['room60'] = cand['code'].map(rooms)
    out_cols.insert(6, 'room60')
    cand[out_cols].to_csv(os.path.join(outdir, 'candidates_top30.csv'), index=False)

    log(f'win_rate={summary["win_rate"]:.4f} total_picks={summary["total_picks"]} '
        f'avg_ret={summary["avg_ret"]:.5f}')
    log(f'产物目录: {outdir}')
    print('ENGINE200_DONE')


if __name__ == '__main__':
    main()
