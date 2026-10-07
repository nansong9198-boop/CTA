#!/usr/bin/env python3
"""回撤分布分析公共模块(CTA周频体系与股票类体系共用, 2026-10-07用户拍板统一口径)。

回撤不只看单次最大值, 看分布:
- 独立回撤episode = 峰值 -> 谷底 -> 修复创新高; 当前仍水下的为未修复episode
- 前5大回撤均值: episode按深度排序取前5取平均(不足5个用实际数量, 低置信度)
- 回撤集中度 = 最大回撤 / 前5大均值(≈1: 深度回撤是常态; 明显>1: 单次尾部事件)
- 实质回撤平均修复时间只统计谷底<-5%的episode, 避免微小波动一周修复拉低均值
"""
import statistics


def drawdown(curve):
    """单次最大回撤(序列口径)"""
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd


def drawdown_episodes(dates, curve):
    """净值曲线 -> 独立回撤episode列表(峰值->谷底->修复创新高)。

    每episode: depth(谷底/峰-1), peak_date/trough_date/recovery_date,
    repair_weeks(谷底->创新高, 周), repaired(未修复=当前仍水下)
    """
    eps = []
    peak, peak_date = curve[0], dates[0]
    in_dd = False
    ep = None
    for d, v in zip(dates[1:], curve[1:]):
        if v >= peak:
            if in_dd:
                ep["recovery_date"] = d
                ep["repair_weeks"] = round((d - ep["trough_date"]).days / 7, 1)
                ep["repaired"] = True
                eps.append(ep)
                in_dd = False
            peak, peak_date = v, d
        else:
            if not in_dd:
                in_dd = True
                ep = dict(peak_date=peak_date, peak=peak,
                          trough_date=d, trough=v, repaired=False)
            if v < ep["trough"]:
                ep["trough"], ep["trough_date"] = v, d
    if in_dd:  # 未修复: 损失是现实的而非历史的
        ep["depth"] = ep["trough"] / ep["peak"] - 1
        eps.append(ep)
    for ep in eps:
        ep.setdefault("depth", ep["trough"] / ep["peak"] - 1)
    return eps


def drawdown_shape(dates, curve):
    """回撤形态四指标: 前5大回撤均值/回撤集中度/深度回撤次数/实质回撤平均修复周数"""
    eps = drawdown_episodes(dates, curve)
    if not eps:
        return dict(top5_mdd=0.0, dd_concentration=float("nan"), n_deep=0,
                    avg_repair_weeks=None, n_episodes=0, n_unrepaired=0,
                    dd_low_conf=True, top5=[])
    by_depth = sorted(eps, key=lambda e: e["depth"])
    top5 = by_depth[:5]
    top5_mean = statistics.mean(e["depth"] for e in top5)
    mdd = by_depth[0]["depth"]
    repaired = [e for e in eps if e["repaired"]]
    # 平均修复时间只统计实质回撤(谷底<-5%), 避免微小波动一周修复拉低均值
    repaired_deep = [e for e in repaired if e["depth"] < -0.05]
    return dict(
        top5_mdd=top5_mean,                      # 前5大回撤均值
        dd_concentration=mdd / top5_mean if top5_mean else float("nan"),  # ≈1: 深度回撤是常态
        n_deep=sum(1 for e in eps if e["depth"] < -0.10),  # 深度回撤次数(谷底跌超-10%)
        avg_repair_weeks=(round(statistics.mean(e["repair_weeks"] for e in repaired_deep), 1)
                          if repaired_deep else None),
        n_episodes=len(eps),
        n_unrepaired=sum(1 for e in eps if not e["repaired"]),
        dd_low_conf=len(eps) < 5,                # episode不足5个: 均值低置信度
        top5=top5,
    )
