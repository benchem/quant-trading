#!/usr/bin/env python3
"""从 final_scores.json 生成 xlsx spec（Top10 计分表 / 全部19只 / 五流派明细 / 引擎统计）"""
import json, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
rows = json.load(open('final_scores.json', encoding='utf-8'))
eng = json.load(open('engine_summary.json', encoding='utf-8'))

LOGIC = {
    '601019.SH': '缩量后放量板封单最实(占成交~8%)；F=7年报实稳；R=4.3：5.75σ极端日但pct60仅+2.18%平台首破获缓刑。本表第一≠买入（袖仓本周作废）',
    '300770.SZ': '量价阶梯全场最顺，年线上多头排列；现金牛分红率98%(F=7)；主线成员非龙头(S=6.3)',
    '603345.SH': '全场最干净基本面(F=8.3最高)：业绩现金流双优；四连阳温和量阶梯、次日统计佳；短板无板块主线(S=4)',
    '600633.SH': 'I64互联网中军，封单第二实(0.25亿)，扣非稳；qfq复算实际年线下12%（hfq坏点曾虚报），M按真实位置重估',
    '603368.SH': '教科书缩量回踩→放量突破（qfq复算为真），五家M/V一致高分；医药商业无题材共振(S=4)',
    '000400.SZ': '13.9亿全场最大承接+温和放量(V=8最高)；σ5.94全场最高+炸板+净利-33%，R维对撞后定4',
    '603587.SH': '结构最强(M=8)二连板完美形态；R=2.3全场最低：%B1.44最超买+封单衰减烂板（风控派坚持无人驳）。结构最好与最不能买是同一只票',
    '688192.SH': 'AZ合作6亿美元首付款真实到账(F=6，-U标签过时)；技术位全场最低=回撤空间最小(R=6)',
    '300788.SZ': '主线龙头(S=8)但R1第2→R2第9：封单0.042亿极弱+大阴线后20cm V反非真突破(历史4选3败)+烂板次日负期望。对抗赛最有价值纠错案例',
    '600020.SH': '6.8倍量脉冲首站年线(M=7)；板块孤军(S=3)、封单极弱，选举周孤立板先补跌被点名',
}
SHORT = {
    '601019.SH': '缩量放量板封单实/σ5.75缓刑',
    '300770.SZ': '量价阶梯最顺/现金牛',
    '603345.SH': '基本面最干净/无主线',
    '600633.SH': '互联网中军/封单第二实',
    '603368.SH': '教科书回踩突破/无板块',
    '000400.SZ': '最大承接13.9亿/σ5.94炸板',
    '603587.SH': '结构最强M=8/最超买R=2.3',
    '688192.SH': 'AZ 6亿美元到账/技术位最低',
    '300788.SZ': '龙头S=8/封单极弱V反',
    '600020.SH': '6.8倍量首站年线/孤军',
}

HDR = {'bold': True, 'fill': 'DDEBF7', 'align': 'center', 'border': 'thin'}
def h(t): return {'value': t, **HDR}

def stock_sheet(data, name, with_logic):
    header = ['排名','代码','名称','行业','信号','当日涨幅%','流通市值(亿)','M动量','V量能','S板块','F基本面','R风控','总分','分歧极差']
    if with_logic: header.append('核心逻辑')
    out = [[h(x) for x in header]]
    for r in data:
        line = [
            r['rank'], r['code'], r['name'], r['board'], r['signal'],
            {'value': r['pct'], 'format': '+0.00;-0.00'},
            {'value': r['float_mv_yi'], 'format': '0.0'},
            {'value': r['M'], 'format': '0.0'}, {'value': r['V'], 'format': '0.0'},
            {'value': r['S'], 'format': '0.0'}, {'value': r['F'], 'format': '0.0'},
            {'value': r['R'], 'format': '0.0'},
            {'value': r['total'], 'format': '0.00', 'bold': True},
            r['spread'],
        ]
        if with_logic:
            line.append({'value': SHORT.get(r['code'], r['notes'][:40]), 'wrap': True})
        out.append(line)
    last_col = chr(ord('A') + len(header) - 1)
    return {
        'name': name, 'rows': out, 'freeze_panes': 'A2',
        'autofilter': f'A1:{last_col}{len(out)}',
        'column_widths': {'A': 5, 'B': 11, 'C': 11, 'D': 14, 'E': 13, 'F': 10, 'G': 11,
                          'H': 7, 'I': 7, 'J': 7, 'K': 8, 'L': 7, 'M': 7, 'N': 8, 'O': 38},
    }

# 五流派明细（长表）
det = [[h(x) for x in ['代码','名称','流派','M','V','S','F','R','该派加权分']]]
SCHOOLS = ['动量技术','量价资金','板块博弈','基本面','风控保守']
KEY = {'momentum': 0, 'volume': 1, 'sector': 2, 'fundamental': 3, 'risk': 4}
W = {'M': .25, 'V': .20, 'S': .20, 'F': .20, 'R': .15}
for r in rows:
    for sk, label in KEY.items():
        ss = r['school_scores'][sk]
        tot = sum(W[d] * ss[d] for d in W)
        det.append([r['code'], r['name'], SCHOOLS[label],
                    ss['M'], ss['V'], ss['S'], ss['F'], ss['R'],
                    {'value': round(tot, 2), 'format': '0.00'}])

# 引擎统计
kv = [[h('指标'), h('数值')]]
for k, v in [
    ('推演轮次', '200 个交易日（≈2025-12下旬 → 2026-11-02）'),
    ('总信号笔数', eng.get('total_picks')), ('可成交笔数', eng.get('tradeable')),
    ('胜率', f"{eng.get('win_rate', 0)*100:.2f}%"), ('平均单笔收益', f"{eng.get('avg_ret', 0)*100:+.3f}%"),
    ('T日上涨占比', f"{eng.get('t_up_rate', 0)*100:.1f}%"),
    ('止盈率(+6%)', f"{eng.get('tp_rate', 0)*100:.1f}%"), ('止损率(-4%)', f"{eng.get('stop_rate', 0)*100:.1f}%"),
    ('停牌无法成交', eng.get('untradeable_suspended')), ('一字板无法成交', eng.get('untradeable_limit')),
    ('11-02 当日过筛', '19 只（Top30为上限）'),
    ('交叉验证', '仪表盘模拟交易模块20日独立回测：胜率46.2%/平均+0.13%'),
    ('结论', '裸策略≈零边际；对抗层价值=抬升选股质量；hfq坏点修复后需重跑'),
]:
    kv.append([k, v])

spec = {'sheets': [
    stock_sheet(rows[:10], 'Top10计分表', True),
    stock_sheet(rows, '全部19只终审', False),
    {'name': '五流派明细', 'rows': det, 'freeze_panes': 'A2',
     'autofilter': f'A1:I{len(det)}',
     'column_widths': {'A': 11, 'B': 11, 'C': 10, 'D': 6, 'E': 6, 'F': 6, 'G': 6, 'H': 6, 'I': 10}},
    {'name': '200轮引擎统计', 'rows': kv, 'column_widths': {'A': 22, 'B': 52}},
]}
json.dump(spec, open('xlsx_spec.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('spec 写好：Top10计分表 / 全部19只终审 / 五流派明细(95行) / 200轮引擎统计')
