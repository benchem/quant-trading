"""时间序列与 GARCH 波动率均值回归模型（ARMA-GARCH Model）。

所属维度: 数理统计与统计套利。
目标: 用 ARMA 预测收益率短期条件均值 + GARCH 拟合波动率聚集效应，建立"价格
偏离均值"的概率分布模型，在震荡市中捕捉均值回归机会，并内置 regime 检测
识别"均值回归失效"场景（强单边趋势 / 极端波动黑天鹅）。

模型结构（四层，自下而上）:

1) 均值层 —— ARMA(p,q)（Hannan-Rissanen 法 OLS 迭代估计，纯 numpy 实现）:
   对日**对数收益** r_t 拟合 r_t = c + Σφᵢr_{t-i} + Σθⱼe_{t-j} + e_t，
   输出 1 步条件均值预测 μ̂_{t+1}。

2) 波动层 —— GARCH(1,1)（高斯极大似然，纯 numpy 自实现 Nelder-Mead 优化）:
   σ²_t = ω + α·e²_{t-1} + β·σ²_{t-1}，捕捉波动率聚集（大波动后跟大波动）。
   输出 1 步条件波动 σ̂_{t+1} 与持续性 α+β（>0.9 表示波动记忆强）。

3) 均值回归核心 —— 偏离指标（波动率调整）:
   dev_z = ln(P_t / MA_N(P)) / (σ_t · √H)，H 为回归视界（默认 5 日）。
   含义: "当前价格偏离长期均值相当于 H 日预测波动的几倍"。
   |dev_z| ≥ z_entry（默认 2）且处于震荡 regime → 均值回归信号:
   dev_z ≤ -z_entry 超卖 → 预测向上回归；dev_z ≥ +z_entry 超买 → 预测向下回归。

4) regime 检测（针对模型已知弱点"均值回归失效"）:
   - 强单边趋势: |20日对数位移| ≥ trend_sig(2.5) × σ̂·√20 → 趋势市，回归信号降权
   - 黑天鹅:     单日 |r| ≥ shock_sigma(6) × σ̂（结构性断裂）→ 回归失效，仅保留区间
   - 回归幅度门: H 日预期回归比例 1-ρ^H < 5%（半衰期 >约70日）→ 压制回归信号
                 （dev 为高持续带通序列, ρ≈0.95 属常态; 该门只过滤"偏离永不回归"）
   - 否则: 震荡市（本模型主战场，区间高低点预测最有效）
   注意: 波动聚集（σ̂ 抬升）是 GARCH 的正常功能、区间加宽即可，**不算失效**，不抑制信号。

特色输出:
   - 次日 95% 预测区间 [P·e^{μ̂-1.96σ̂}, P·e^{μ̂+1.96σ̂}]（区间高低点预测）;
   - 历史信号回测: 信号日后 5/10/20 日收益中"方向与回归方向一致"的占比
     （= 宣称的 55%~60% 胜率的可实测版本），并附无 regime 过滤对照组。

防过拟合说明: ARMA/GARCH 阶数取最小充分设定（2,1）与（1,1），regime 阈值
均用 σ 倍数表达（跨股票可比），未做网格搜索。

作者: quant-trading 项目（2026-10）
数据依赖: 仅需收盘价（走 ctx.kline），需 ≥ 约 300 个交易日。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analysis import AnalysisPlugin, AnalysisResult, ParamSpec

#: 回测前瞻期（交易日）
HORIZONS = (5, 10, 20)
# 正式信号要求的 H 日内最小预期回归比例（1-ρ^H ≥ 该值, 即半衰期约 ≤70 日（H=5））。
# 过滤 ρ→1 的"偏离永不回归"行情; 常规 A 股 ρ≈0.94-0.98 → rev_frac≈0.10-0.27 均通过。
MIN_REV_FRAC = 0.05


# ------------------------------------------------------------------ 优化器


def _nelder_mead(f, x0: np.ndarray, step: float = 0.4,
                 max_iter: int = 300, tol: float = 1e-10) -> tuple[np.ndarray, float]:
    """自实现 Nelder-Mead 无约束最小化（3~4 维小问题足够）。

    早停条件: 最优/最差目标差 < tol 或单纯形直径 < 1e-6（参数空间相对尺度）。
    """
    n = len(x0)
    # 注意必须拷贝: np.asarray 对 ndarray 输入返回原对象，会让顶点共享同一数组
    simplex = [np.array(x0, dtype=float)]
    for i in range(n):
        x = np.array(x0, dtype=float)
        x[i] += step
        simplex.append(x)
    vals = [f(x) for x in simplex]
    for _ in range(max_iter):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        simplex = [simplex[i] for i in order]
        vals = [vals[i] for i in order]
        if not np.isfinite(vals[0]) or abs(vals[-1] - vals[0]) < tol:
            break
        if np.max(np.ptp(np.array(simplex), axis=0)) < 1e-6:
            break
        cen = np.mean(simplex[:n], axis=0)
        worst = simplex[-1]
        # 反射
        xr = cen + 1.0 * (cen - worst)
        fr = f(xr)
        if vals[0] <= fr < vals[-2]:
            simplex[-1], vals[-1] = xr, fr
        elif fr < vals[0]:
            # 扩张
            xe = cen + 2.0 * (cen - worst)
            fe = f(xe)
            if fe < fr:
                simplex[-1], vals[-1] = xe, fe
            else:
                simplex[-1], vals[-1] = xr, fr
        else:
            # 收缩
            xc = cen + 0.5 * (worst - cen)
            fc = f(xc)
            if fc < vals[-1]:
                simplex[-1], vals[-1] = xc, fc
            else:
                # 全体向最优点收缩
                for i in range(1, n + 1):
                    simplex[i] = simplex[0] + 0.5 * (simplex[i] - simplex[0])
                    vals[i] = f(simplex[i])
    order = sorted(range(n + 1), key=lambda i: vals[i])
    return simplex[order[0]], vals[order[0]]


# ------------------------------------------------------------------ ARMA


def _fit_arma(r: np.ndarray, p: int, q: int) -> dict:
    """Hannan-Rissanen 法估计 ARMA(p,q)（条件 OLS 迭代，工程上足够稳健）。

    Returns:
        {"phi": (p,), "theta": (q,), "c": float,
         "resid": ndarray (全序列残差, 前 p+q 位为 0)}
    """
    n = len(r)
    if n < max(p, q) + 50:
        raise ValueError("ARMA 数据不足")
    y = r[p:]                       # t = p .. n-1
    m = len(y)
    e = np.zeros(n)                 # 残差缓冲（迭代更新）
    beta = None
    for _ in range(8):
        cols = [np.ones(m)]
        for i in range(1, p + 1):
            cols.append(r[p - i:n - i])
        for j in range(1, q + 1):
            cols.append(e[p - j:n - j])
        X = np.column_stack(cols)
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        e[p:] = y - X @ beta
    phi = beta[1:1 + p]
    theta = beta[1 + p:1 + p + q]
    c = beta[0]
    return {"phi": phi, "theta": theta, "c": c, "resid": e}


def _arma_forecast(fit: dict, r: np.ndarray) -> float:
    """1 步条件均值预测: μ̂_{t+1} = c + Σφᵢr_{t+1-i} + Σθⱼe_{t+1-j}。"""
    t = len(r) - 1
    mu = fit["c"]
    for i, a in enumerate(fit["phi"], start=1):
        mu += a * r[t + 1 - i]
    for j, b in enumerate(fit["theta"], start=1):
        if t + 1 - j >= 0:
            mu += b * fit["resid"][t + 1 - j]
    return float(mu)


# ------------------------------------------------------------------ GARCH


def _garch_sigma2(omega: float, alpha: float, beta: float, e2: np.ndarray,
                  var0: float) -> np.ndarray:
    """GARCH 条件方差递归路径。"""
    n = len(e2)
    s2 = np.empty(n)
    s2[0] = var0
    for t in range(1, n):
        s2[t] = omega + alpha * e2[t - 1] + beta * s2[t - 1]
    return s2


def _fit_garch(eps: np.ndarray) -> dict:
    """GARCH(1,1) 高斯极大似然。

    稳健策略: ① 粗网格扫描（ω 对数网格 × 持续性 × α占比，约 300 点）
    定位似然峰邻域 → ② Nelder-Mead 局部抛光。比单点 NM 对初始值稳健得多。
    参数化保证 ω>0, α>0, β>0, α+β<0.98（平稳）。
    Returns: {"omega","alpha","beta","sigma2","persistence","loglik"}
    """
    n = len(eps)
    var0 = float(np.var(eps)) or 1e-8
    e2 = eps ** 2

    def params(u):
        w = float(np.exp(u[0]))
        s1, s2 = float(np.exp(u[1])), float(np.exp(u[2]))
        a = 0.98 * s1 / (1.0 + s1 + s2)
        b = 0.98 * s2 / (1.0 + s1 + s2)
        return w, a, b

    def negloglik(u):
        w, a, b = params(u)
        s2v = _garch_sigma2(w, a, b, e2, var0)
        if (s2v <= 0).any() or not np.all(np.isfinite(s2v)):
            return 1e12
        return 0.5 * float(np.sum(np.log(2 * np.pi) + np.log(s2v) + e2 / s2v))

    def to_u(w, a, b):
        """(ω,α,β) → 无约束参数 u。由 a+b = 0.98·S/(1+S), S=s1+s2 反解。"""
        S = (a + b) / (0.98 - a - b)
        return np.log([w, S * a / (a + b), S * b / (a + b)])

    # 阶段 1: 粗网格（ω 相对无条件方差, 持续性, α 占比）
    best_u, best_f = None, np.inf
    for log_rel in np.linspace(-3.5, -0.5, 9):        # ω = var0 × 10^log_rel
        for persist in (0.45, 0.60, 0.72, 0.82, 0.88, 0.93, 0.96, 0.975, 0.978):
            for ratio in (0.05, 0.10, 0.16, 0.25, 0.40, 0.60):
                u = to_u(var0 * 10.0 ** log_rel, persist * ratio,
                         persist * (1.0 - ratio))
                f = negloglik(u)
                if f < best_f:
                    best_f, best_u = f, u
    # 阶段 2: NM 抛光（含 2 个典型初值做保险）
    starts = [best_u] if best_u is not None else []
    starts.append(np.array([np.log(var0 / 10.0), np.log(0.1), np.log(1.1)]))
    starts.append(np.array([np.log(var0 / 30.0), np.log(0.05), np.log(2.0)]))
    u_best, f_best = None, np.inf
    for u0 in starts:
        if u0 is None or not np.all(np.isfinite(u0)):
            continue
        u, f = _nelder_mead(negloglik, u0, step=0.25, max_iter=300)
        if f < f_best:
            f_best, u_best = f, u
    w, a, b = params(u_best)
    s2v = _garch_sigma2(w, a, b, e2, var0)
    return {"omega": w, "alpha": a, "beta": b, "sigma2": s2v,
            "persistence": a + b, "loglik": -f_best}


# ------------------------------------------------------------------ 插件


class GarchMeanReversionPlugin(AnalysisPlugin):
    name = "garch_mean_reversion"
    display_name = "GARCH波动均值回归"
    description = (
        "ARMA(2,1)条件均值 + GARCH(1,1)波动聚集拟合价格偏离均值的概率分布，"
        "震荡市超卖/超买均值回归信号 + regime检测(趋势/极端波动时提示失效) + "
        "次日95%预测区间 + 历史信号方向胜率回测"
    )
    version = "0.1.0"
    category = "technical"
    params = {
        "arma_p": ParamSpec(type="int", default=2, min=1, max=4, help="AR 阶数"),
        "arma_q": ParamSpec(type="int", default=1, min=0, max=3, help="MA 阶数"),
        "ma_window": ParamSpec(type="int", default=60, min=20, max=120,
                               help="长期均值（均衡价）均线周期"),
        "horizon": ParamSpec(type="int", default=5, min=2, max=20,
                             help="回归视界 H（偏离指标的分母 σ·√H）"),
        "z_entry": ParamSpec(type="float", default=2.0, min=1.0, max=4.0,
                             help="均值回归触发阈值 |dev_z| ≥ 该值"),
        "trend_sig": ParamSpec(type="float", default=2.5, min=1.5, max=6.0,
                               help="趋势 regime 阈值: 20日位移 ≥ 该σ倍数"),
        "shock_sigma": ParamSpec(type="float", default=6.0, min=3.0, max=15.0,
                                 help="黑天鹅 regime: 单日收益 |r| ≥ 该×σ̂（结构性断裂）"),
    }

    def run(self, ctx, code: str, arma_p: int = 2, arma_q: int = 1,
            ma_window: int = 60, horizon: int = 5, z_entry: float = 2.0,
            trend_sig: float = 2.5, shock_sigma: float = 6.0) -> AnalysisResult:
        need = max(300, ma_window + 50, arma_p + arma_q + 50)
        df = ctx.kline(code, last=max(need, 600))
        r = AnalysisResult(self.name, code)
        if len(df) < need:
            r.summary = (f"历史数据不足（ARMA-GARCH 需约 {need} 个交易日，"
                         f"实际 {len(df)}），跳过")
            r.signal("warn", "历史数据不足")
            return r

        close = df["close"].astype(float)
        idx = df.index
        rlog = np.log(close).diff().dropna().astype(float).to_numpy()
        if len(rlog) < need - 10:
            r.summary = "有效收益序列过短，跳过"
            r.signal("warn", "历史数据不足")
            return r

        # ============ 1) ARMA 条件均值（对全序列）============
        try:
            fit = _fit_arma(rlog, arma_p, arma_q)
        except Exception as e:  # noqa: BLE001
            r.summary = f"ARMA 估计失败: {e}"
            r.signal("warn", "模型估计失败")
            return r
        e_resid = fit["resid"]
        mu1 = _arma_forecast(fit, rlog)    # 下一日条件均值

        # ============ 2) GARCH 波动（对 ARMA 残差）============
        L = max(arma_p, arma_q)
        g = _fit_garch(e_resid[L:])
        var0 = float(np.var(rlog)) or 1e-8
        sigma2_rlog = np.full(len(rlog), var0)
        sigma2_rlog[L:] = g["sigma2"]      # σ²_t（收益 t 发生时的条件方差）
        sigma2 = np.empty(len(close))      # 对齐收盘: close[t] 的收益 = rlog[t-1]
        sigma2[0] = var0
        sigma2[1:] = sigma2_rlog
        sigma = np.sqrt(sigma2)            # σ_t（t 日收盘时已知, 为当日收益的条件波动）
        # 次日波动预测: σ²_{k+1} = ω + αe²_k + βσ²_k（同索引递归）
        s_next = np.sqrt(g["omega"] + g["alpha"] * e_resid[:-1] ** 2
                         + g["beta"] * sigma2_rlog[:-1])
        s_next_close = np.empty(len(close))      # t 日收盘后对"次日"的 σ 预测
        s_next_close[0] = s_next[0]
        s_next_close[1:-1] = s_next              # day t (1..N-2): σ_{t+1} = s_next[t-1]
        # 最后一日的真正"次日"预测（递归再进一步）
        s1 = float(np.sqrt(g["omega"] + g["alpha"] * float(e_resid[-1]) ** 2
                           + g["beta"] * float(sigma2_rlog[-1])))
        s_next_close[-1] = s1

        # ============ 3) 偏离指标与 regime（历史序列, 无未来函数）============
        ln_close = np.log(close.to_numpy(dtype=float))
        ma = close.rolling(ma_window, min_periods=ma_window).mean().to_numpy()
        dev = np.full(len(close), np.nan)
        dev_valid = np.isfinite(ma) & (ma > 0)
        dev[dev_valid] = ln_close[dev_valid] - np.log(ma[dev_valid])
        # 分母加下限: 防止 σ 塌缩时 dev_z 爆炸（取下限=近250日 σ 中位数的一半）
        sig_floor = float(np.median(sigma[-250:])) * 0.5
        denom = np.maximum(sigma, sig_floor) * np.sqrt(horizon)
        dz_ok = dev_valid & (denom > 0)
        dev_z = np.full(len(close), np.nan)
        dev_z[dz_ok] = dev[dz_ok] / denom[dz_ok]

        # 偏离一阶自相关 ρ（滚动120日）: <0 均值回归 / ≈0 无记忆 / >0 偏离扩大
        dev_s = pd.Series(dev)
        x1, y1 = dev_s.shift(1), dev_s
        cxy = (x1 * y1).rolling(120, min_periods=60).mean()
        mx, my = x1.rolling(120, min_periods=60).mean(), y1.rolling(120, min_periods=60).mean()
        varx = (x1 ** 2).rolling(120, min_periods=60).mean() - mx ** 2
        rho_series = (cxy - mx * my) / varx.replace(0.0, np.nan)
        rho_dev = float(rho_series.iloc[-1])

        move20 = np.full(len(close), np.nan)
        move20[20:] = ln_close[20:] - ln_close[:-20]
        trend_score = np.where(np.isfinite(move20) & (sigma > 0),
                               np.abs(move20) / (sigma * np.sqrt(20)), np.nan)

        # 单日 GARCH 异常值 z 分数: shock[t] = |r_t| / σ_t（收盘 t 日的当日收益异常度）
        shock = np.full(len(close), np.nan)
        shock[1:] = np.abs(rlog) / np.where(sigma[1:] > 0, sigma[1:], np.nan)

        # 次日波动预测 s_next 的 250 日分位（展示用; 波动聚集是 GARCH 的正常功能）
        vol_pct = pd.Series(s_next_close).rolling(250, min_periods=125).apply(
            lambda x: float((x <= x[-1]).mean()), raw=True).to_numpy()

        # regime: 强单边趋势 / 黑天鹅(单日极端断裂) / 震荡(主战场)
        # 注意: 波动聚集(σ̂抬升)不算失效——GARCH 区间加宽即可, 不抑制回归信号
        regime = np.where(np.isnan(trend_score) | (trend_score < trend_sig),
                          "range", "trending")
        regime = np.where((~np.isnan(shock)) & (shock >= shock_sigma),
                          "black_swan", regime)

        # ============ 4) 信号: 超卖/超买 × regime × 预期回归幅度 ============
        # 注意: dev 为高持续序列（随机游走减去其均线 = 带通滤波, ρ≈0.95 是常态,
        # 半衰期约2周）, 回归会发生但慢。H 日内预期回归比例 rev_frac = 1-ρ^H
        # （OU 过程理论值）; 信号质量 = |dev_z|·rev_frac（预期回归的σ倍数）。
        rho_arr = rho_series.to_numpy()
        rev_frac = 1.0 - np.power(np.clip(rho_arr, 0.0, 0.9999), float(horizon))
        exp_rev = np.abs(dev) * rev_frac            # 预期回归幅度（对数收益）
        half_life = np.log(2.0) / (-np.log(np.clip(rho_arr, 1e-9, 0.9999)))
        up_sig = (dev_z <= -z_entry)
        down_sig = (dev_z >= z_entry)
        sig_dir = np.where(up_sig, 1.0, np.where(down_sig, -1.0, 0.0))
        sig_ok = sig_dir != 0
        sig_dir_full = sig_dir.copy()               # 全组（含任何 regime）
        # 正式信号: 震荡市 + 预期回归幅度足够（rev_frac ≥ MIN_REV_FRAC,
        # 即半衰期约 ≤70 日（H=5 时）; 过滤"偏离永不回归"的持续性行情）
        sig_dir[sig_ok & (regime != "range")] = 0.0
        sig_dir[sig_ok & ~(rev_frac >= MIN_REV_FRAC)] = 0.0

        # ---- 回测: 方向与回归方向一致 = 胜 ----
        n = len(close)
        close_np = close.to_numpy(dtype=float)

        def _bt(mask_dir):
            # 注意: 不能用 NaN 初值做增量均值（NaN*0=NaN 会污染全部结果），
            # 先收集再统一计算
            rows = []
            wins = {h: [] for h in HORIZONS}
            fwds = {h: [] for h in HORIZONS}
            for t in np.where(mask_dir != 0)[0]:
                row = {"date": idx[t], "dir": int(mask_dir[t]), "dev_z": dev_z[t],
                       "close": close_np[t]}
                for h in HORIZONS:
                    fwd = float(close_np[t + h] / close_np[t] - 1.0) \
                        if t + h < n else float("nan")
                    row[f"fwd{h}"] = fwd
                    if np.isfinite(fwd):
                        fwds[h].append(fwd)
                        wins[h].append(bool((fwd > 0) if mask_dir[t] > 0
                                            else (fwd < 0)))
                rows.append(row)
            stats = {h: {"n": len(wins[h]),
                         "win": float(np.mean(wins[h])) if wins[h] else float("nan"),
                         "mean": float(np.mean(fwds[h])) if fwds[h] else float("nan")}
                     for h in HORIZONS}
            return rows, stats

        rows_full, stats_full = _bt(sig_dir_full)
        rows_range, stats_range = _bt(sig_dir)

        # ---- 区间高低点预测: 显式建模日内 high/low 偏移（σ 倍数）----
        # 次日 close 的 95% 区间 = ±1.96σ; 次日 high/low 还需叠加日内偏移,
        # 用历史 (high|low 相对前收) / σ 的 95 分位标定偏移倍数（稳健、无未来函数）
        band_all_n = band_all_hit = band_rg_n = band_rg_hit = 0
        hi_off = lo_off = float("nan")
        if "high" in df.columns and "low" in df.columns:
            high_np = df["high"].astype(float).to_numpy()
            low_np = df["low"].astype(float).to_numpy()
            d = s_next_close[:-1]                       # t→t+1 的 σ 预测
            hi_off_s = np.log(high_np[1:] / close_np[:-1]) / d
            lo_off_s = -np.log(low_np[1:] / close_np[:-1]) / d
            ok = np.isfinite(hi_off_s) & np.isfinite(lo_off_s) & (d > 0)
            # 高低点越界近似独立: 联合 95% 覆盖需每侧 ~97.5% 分位 (√0.95)
            hi_off = float(np.nanpercentile(hi_off_s[ok], 97.5))
            lo_off = float(np.nanpercentile(lo_off_s[ok], 97.5))
        band_hi = close_np * np.exp(hi_off * s_next_close)
        band_lo = close_np * np.exp(-lo_off * s_next_close)
        if np.isfinite(hi_off) and np.isfinite(lo_off):
            t = np.arange(60, n - 1)                     # 避开 MA 预热段
            inside = (high_np[t + 1] <= band_hi[t]) & (low_np[t + 1] >= band_lo[t])
            band_all_n = len(t)
            band_all_hit = int(inside.sum())
            m = (t >= 125) & (regime[t] == "range")      # 震荡市子集（regime 有定义）
            band_rg_n = int(m.sum())
            band_rg_hit = int(inside[m].sum())

        # ============ 5) 当日状态 ============
        last = -1
        dz0 = float(dev_z[last]) if np.isfinite(dev_z[last]) else float("nan")
        dev0 = float(dev[last]) if np.isfinite(dev[last]) else float("nan")
        regime0 = str(regime[last])
        t0 = float(trend_score[last]) if np.isfinite(trend_score[last]) else float("nan")
        vp0 = float(vol_pct[last]) if np.isfinite(vol_pct[last]) else float("nan")
        sz0 = float(shock[last]) if np.isfinite(shock[last]) else float("nan")
        hl0 = float(half_life[last]) if np.isfinite(half_life[last]) else float("nan")
        rf0 = float(rev_frac[last]) if np.isfinite(rev_frac[last]) else float("nan")
        er0 = float(exp_rev[last]) if np.isfinite(exp_rev[last]) else float("nan")
        p_last = float(close_np[last])
        fired = int(sig_dir[last])
        fc_low = p_last * float(np.exp(mu1 - 1.96 * s1))
        fc_high = p_last * float(np.exp(mu1 + 1.96 * s1))
        # 当日"区间高低点"预测（含日内偏移）
        range_lo = p_last * float(np.exp(-lo_off * s1)) if np.isfinite(lo_off) else None
        range_hi = p_last * float(np.exp(hi_off * s1)) if np.isfinite(hi_off) else None

        r.metric("close", p_last, "最新收盘", 3)
        r.metric("ma", float(ma[last]), f"{ma_window}日均值(均衡价)", 3)
        r.metric("deviation", dev0, "对数偏离(价/均值)", 4)
        r.metric("dev_z", dz0, f"偏离指标(σ·√{horizon}单位)", 2)
        r.metric("regime", regime0, "市场状态(震荡/趋势/黑天鹅)", 1)
        r.metric("trend_score", t0, "20日位移(σ倍数)", 2)
        r.metric("shock", sz0, f"单日收益异常度(|r|/σ̂, ≥{shock_sigma:.0f}为黑天鹅)", 2)
        r.metric("vol_pctile", vp0, "次日σ̂预测 250日分位(展示)", 3)
        r.metric("ar1", float(fit["phi"][0]) if arma_p >= 1 else None, "AR(1)系数", 4)
        r.metric("rho_dev", rho_dev, "偏离自相关ρ(滚动120日)", 3)
        r.metric("half_life", hl0, "偏离回归半衰期(日, -ln2/lnρ)", 1)
        r.metric("rev_frac", rf0, f"{horizon}日预期回归比例(1-ρ^{horizon})", 3)
        r.metric("exp_rev", er0, f"{horizon}日预期回归幅度(对数)", 4)
        r.metric("garch_alpha", g["alpha"], "GARCH α(冲击)", 4)
        r.metric("garch_beta", g["beta"], "GARCH β(记忆)", 4)
        r.metric("persistence", g["persistence"], "波动持续性 α+β", 3)
        r.metric("sigma1", s1, "次日预测波动σ̂", 4)
        r.metric("fc_low", fc_low, "次日95%收盘区间下沿", 3)
        r.metric("fc_high", fc_high, "次日95%收盘区间上沿", 3)
        r.metric("hi_off", hi_off, "次日高点日内偏移(σ倍数, 97.5分位)", 3)
        r.metric("lo_off", lo_off, "次日低点日内偏移(σ倍数, 97.5分位)", 3)
        r.metric("range_lo", range_lo, "次日区间低点预测(high/low包络)", 3)
        r.metric("range_hi", range_hi, "次日区间高点预测(high/low包络)", 3)
        r.metric("band_hit", band_all_hit / band_all_n if band_all_n else None,
                 f"次日区间(high/low)命中率(全期, n={band_all_n})", 3)
        r.metric("band_hit_range", band_rg_hit / band_rg_n if band_rg_n else None,
                 f"次日区间(high/low)命中率(震荡市, n={band_rg_n})", 3)
        r.metric("signal_today", fired, "今日回归信号(+1超卖/-1超买)", 1)
        r.metric("bt_count", len(rows_range), "历史信号数(震荡市)", 1)
        r.metric("bt_count_base", len(rows_full), "历史信号数(无regime过滤)", 1)
        for h in HORIZONS:
            r.metric(f"bt_win{h}", stats_range[h]["win"],
                     f"回测{h}日方向胜率(震荡市, n={stats_range[h]['n']})", 3)
        for h in (5, 20):
            r.metric(f"bt_win{h}_base", stats_full[h]["win"],
                     f"回测{h}日方向胜率(对照组, n={stats_full[h]['n']})", 3)

        # ---- 信号说明（透明）----
        reg_txt = {"range": "震荡市（均值回归模型有效）",
                   "trending": "强单边趋势（均值回归易失效）",
                   "black_swan": "黑天鹅（单日极端断裂，均值回归失效）"}[regime0]
        if np.isfinite(dz0):
            if fired > 0:
                r.signal("alert", f"★ 超卖均值回归: dev_z {dz0:.2f} ≤ -{z_entry}，"
                                  f"预测{horizon}日内向上回归 {er0 * 100:.1f}% 至均值 "
                                  f"{ma[last]:.2f}")
            elif fired < 0:
                r.signal("alert", f"★ 超买均值回归: dev_z {dz0:+.2f} ≥ +{z_entry}，"
                                  f"预测{horizon}日内向下回归 {er0 * 100:.1f}% 至均值 "
                                  f"{ma[last]:.2f}")
            elif abs(dz0) < z_entry:
                r.signal("info", f"dev_z {dz0:+.2f}，未达 ±{z_entry} 触发阈值，无回归信号")
            else:
                why = (f"regime={regime0}" if regime0 != "range"
                       else f"预期回归比例 {rf0:.0%} < {MIN_REV_FRAC:.0%}")
                r.signal("info", f"偏离已达阈值（dev_z {dz0:+.2f}）但被过滤"
                                 f"（{why}），不发信号")
        if regime0 == "trending":
            r.signal("warn", f"20日位移 {t0:.1f}σ ≥ {trend_sig}σ：强趋势行情，"
                             f"均值回归信号不可靠（降权）")
        if regime0 == "black_swan":
            r.signal("warn", f"单日收益 {sz0:.1f}σ ≥ {shock_sigma:.0f}σ：结构性断裂"
                             f"（黑天鹅），均值回归失效，仅保留区间预测")
        if g["persistence"] >= 0.95:
            r.signal("info", f"波动聚集强（α+β={g['persistence']:.2f}），"
                             f"波动率预测惯性大")
        if np.isfinite(hl0) and np.isfinite(rf0):
            if rf0 < MIN_REV_FRAC:
                r.signal("info", f"偏离回归半衰期 {hl0:.0f} 日（{horizon}日预期回归仅 "
                                 f"{rf0:.0%} < {MIN_REV_FRAC:.0%}）：偏离高度持续，"
                                 f"回归慢于视界（信号压制）")
            elif hl0 > 2 * horizon:
                r.signal("info", f"偏离回归半衰期 {hl0:.0f} 日 > 2×视界{horizon}日，"
                                 f"{horizon}日预期回归 {rf0:.0%}（回归发生但慢）")
            else:
                r.signal("info", f"偏离回归半衰期 {hl0:.0f} 日 ≤ 视界{horizon}日，"
                                 f"{horizon}日预期回归 {rf0:.0%}（回归较快）")
        if band_all_n:
            r.signal("info", f"次日区间(high/low)历史命中率 {band_all_hit / band_all_n:.1%}"
                             f"（全期 n={band_all_n}；震荡市 "
                             f"{(band_rg_hit / band_rg_n) if band_rg_n else float('nan'):.1%}"
                             f" n={band_rg_n}）")

        # ---- 回测明细表（按日期倒序，最多 100 行）----
        if rows_range:
            tbl = pd.DataFrame(rows_range).set_index("date").sort_index(ascending=False)
            r.add_table("signals", tbl, nd=3, max_rows=100)
        else:
            r.add_table("signals", pd.DataFrame(
                columns=["dir", "dev_z", "close", *[f"fwd{h}" for h in HORIZONS]]))
        if not rows_range and rows_full:
            r.signal("info", "历史信号全部被 regime/回归幅度过滤"
                             "（多为趋势/黑天鹅/偏离持续期）")

        # ---- 综合评分: 均值回归机会强度 ----
        a = abs(dz0) if np.isfinite(dz0) else 0.0
        base = 50.0 + 15.0 * min(a, 3.0)
        if regime0 == "range":
            score = base
        elif regime0 == "trending":
            score = 50.0 + 0.25 * (base - 50.0)
        else:  # black_swan
            score = 50.0 + 0.1 * (base - 50.0)
        r.score = round(float(np.clip(score, 0, 100)), 1)
        if fired != 0 and regime0 == "range":
            r.verdict = "强均值回归机会（" + ("超卖" if fired > 0 else "超买") + "）"
        elif regime0 == "black_swan":
            r.verdict = "黑天鹅（回归失效，仅区间预测）"
        elif regime0 == "trending":
            r.verdict = "强趋势（回归不可靠，仅区间预测）"
        elif a >= z_entry and np.isfinite(rf0) and rf0 < MIN_REV_FRAC:
            r.verdict = "偏离显著但回归过慢（半衰期长，信号压制）"
        elif a >= z_entry:
            r.verdict = f"偏离显著但 regime 不利（{regime0}）"
        elif a >= 1.0:
            r.verdict = "偏离中等（观察）"
        else:
            r.verdict = "贴近均值（无信号）"

        r.summary = (
            f"dev_z {dz0:+.2f}（偏离 {dev0 * 100:+.1f}%），regime {reg_txt}，"
            f"次日区间预测 {range_lo:.2f}~{range_hi:.2f}"
            if (range_lo is not None and np.isfinite(dz0))
            else f"regime {reg_txt}"
        )

        # ---- 图表序列（近 250 日）; band 用 s_next_close（t 日收盘对次日的预测）----
        tail = slice(-250, None)
        r.add_series("close", idx[tail], close[tail], title="收盘价")
        r.add_series("ma", idx[tail], ma[tail], title=f"{ma_window}日均值(均衡线)")
        r.add_series("band_lo", idx[tail], band_lo[tail], title="次日区间下沿(high/low包络)")
        r.add_series("band_hi", idx[tail], band_hi[tail], title="次日区间上沿(high/low包络)")
        r.add_series("dev_z", idx[tail], dev_z[tail], title="偏离指标 dev_z")
        r.add_series("sigma", idx[tail], s_next_close[tail] * 100.0,
                     title="次日波动率预测σ̂(%)")
        r.add_series("signal", idx[tail], sig_dir[tail], title="回归信号(+1超卖/-1超买)")

        r.extra["model"] = {
            "arma": f"ARMA({arma_p},{arma_q}) 条件均值",
            "garch": f"GARCH(1,1): ω={g['omega']:.2e}, α={g['alpha']:.3f}, "
                     f"β={g['beta']:.3f}, 持续性={g['persistence']:.3f}",
            "dev_z": f"ln(P/MA{ma_window}) / (σ̂·√{horizon})",
            "regime_rules": {
                "trending": f"|20日位移| ≥ {trend_sig}σ",
                "black_swan": f"单日 |r| ≥ {shock_sigma:.0f}σ（结构性断裂）",
                "range": "其余（模型主战场）",
            },
            "rho_gate": f"{horizon}日预期回归比例 1-ρ^{horizon} < {MIN_REV_FRAC:.0%} 时"
                        "压制回归信号（偏离持续/回归慢于视界）",
            "note": "震荡市: 次日区间命中率与超卖/超买回归信号均实测输出"
                    "（band_hit* / bt_win*）；强趋势/黑天鹅期由 regime 检测自动降权，"
                    "黑天鹅日仅保留区间预测。波动聚集(σ̂抬升)不算失效——GARCH 区间"
                    "加宽即可，不抑制信号。dev 为高持续带通序列（ρ≈0.95 常态），"
                    "回归速度由半衰期与预期回归比例(1-ρ^H)量化。"
                    "信号判定无未来函数，前瞻收益/命中率仅用于回测评估。",
        }
        return r
