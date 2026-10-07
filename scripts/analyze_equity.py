#!/usr/bin/env python3
"""股票类私募适配度评估(方法论独立于CTA体系, 见 docs/equity_evaluation_plan.md)。

范围: 仅评估用户持有的3只股票类产品, 不建候选池。
数据源: data/simuwang/<产品名>.json (排排网周频净值) + data/idx_daily.json (东财/新浪日K)
输出: docs/equity_evaluation_report.md + 终端指标表

口径说明:
- 周频(每周最后一个净值点), 年化按 n/52, RF=1.5%(与CTA体系一致)
- 基准各自定制(用户2026-10-07拍板):
    国源拾金3号      = 沪深300x60% + 黄金ETF(518880)x40%  (持仓约30%股+30%面值黄金期货)
    龙旗X计划12号1期 = 沪深300x50% + 现金(0收益)x50%       (中性+择时+轮动的混合代理)
    龙旗红利科技轮动平衡5号 = 中证红利x50% + 创业板指x50%  (红利+科技哑铃代理, 满仓无择时)
  另附四指数相关性自动识别结果作诊断对照
- 超额序列 = 基金周收益 - 基准周收益; 超额回撤 = 累计超额净值的回撤(指增关键指标)
- 上行/下行捕获比 = 基准涨/跌周的基金平均收益 / 基准平均收益
- 危机相对表现 = 基准季度收益 < -10% 的季度里基金的超额(股票类不要求危机赚钱, 要求少跌)
- 权重(用户拍板): 信息比率25% 超额回撤(逆)15% 下行捕获比(逆)15% 索提诺15% alpha10% 卡玛10% 最长水下(逆)10%
- 指增(龙旗)与主观多资产(国源)分类型标注不混排; 类型内样本=1时分位数退化为0.5, 评分仅供参考
- 硬阈值(不达标->观察档): 费后年化>=10%, 前5大回撤均值>=-20%, 夏普>=1.0, 信息比率>=0.5
  (回撤看分布不看单次最大值——单次回撤只是尾部事件, 风控能力看分布; 用户2026-10-07拍板;
  排排网披露净值为费后口径, 业绩报酬已在净值中扣除)
- 软指标(扣分不否决): 最长水下>12个月 -> 得分-0.05
- 营销诚信度: 宣传口径 vs 实际净值偏差>2倍 -> 得分-0.10 并标注
  (典型: 龙旗X计划宣传最大回撤2.8% vs 实际-22.0%, 偏差近8倍)
- 可靠性折扣: min(1, 周数/312), 即6年满信度
"""
import json
import math
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_weekly import resample_weekly  # 周频重采样与CTA体系一致

RF = 0.015
WEEKS_PER_YEAR = 52
FULL_RELIABILITY_WEEKS = 312  # 6年满信度
MIN_SAMPLE_WEEKS = 52         # 周数不足标记观察
MGR_MIN_AGE = 5               # 管理人成立不足5年剔除
MGR_MIN_AUM = 10              # 规模不足10亿剔除(亿元)
UW_SOFT_DAYS = 365            # 最长水下>12个月: 软指标扣分
UW_SOFT_PENALTY = 0.05
MARKETING_PENALTY = 0.10      # 宣传与实际偏差>2倍
HARD = dict(ann_ret=0.10, top5_mdd=-0.20, sharpe=1.0, ir=0.5)  # 硬阈值(回撤看前5大episode均值)

PRODUCTS = [
    dict(name="国源拾金3号", ptype="主观多资产（股票+黄金）",
         bench={"hs300": 0.6, "gold518880": 0.4},
         bench_name="沪深300×60% + 黄金ETF(518880)×40%",
         holding_wan=149.7, pnl_pct=+18.65),
    dict(name="龙旗红利科技轮动平衡5号", ptype="量化多头（红利↔科技指增哑铃，满仓无择时）",
         bench={"cndiv": 0.5, "cyb": 0.5},
         bench_name="中证红利×50% + 创业板指×50%（哑铃代理）",
         holding_wan=91.2, pnl_pct=-8.77),
    dict(name="龙旗X计划12号1期", ptype="量化多头（中性+择时+量选轮动，动态仓位）",
         bench={"hs300": 0.5},  # 其余50%为现金(周收益0), 混合=中性+股指代理
         bench_name="沪深300×50% + 现金×50%（中性+股指混合代理）",
         holding_wan=89.6, pnl_pct=-10.38,
         marketing=dict(claim_mdd=-0.028,
                        claim_text="路演宣称X计划最大回撤仅2.8%（策略指数口径，截至路演日）")),
]
IDX_CN = {"hs300": "沪深300", "zz500": "中证500", "zz1000": "中证1000",
          "cyb": "创业板指", "cndiv": "中证红利", "gold518880": "黄金ETF"}
CASH_BAO_WAN = 248.0  # 现金宝(无风险)


def load_nav(name):
    d = json.loads((ROOT / "data" / "simuwang" / f"{name}.json").read_text(encoding="utf-8"))
    ns = [(date.fromisoformat(x["date"]), float(x["cum_nav"]))
          for x in (d.get("nav_series") or []) if x.get("cum_nav") is not None]
    return d, sorted(ns)


def weekly_index_returns(daily: dict) -> dict:
    """指数日收盘 -> {ISO周key: 周收益}(每周最后交易日收盘环比)"""
    wk = {}
    for ds, c in sorted(daily.items()):
        d = date.fromisoformat(ds)
        iso = d.isocalendar()
        wk[(iso[0], iso[1])] = c
    keys = sorted(wk)
    return {keys[i]: wk[keys[i]] / wk[keys[i - 1]] - 1 for i in range(1, len(keys))}


def align_fund_bench(fund_points, bench_comp_rets, weights):
    """基金周频点与加权基准对齐 -> (dates, fund_rets, bench_rets, fund_nav, bench_nav)"""
    f_wk = {}
    for d, v in fund_points:
        iso = d.isocalendar()
        f_wk[(iso[0], iso[1])] = (d, v)
    fkeys = sorted(f_wk)
    f_ret = {fkeys[i]: f_wk[fkeys[i]][1] / f_wk[fkeys[i - 1]][1] - 1
             for i in range(1, len(fkeys))}
    common = sorted(set(f_ret) & set.intersection(*[set(r) for r in bench_comp_rets.values()]))
    fr = [f_ret[k] for k in common]
    br = [sum(w * bench_comp_rets[idx].get(k, 0.0) for idx, w in weights.items())
          for k in common]
    dates = [f_wk[k][0] for k in common]
    fn, bn, fnav, bnav = 1.0, 1.0, [], []
    for a, b in zip(fr, br):
        fn *= 1 + a; bn *= 1 + b
        fnav.append(fn); bnav.append(bn)
    return dates, fr, br, fnav, bnav


def drawdown(curve):
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
    """回撤形态四指标(用户2026-10-07拍板: 回撤看分布, 不只看单次最大值)"""
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


def uw_stats(dates, curve):
    """最长水下: 周数 + 自然日天数"""
    uw, uw_max = 0, 0
    peak, pk_date, days_max = curve[0], dates[0], 0
    cur_pk_date = dates[0]
    for d, v in zip(dates[1:], curve[1:]):
        if v >= peak:
            peak, uw = v, 0
            cur_pk_date = d
        else:
            uw += 1
            uw_max = max(uw_max, uw)
            days_max = max(days_max, (d - cur_pk_date).days)
    return uw_max, days_max


def metrics(dates, fr, br, fnav, bnav):
    n = len(fr)
    if n < 4:
        return None
    years = n / WEEKS_PER_YEAR
    ann_ret = fnav[-1] ** (1 / years) - 1
    bench_ann = bnav[-1] ** (1 / years) - 1
    excess_ann = (fnav[-1] / bnav[-1]) ** (1 / years) - 1  # 几何超额年化
    ex = [a - b for a, b in zip(fr, br)]
    ex_curve, c = [], 1.0
    for e in ex:
        c *= 1 + e
        ex_curve.append(c)
    ex_std = statistics.stdev(ex) if n > 1 else float("nan")
    ir = statistics.mean(ex) / ex_std * math.sqrt(WEEKS_PER_YEAR) if ex_std else float("nan")
    excess_mdd = drawdown(ex_curve)
    # beta/alpha(周频OLS)
    mf, mb = statistics.mean(fr), statistics.mean(br)
    var_b = statistics.pvariance(br)
    beta = (sum((a - mf) * (b - mb) for a, b in zip(fr, br)) / n / var_b) if var_b else float("nan")
    alpha_ann = (mf - beta * mb) * WEEKS_PER_YEAR if beta == beta else float("nan")
    mdd = drawdown(fnav)
    w_std = statistics.stdev(fr)
    ann_vol = w_std * math.sqrt(WEEKS_PER_YEAR)
    sharpe = (ann_ret - RF) / ann_vol if ann_vol else float("nan")
    downside = [min(r, 0.0) for r in fr]
    dd_ann = math.sqrt(sum(d * d for d in downside) / n) * math.sqrt(WEEKS_PER_YEAR)
    sortino = (ann_ret - RF) / dd_ann if dd_ann else float("nan")
    calmar = ann_ret / abs(mdd) if mdd else float("nan")
    win = sum(1 for r in fr if r > 0) / n
    new_high = sum(1 for i, v in enumerate(fnav) if v >= max(fnav[:i + 1])) / n
    uw_max, uw_days = uw_stats(dates, fnav)
    shape = drawdown_shape(dates, fnav)
    # 上行/下行捕获比
    up = [(a, b) for a, b in zip(fr, br) if b > 0]
    dn = [(a, b) for a, b in zip(fr, br) if b < 0]
    up_cap = (statistics.mean(a for a, _ in up) / statistics.mean(b for _, b in up)) if up else float("nan")
    down_cap = (statistics.mean(a for a, _ in dn) / statistics.mean(b for _, b in dn)) if dn else float("nan")
    return dict(n=n, ann_ret=ann_ret, bench_ann=bench_ann, excess_ann=excess_ann,
                ir=ir, beta=beta, alpha_ann=alpha_ann, mdd=mdd, excess_mdd=excess_mdd,
                ann_vol=ann_vol, sharpe=sharpe, sortino=sortino, calmar=calmar,
                win=win, new_high=new_high, uw_max=uw_max, uw_days_max=uw_days,
                up_cap=up_cap, down_cap=down_cap, **shape)


def quarterly_from_weekly(dates, curve):
    """周频净值 -> {季度: 季收益}"""
    qend = {}
    for d, v in zip(dates, curve):
        q = f"{d.year}Q{(d.month - 1) // 3 + 1}"
        qend[q] = v
    qs = sorted(qend)
    return {qs[i]: qend[qs[i]] / qend[qs[i - 1]] - 1 for i in range(1, len(qs))}


def crisis_relative(dates, fnav, bnav, limit=-0.10):
    """基准大跌季度(季收益<limit)的基金超额"""
    fq = quarterly_from_weekly(dates, fnav)
    bq = quarterly_from_weekly(dates, bnav)
    out = []
    for q in sorted(set(fq) & set(bq)):
        if bq[q] < limit:
            out.append((q, fq[q], bq[q], fq[q] - bq[q]))
    return out


def corr(a, b):
    ma, mb = statistics.mean(a), statistics.mean(b)
    sd = statistics.pstdev(a) * statistics.pstdev(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a) / sd if sd else float("nan")


def score_pool(pool):
    """分类型标注不混排; 类型内样本=1时分位数退化为0.5(评分仅供参考, 分层以硬阈值为主)"""
    by_type = {}
    for r in pool:
        by_type.setdefault(r["ptype"], []).append(r)
    for rs in by_type.values():
        if len(rs) < 2:
            for r in rs:
                r["degenerate"] = True
    spec = [("ir", False), ("excess_mdd", False), ("down_cap", True),
            ("sortino", False), ("alpha_ann", False), ("calmar", False),
            ("uw_max", True)]
    weights = dict(ir=0.25, excess_mdd=0.15, down_cap=0.15, sortino=0.15,
                   alpha_ann=0.10, calmar=0.10, uw_max=0.10)
    for rs in by_type.values():
        for key, reverse in spec:
            vals = sorted(r[key] for r in rs if r.get(key) is not None and r[key] == r[key])
            for r in rs:
                v = r.get(key)
                if v is None or v != v or len(vals) < 2:
                    r["_s_" + key] = 0.5  # 样本不足/缺失取中性
                else:
                    rk = vals.index(v) / (len(vals) - 1)
                    r["_s_" + key] = 1 - rk if reverse else rk
        for r in rs:
            raw = sum(weights[k] * r["_s_" + k] for k in weights)
            rel = min(1.0, r["n"] / FULL_RELIABILITY_WEEKS)
            score = raw * rel + 0.5 * (1 - rel)
            r["raw_score"] = raw
            r["reliability"] = rel
            # 软指标: 最长水下>12个月 扣分不否决
            r["soft_penalty"] = UW_SOFT_PENALTY if r["uw_days_max"] > UW_SOFT_DAYS else 0.0
            score -= r["soft_penalty"]
            # 营销诚信度: 宣传回撤 vs 实际 偏差>2倍 扣分
            mk = r.get("marketing")
            r["mkt_penalty"] = 0.0
            if mk and r["mdd"] < mk["claim_mdd"] * 2:
                r["mkt_penalty"] = MARKETING_PENALTY
                score -= MARKETING_PENALTY
            r["score"] = max(score, 0.0)


def hard_fails(r):
    fails = []
    if r["ann_ret"] < HARD["ann_ret"]:
        fails.append(f"费后年化{r['ann_ret']*100:+.1f}%未达10%")
    if r["top5_mdd"] < HARD["top5_mdd"]:
        fails.append(f"前5大回撤均值{r['top5_mdd']*100:.1f}%超-20%")
    if r["sharpe"] < HARD["sharpe"]:
        fails.append(f"夏普{r['sharpe']:.2f}未达1.0")
    if r["ir"] != r["ir"] or r["ir"] < HARD["ir"]:
        fails.append(f"信息比率{r['ir']:.2f}未达0.5")
    return fails


def tier_of(r):
    fails = hard_fails(r)
    if fails or r["n"] < MIN_SAMPLE_WEEKS:
        if r["n"] < MIN_SAMPLE_WEEKS:
            fails.append(f"样本仅{r['n']}周(<52)")
        return "观察", fails
    # 分位数退化(类型内样本=1)时硬阈值全过 -> 保底中适配, 评分仅供参考(方案: 分层以硬阈值为主)
    if r.get("degenerate"):
        return ("高适配（核心候选）" if r["score"] >= 0.70 else "中适配（备选）"), []
    if r["score"] >= 0.70:
        return "高适配（核心候选）", []
    if r["score"] >= 0.62:
        return "中适配（备选）", []
    if r["score"] >= 0.50:
        return "观察", []
    return "低适配", []


def main():
    idx_daily = json.loads((ROOT / "data" / "idx_daily.json").read_text(encoding="utf-8"))
    idx_wk = {k: weekly_index_returns(v) for k, v in idx_daily.items()}
    # 管理人门槛数据
    mgr_info = {}
    raw_mi = json.loads((ROOT / "data" / "manager_info.json").read_text(encoding="utf-8"))
    mgr_info = {k: v for k, v in raw_mi.items() if k != "_symbol_override"}
    pm_tenure = json.loads((ROOT / "data" / "pm_tenure.json").read_text(encoding="utf-8"))

    today = date.today()
    pool, gated, excluded = [], [], []
    for p in PRODUCTS:
        name = p["name"]
        d, ns = load_nav(name)
        detail = d.get("detail") or {}
        bi = detail.get("baseInfo") or {}
        ci = detail.get("companyInfo") or {}
        rec = dict(p)
        rec["strategy"] = bi.get("first_strategy", "") + "/" + (bi.get("second_strategy") or "")
        rec["inception"] = bi.get("inception_date", "")
        rec["company"] = ci.get("company_name", "")
        rec["company_size"] = ci.get("company_asset_size", "")
        ei = detail.get("elementInfo") or {}
        rec["fees"] = (f"管理费{ei.get('management_fee_text') or '-'} / "
                       f"业绩报酬{ei.get('performance_fee_text') or '-'} / "
                       f"锁定期{(ei.get('lock_period_text') or '-').split('，')[0]}")
        cur = [m for m in (detail.get("relationManager") or [])
               if m.get("management_end_date") is None]
        rec["pm"] = "、".join(m["personnel_name"] for m in cur) or (bi.get("managers_name") or "")
        rec["pm_start"] = min((m["management_start_date"] for m in cur
                               if m.get("management_start_date")), default="")
        if rec["inception"]:
            y, mo, dd = map(int, rec["inception"].split("-"))
            rec["age"] = round((today - date(y, mo, dd)).days / 365.25, 1)
        # 管理人门槛
        mi = mgr_info.get(rec["company"])
        rec["mgr_info"] = mi
        if mi:
            if mi.get("found_year") and today.year - mi["found_year"] < MGR_MIN_AGE:
                excluded.append((name, f"管理人门槛: 成立{mi['found_year']}年, 不足{MGR_MIN_AGE}年"))
                continue
            if mi.get("aum_yi") is not None and mi["aum_yi"] < MGR_MIN_AUM:
                excluded.append((name, f"管理人门槛: 规模约{mi['aum_yi']}亿, 低于{MGR_MIN_AUM}亿"))
                continue
        # 净值门控: 无有效序列 -> 跳过评分, 仅列入持仓清单
        if len(ns) < 5:
            rec["gate_reason"] = "净值数据被排排网门控（nav_trend为空），数据不可得，跳过评分，仅列入持仓清单"
            gated.append(rec)
            print(f"[门控] {name}: {rec['gate_reason']}")
            continue
        weekly = resample_weekly(ns)
        # 任职区间过滤
        if rec["pm_start"]:
            y, mo, dd = map(int, rec["pm_start"].split("-"))
            weekly = [(dt, v) for dt, v in weekly if dt >= date(y, mo, dd)]
        # 基准序列
        comp_rets = {k: idx_wk[k] for k in p["bench"]}
        dates, fr, br, fnav, bnav = align_fund_bench(weekly, comp_rets, p["bench"])
        m = metrics(dates, fr, br, fnav, bnav)
        if not m:
            excluded.append((name, f"样本不足: 对齐后仅{len(fr)}周(<4)"))
            continue
        rec.update(m)
        rec["window"] = f"{dates[0]}~{dates[-1]}"
        # 诊断: 与四指数相关性(基准自动识别对照)
        rec["corr_detail"] = {}
        for ik in ("hs300", "zz500", "zz1000", "cyb"):
            iw = idx_wk[ik]
            common = sorted(set(iw) & set(d.isocalendar()[:2] for d in dates))
            if len(common) >= 4:
                fmap = {d.isocalendar()[:2]: r for d, r in zip(dates, fr)}
                rec["corr_detail"][ik] = corr([fmap[k] for k in common], [iw[k] for k in common])
        rec["auto_bench"] = max(rec["corr_detail"], key=rec["corr_detail"].get) \
            if rec["corr_detail"] else ""
        # 对纯沪深300的beta(权益敞口折算用; 与定制混合基准的beta区分)
        iw = idx_wk["hs300"]
        common = sorted(set(iw) & set(d.isocalendar()[:2] for d in dates))
        if len(common) >= 4:
            fmap = {d.isocalendar()[:2]: r for d, r in zip(dates, fr)}
            f_al = [fmap[k] for k in common]
            b_al = [iw[k] for k in common]
            mb = statistics.mean(b_al)
            vb = statistics.pvariance(b_al)
            rec["beta_hs300"] = sum((a - statistics.mean(f_al)) * (b - mb)
                                    for a, b in zip(f_al, b_al)) / len(f_al) / vb if vb else float("nan")
        rec["crisis_rel"] = crisis_relative(dates, fnav, bnav)
        pool.append(rec)

    score_pool(pool)
    pool.sort(key=lambda r: -r["score"])
    for r in pool:
        r["tier"], r["tier_fails"] = tier_of(r)

    # 终端指标表
    print("\n=== 股票类私募评估（周频口径，基准各自定制）===")
    hdr = f"{'产品':<18}{'类型':<12}{'周数':>4} {'年化':>7} {'超额年化':>7} {'IR':>5} {'beta':>5} {'alpha':>6} {'回撤':>7} {'超额回撤':>7} {'下捕':>5} {'夏普':>5} {'索提诺':>6} {'卡玛':>5} {'水下':>8} {'得分':>5} 分层"
    print(hdr)
    for r in pool:
        print(f"{r['name'][:16]:<18}{r['ptype'][:10]:<12}{r['n']:>4d} "
              f"{r['ann_ret']*100:>6.1f}% {r['excess_ann']*100:>6.1f}% {r['ir']:>5.2f} "
              f"{r['beta']:>5.2f} {r['alpha_ann']*100:>5.1f}% {r['mdd']*100:>6.1f}% "
              f"{r['excess_mdd']*100:>6.1f}% {r['down_cap']:>5.2f} {r['sharpe']:>5.2f} "
              f"{r['sortino']:>6.2f} {r['calmar']:>5.2f} {r['uw_max']:>3d}周/{r['uw_days_max']}天 "
              f"{r['score']:>5.2f} {r['tier']}")
    for r in gated:
        print(f"{r['name'][:16]:<18}{r['ptype'][:10]:<12}  -- 数据门控, 跳过评分 --")

    generate_report(pool, gated, excluded, idx_wk)


def generate_report(pool, gated, excluded, idx_wk):
    L = ["# 股票类私募适配度评估报告",
         f"> 生成时间: {date.today()} | 范围: 用户持有3只股票类私募 | 方法见 equity_evaluation_plan.md（v1.0 已对齐）",
         "> 分层为适配度评级（与投资者画像的匹配程度），不构成投资建议",
         "> 口径: 周频净值（年化 n/52, RF=1.5%）；基准各自定制；净值为费后口径",
         "> 注意: 各类型内样本量=1，分位数评分退化，分层以硬阈值（费后年化≥10%/前5大回撤均值≥-20%/夏普≥1.0/IR≥0.5）为主",
         ""]
    tiers = ["高适配（核心候选）", "中适配（备选）", "观察", "低适配"]
    for t in tiers:
        grp = [r for r in pool if r["tier"] == t]
        if not grp:
            continue
        L.append(f"## {t}（{len(grp)}只）\n")
        for r in grp:
            L.append(f"### {r['name']}（{r['ptype']}，{r.get('age', '?')}年，适配度得分{r['score']:.2f}）")
            L.append(f"- 统计区间: {r['window']}（{r['n']}周）"
                     + (f"；基金经理 {r['pm']}（{r['pm_start']} 起）" if r.get("pm_start") else ""))
            L.append(f"- 基准: {r['bench_name']}；四指数相关性诊断: "
                     + "，".join(f"{IDX_CN[k]}{v:+.2f}" for k, v in r["corr_detail"].items())
                     + f"（自动识别最接近: {IDX_CN.get(r['auto_bench'], '-')}）")
            L.append(f"- 年化{r['ann_ret']*100:+.1f}%（基准{r['bench_ann']*100:+.1f}%） / "
                     f"超额年化{r['excess_ann']*100:+.1f}% / 信息比率{r['ir']:.2f} / "
                     f"beta{r['beta']:.2f} / alpha{r['alpha_ann']*100:+.1f}% / "
                     f"真实回撤{r['mdd']*100:.1f}% / 超额回撤{r['excess_mdd']*100:.1f}% / "
                     f"上行捕获{r['up_cap']*100:.0f}% / 下行捕获{r['down_cap']*100:.0f}% / "
                     f"夏普{r['sharpe']:.2f} / 索提诺{r['sortino']:.2f} / 卡玛{r['calmar']:.2f} / "
                     f"周胜率{r['win']*100:.0f}% / 新高{r['new_high']*100:.0f}% / "
                     f"最长水下{r['uw_max']}周（{r['uw_days_max']}天）")
            L.append(f"- 管理人: {r['company']}（{r['company_size']}）"
                     + (f"；manager_info: {r['mgr_info'].get('aum_text', '')}"
                        if r.get("mgr_info") else "（manager_info.json 未收录）"))
            L.append(f"- 费率: {r['fees']}")
            # 回撤形态四指标(回撤看分布, 用户2026-10-07拍板)
            shape = (f"前5大回撤均值{r['top5_mdd']*100:.1f}%（{r['n_episodes']}个独立episode"
                     f"{'，不足5个低置信度' if r['dd_low_conf'] else ''}） / "
                     f"回撤集中度{r['dd_concentration']:.2f} / "
                     f"深度回撤(谷底<-10%)次数{r['n_deep']} / "
                     f"实质回撤(<-5%)平均修复{r['avg_repair_weeks']:.0f}周" if r.get("avg_repair_weeks") is not None else
                     f"前5大回撤均值{r['top5_mdd']*100:.1f}%（{r['n_episodes']}个独立episode"
                     f"{'，不足5个低置信度' if r['dd_low_conf'] else ''}） / "
                     f"回撤集中度{r['dd_concentration']:.2f} / "
                     f"深度回撤(谷底<-10%)次数{r['n_deep']} / 实质回撤(<-5%)均无已修复样本")
            L.append(f"- 回撤形态: {shape}")
            if r.get("n_unrepaired"):
                cur = next((e for e in r["top5"] if not e["repaired"]), None)
                L.append(f"- ⚠ 未修复回撤: {r['n_unrepaired']}个episode仍在水下"
                         + (f"（当前回撤最深{cur['depth']*100:.1f}%，始于{cur['peak_date']}，"
                            f"谷底{cur['trough_date']}）——未修复意味着损失是现实的而非历史的"
                            if cur else ""))
            if r["crisis_rel"]:
                txt = "；".join(f"{q} 基准{bq*100:.1f}%/基金{fq_*100:+.1f}%/超额{eq_*100:+.1f}%"
                                for q, fq_, bq, eq_ in r["crisis_rel"])
                L.append(f"- 基准大跌季（<-10%）相对表现: {txt}")
            else:
                L.append("- 基准大跌季（<-10%）相对表现: 样本窗口内无基准跌超-10%的季度")
            if r.get("tier_fails"):
                L.append("- 硬阈值未达标: " + "；".join(r["tier_fails"]))
            if r.get("soft_penalty"):
                L.append(f"- 软指标扣分: 最长水下{r['uw_days_max']}天超12个月（-{UW_SOFT_PENALTY}分，不否决）")
            if r.get("mkt_penalty"):
                mk = r["marketing"]
                L.append(f"- 营销诚信度扣分（-{MARKETING_PENALTY}分）: {mk['claim_text']}，"
                         f"实际净值最大回撤{r['mdd']*100:.1f}%，偏差{abs(r['mdd']/mk['claim_mdd']):.1f}倍")
            pros, cons = reasons(r)
            L.append("- 适配理由: " + "；".join(pros))
            if cons:
                L.append("- 风险点: " + "；".join(cons))
            L.append("")
    if gated:
        L.append("## 数据不可得（跳过评分，仅列入持仓清单）\n")
        for r in gated:
            L.append(f"### {r['name']}（{r['ptype']}）")
            L.append(f"- {r['gate_reason']}")
            L.append(f"- 已知信息: 成立{r.get('inception', '?')}，策略{r.get('strategy', '?')}，"
                     f"管理人{r.get('company', '?')}（{r.get('company_size', '?')}），费率: {r.get('fees', '-')}；"
                     f"用户持仓{r['holding_wan']}万，当前浮盈{r['pnl_pct']:+.2f}%（持仓截图口径）")
            L.append(f"- 注意: 成立于{r.get('inception', '?')}，即使取得净值也只有约半年样本，"
                     f"可靠性折扣 min(1,周数/312) 后评分将极低，须以长样本同系产品佐证")
            L.append("")
    if excluded:
        L.append("## 排除名单（门槛未过）\n")
        for name, reason in excluded:
            L.append(f"- **{name}**：{reason}")
        L.append("")
    L += portfolio_section(pool, gated)
    L += appendix_peer_section()
    L.append("---")
    L.append("> 免责声明： 本报告仅作信息整理与适配度分析，不构成任何投资建议、要约或收益承诺。"
             "私募证券基金过往业绩不预示未来表现，投资者应自行承担投资风险。"
             "数据来源于蛋卷基金公开货架信息及第三方平台截图，可能存在口径偏差或滞后，"
             "最终以基金管理人正式披露文件及基金合同为准。合格投资者认定与适当性匹配请以持牌销售机构流程为准。")
    (ROOT / "docs" / "equity_evaluation_report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"\n报告已生成: docs/equity_evaluation_report.md（{len(pool)}只评分 + {len(gated)}只门控 + {len(excluded)}只排除）")


def reasons(r):
    pros, cons = [], []
    if r["ir"] == r["ir"] and r["ir"] >= 0.5:
        pros.append(f"信息比率{r['ir']:.2f}，超额稳定")
    if r["excess_ann"] > 0.03:
        pros.append(f"超额年化{r['excess_ann']*100:+.1f}%，跑赢定制基准")
    if r["down_cap"] == r["down_cap"] and r["down_cap"] < 1.0:
        pros.append(f"下行捕获{r['down_cap']*100:.0f}%，跌时比基准少跌")
    if r["top5_mdd"] > -0.20 and not r["dd_low_conf"]:
        pros.append(f"前5大回撤均值{r['top5_mdd']*100:.1f}%在用户容忍度（-20%）内")
    if r.get("dd_concentration") == r.get("dd_concentration") and r["dd_concentration"] > 1.5 \
       and r["top5_mdd"] > -0.20 and not r["dd_low_conf"]:
        pros.append(f"回撤集中度{r['dd_concentration']:.1f}，最深回撤为单次尾部事件而非常态")
    if r.get("alpha_ann", 0) > 0.03 and r["n"] >= MIN_SAMPLE_WEEKS:
        pros.append(f"周频回归alpha年化{r['alpha_ann']*100:+.1f}%")
    if r["ann_ret"] < HARD["ann_ret"]:
        cons.append(f"费后年化{r['ann_ret']*100:+.1f}%低于10%目标")
    if r["top5_mdd"] < HARD["top5_mdd"]:
        cons.append(f"前5大回撤均值{r['top5_mdd']*100:.1f}%超-20%容忍线"
                    + ("（episode不足5个，低置信度）" if r["dd_low_conf"] else ""))
    if r.get("dd_concentration") == r.get("dd_concentration") and r["dd_concentration"] <= 1.2 \
       and r["n_episodes"] >= 5:
        cons.append(f"回撤集中度{r['dd_concentration']:.2f}≈1，深度回撤是常态，风控系统性偏弱")
    if r.get("n_unrepaired"):
        cons.append(f"当前仍有{r['n_unrepaired']}个回撤episode未修复，损失是现实的而非历史的")
    if r["excess_mdd"] < -0.10:
        cons.append(f"超额回撤{r['excess_mdd']*100:.1f}%，曾大幅跑输基准")
    if r["down_cap"] == r["down_cap"] and r["down_cap"] >= 1.0:
        cons.append(f"下行捕获{r['down_cap']*100:.0f}%，跌时不比基准少")
    if r["uw_days_max"] > UW_SOFT_DAYS:
        cons.append(f"最长{r['uw_days_max']}天未创新高，修复慢")
    if r["n"] < MIN_SAMPLE_WEEKS:
        cons.append(f"样本仅{r['n']}周，不足一年，指标可信度低")
    if r.get("mkt_penalty"):
        cons.append("营销口径与实际净值严重不符，管理人沟通可信度存疑")
    return pros or ["各项指标居定制基准的中性水平"], cons


def portfolio_section(pool, gated):
    """组合视角: 敞口粗算 + 大跌情景压力测试(粗算)"""
    by = {r["name"]: r for r in pool}
    gy = by.get("国源拾金3号")
    beta_gy = (gy or {}).get("beta_hs300")
    if beta_gy is None or beta_gy != beta_gy:
        beta_gy = 0.37  # 缺数据时回退排排网官方披露beta
    L = ["## 组合视角（粗算，仅供压力感参考）\n",
         "持仓（排排账户截图口径）: 国源拾金3号 149.7万 / 龙旗红利科技轮动平衡5号 91.2万 / "
         "龙旗X计划12号1期 89.6万 / 现金宝 248.0万，合计 578.5万\n",
         f"- 国源权益敞口估算: 对沪深300周频beta≈{beta_gy:.2f}"
         f"（权益等效敞口≈beta×持仓≈{beta_gy * 149.7:.0f}万；"
         "黄金部分按与股市零相关粗算，不贡献权益敞口）",
         "- 两只龙旗同质性检验: " + longqi_homogeneity(by, gated),
         ""]
    # 情景: 沪深300 -10%
    guoyuan_loss = beta_gy * 0.10 * 149.7
    hl_loss = 0.10 * 91.2            # 红利轮动: 满仓指增
    x_loss_lo, x_loss_hi = 0.10 * 0.6 * 89.6, 0.10 * 89.6  # X计划: 择时6-7成~满仓
    lo, hi = guoyuan_loss + hl_loss + x_loss_lo, guoyuan_loss + hl_loss + x_loss_hi
    L.append("**情景: 沪深300单季-10%**（粗算，假设beta稳定、黄金持平、X计划仓位6-7成至满仓）\n")
    L.append(f"- 国源拾金3号: beta×-10%×149.7万 ≈ -{guoyuan_loss:.1f}万")
    L.append(f"- 龙旗红利科技轮动平衡5号: 满仓权益 ×-10% ≈ -{hl_loss:.1f}万")
    L.append(f"- 龙旗X计划12号1期: -{x_loss_lo:.1f}~-{x_loss_hi:.1f}万（动态仓位）")
    L.append("- 现金宝: ≈0")
    L.append(f"- **组合合计约 -{lo:.0f}~-{hi:.0f}万，占账户总额578.5万的 {lo/578.5*100:.1f}%~{hi/578.5*100:.1f}%**")
    L.append("- 敏感性: 若黄金同跌10%，国源再加约-4.5万；若beta上移至0.5，国源损失约-7.5万")
    L.append("")
    return L


def longqi_homogeneity(by, gated):
    x = by.get("龙旗X计划12号1期")
    if any(r["name"] == "龙旗红利科技轮动平衡5号" for r in gated):
        return ("红利科技轮动平衡5号净值被门控，无法计算两只龙旗的周频相关性；"
                "待数据可得后复算（>0.9 警告'重复持有同一赌注'）")
    return "数据不足"


def appendix_peer_section():
    """附录: 国源水下期(2021-12~2024-04)的市场环境与同业对比。

    市场环境用 data/idx_daily.json 本地指数日K; 同业用 data/simuwang/peer/*.json
    (fetch_peer_equity.py 抓取, 不存在则跳过本附录)。所有数字均由本地数据计算。
    """
    peer_dir = ROOT / "data" / "simuwang" / "peer"
    peers = sorted(peer_dir.glob("*.json")) if peer_dir.exists() else []
    if not peers:
        return []
    W0, W1 = "2021-12-01", "2024-04-30"
    idx_daily = json.loads((ROOT / "data" / "idx_daily.json").read_text(encoding="utf-8"))

    def window_stats(series):
        """series: [(date_str, nav)] -> 窗口区间收益/窗口内最大跌幅(窗口起点为峰值起点)/
        谷底日期/2024-04前是否创全历史新高(全历史高水位)/水下天数(高水位日期->修复或窗口末)"""
        pts = sorted((d, v) for d, v in series)
        pre = [(d, v) for d, v in pts if d < W0]
        win = [(d, v) for d, v in pts if W0 <= d <= W1]
        if not pre or not win:
            return None
        interval_ret = win[-1][1] / pre[-1][1] - 1
        peak, mdd, trough_d = pre[-1][1], 0.0, None
        for d, v in win:
            if v > peak:
                peak = v
            dd = v / peak - 1
            if dd < mdd:
                mdd, trough_d = dd, d
        # 全历史高水位: 创新高判定与水下天数(取窗口内最长水下段, 修复后重新计时)
        hwm_d, hwm = pts[0]
        uw_start, uw_best = None, (0, None, None)  # (天数, 峰值日, 修复日)
        for d, v in pts:
            if d > W1:
                break
            if v >= hwm:
                if uw_start is not None:
                    days = (date.fromisoformat(d) - date.fromisoformat(uw_start)).days
                    if days > uw_best[0]:
                        uw_best = (days, uw_start, d)
                    uw_start = None
                hwm_d, hwm = d, v
            elif uw_start is None and d >= W0:
                uw_start = hwm_d
        if uw_start is not None:  # 窗口末仍未修复
            days = (date.fromisoformat(W1) - date.fromisoformat(uw_start)).days
            if days > uw_best[0]:
                uw_best = (days, uw_start, None)
        uw_days, _uw_from, repair_d = uw_best
        repaired = repair_d is not None or uw_days == 0
        return dict(interval_ret=interval_ret, mdd=mdd, trough=trough_d,
                    repaired=repaired, repair_d=repair_d, uw_days=uw_days)

    # 国源同窗口(先算, 供标题与结论引用)
    _, gy_ns = load_nav("国源拾金3号")
    gy = window_stats([(str(d), v) for d, v in gy_ns])
    L = ["## 附录：国源水下期（2021-12~2024-04）的市场环境与同业对比\n",
         f"> 问题：国源拾金3号最长水下{gy['uw_days']}天（2021-12~2024-04）是个案还是行业现象？"
         "全部数字由本地数据计算（指数: 新浪日K；同业: 排排网周频净值）\n",
         "### 同期市场行情\n",
         "| 指数 | 区间收益 | 期间最大跌幅 | 谷底日期 | 2022 | 2023 | 2024前4月 |",
         "|---|---|---|---|---|---|---|"]
    for key, cn in [("hs300", "沪深300"), ("zz500", "中证500"), ("zz1000", "中证1000"),
                    ("cyb", "创业板指"), ("cndiv", "红利ETF"), ("gold518880", "黄金ETF")]:
        daily = idx_daily.get(key)
        if not daily:
            continue
        st = window_stats(list(daily.items()))
        def yret(y0, y1):
            p = sorted((d, c) for d, c in daily.items() if y0 <= d <= y1)
            b = [c for d, c in sorted(daily.items()) if d < y0][-1]
            return p[-1][1] / b - 1
        L.append(f"| {cn} | {st['interval_ret']*100:+.1f}% | {st['mdd']*100:.1f}% | "
                 f"{st['trough']} | {yret('2022-01-01','2022-12-31')*100:+.1f}% | "
                 f"{yret('2023-01-01','2023-12-31')*100:+.1f}% | "
                 f"{yret('2024-01-01','2024-04-30')*100:+.1f}% |")
    # 行情阶段佐证数字: 2024-01 微盘股流动性危机 + 2024-02 反弹
    def mret(key, m0, m1):
        daily = idx_daily[key]
        p = sorted((d, c) for d, c in daily.items() if m0 <= d <= m1)
        b = [c for d, c in sorted(daily.items()) if d < m0][-1]
        return p[-1][1] / b - 1
    L += ["",
          f"行情阶段（数据佐证）: ①2022全年下跌（沪深300 {mret('hs300','2022-01-01','2022-12-31')*100:+.1f}%，"
          f"创业板 {mret('cyb','2022-01-01','2022-12-31')*100:+.1f}%）；"
          f"②2023阴跌（沪深300 {mret('hs300','2023-01-01','2023-12-31')*100:+.1f}%）；"
          f"③2024年1月微盘股流动性危机（中证1000单月 {mret('zz1000','2024-01-01','2024-01-31')*100:+.1f}%，"
          f"中证500 {mret('zz500','2024-01-01','2024-01-31')*100:+.1f}%）；"
          f"④2024年2月国家队入场后反弹（中证1000 {mret('zz1000','2024-02-01','2024-02-29')*100:+.1f}%，"
          f"沪深300 {mret('hs300','2024-02-01','2024-02-29')*100:+.1f}%）→ 4月新国九条。"
          f"同期避险资产大涨（黄金ETF区间 {window_stats(list(idx_daily['gold518880'].items()))['interval_ret']*100:+.1f}%），"
          "红利资产为正——国源的重仓方向（黄金+低估值）正是该窗口的强势资产，但其股票部分仍受大盘拖累",
          ""]
    # 国源同窗口(已在上方计算)
    L += ["### 同业对比（2021-12-01 ~ 2024-04-30 窗口）\n",
          "| 产品 | 管理人 | 区间收益 | 窗口最大回撤 | 谷底 | 2024-04前修复创新高 | 水下天数 |",
          "|---|---|---|---|---|---|---|",
          f"| **国源拾金3号** | 国源信达 | {gy['interval_ret']*100:+.1f}% | {gy['mdd']*100:.1f}% | "
          f"{gy['trough']} | {'是（' + str(gy['repair_d']) + '）' if gy['repaired'] else '否'} | {gy['uw_days']} |"]
    rows = []
    for p in peers:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        ns = [(x["date"], float(x["cum_nav"])) for x in (d.get("nav_series") or [])
              if x.get("cum_nav") is not None]
        st = window_stats(ns)
        if not st:
            continue
        bi = (d.get("detail") or {}).get("baseInfo") or {}
        comp = ((d.get("detail") or {}).get("companyInfo") or {}).get("company_short_name", "")
        rows.append((p.stem, comp, st))
    for name, comp, st in rows:
        L.append(f"| {name} | {comp} | {st['interval_ret']*100:+.1f}% | {st['mdd']*100:.1f}% | "
                 f"{st['trough']} | {'是（' + str(st['repair_d']) + '）' if st['repaired'] else '否'} "
                 f"| {st['uw_days']} |")
    L.append("")
    # 结论
    if rows:
        mdds = [st["mdd"] for _, _, st in rows]
        uws = [st["uw_days"] for _, _, st in rows]
        n_rep = sum(1 for _, _, st in rows if st["repaired"])
        med_mdd = statistics.median(mdds)
        med_uw = statistics.median(uws)
        L += [f"### 结论：国源水下{gy['uw_days']}天是个案还是行业现象\n",
              f"- 同业（{len(rows)}只有数据）窗口最大回撤中位数 {med_mdd*100:.1f}%，"
              f"水下天数中位数 {med_uw:.0f} 天，2024-04前修复创新高 {n_rep}/{len(rows)}；"
              f"国源窗口最大回撤 {gy['mdd']*100:.1f}%，水下 {gy['uw_days']} 天，"
              f"{'已于' + str(gy['repair_d']) + '修复' if gy['repaired'] else '窗口内未修复'}",
              "- " + ("同业普遍回撤更深、修复更晚 → 国源的防守在同业中偏上，水下长主要是行业贝塔问题"
                      if gy["mdd"] > med_mdd and gy["uw_days"] <= med_uw else
                      "同业回撤与国源相当但修复时间相近/更晚 → 水下长是行业贝塔与个股选择的混合"
                      if gy["uw_days"] <= med_uw else
                      "同业明显更早修复 → 国源水下长更多是个体问题"),
              f"- 与用户12个月水下容忍度（软指标）的关系: {gy['uw_days']}天≈{gy['uw_days']//30}个月，远超容忍度；"
              f"即便属行业现象，该窗口主观股多普遍水下{med_uw:.0f}天量级，"
              "说明'主观股多+长水下'是此类资产的系统性特征，配置比例应据此控制",
              ""]
    return L


if __name__ == "__main__":
    main()
