#!/usr/bin/env python3
"""基于蛋卷货架季度收益序列, 计算36只CTA私募的筛选指标并排名。

数据源: danjuan_cta_page*.json 中 fund_index_info.data_list (逐季度收益率)
输出: danjuan_cta_metrics.csv + 终端排名表

口径说明:
- 季度频率, 年化波动=季度std*2, 夏普=(年化收益-RF)/年化波动, RF=1.5%
- 最大回撤基于季度末累计净值(季度内回撤不可见, 实际值会更大)
- 新高占比 = 季末净值创历史新高的季度数 / 总季度数
- data_list 为最新季度在前(倒序), 已反转还原; 序列覆盖至当前季度2026Q3
  (与排排网净值逐季核对确认, 修复前按正序对齐导致回撤形态/新高/危机阿尔法/相关性全部错位)
- 分红口径风险: 蛋卷季度序列与排排网复权净值存在口径/份额类别不一致的产品
  (远澜银杏1号 37季0匹配、均成CTA增强25号1期 0/13、因诺CTA2号B 仅3/16匹配,
  排排网detail中无显式分红/复权字段, 疑为分红再投资处理或份额类别差异),
  季度口径仅作辅助参考, 最终结论以排排网周频口径(simuwang_weekly_report.md)为准
"""
import csv
import json
import math
import os
import statistics
from datetime import date

RF = 0.015  # 无风险利率(年化)
COMMON_Q = 8  # 同周期对比窗口: 最近8个季度(约2年)


def load_funds():
    funds = []
    for p in ("data/danjuan_cta_page1.json", "data/danjuan_cta_page2.json"):
        d = json.load(open(p, encoding="utf-8"))
        funds.extend(d["data"]["fund_datas"])
    return funds


def metrics(rets):
    n = len(rets)
    if n < 4:
        return None
    # 累计净值
    nav, curve = 1.0, [1.0]
    for r in rets:
        nav *= 1 + r
        curve.append(nav)
    total_ret = curve[-1] / curve[0] - 1
    years = n / 4
    ann_ret = (1 + total_ret) ** (1 / years) - 1
    q_std = statistics.stdev(rets)
    ann_vol = q_std * 2
    sharpe = (ann_ret - RF) / ann_vol if ann_vol else float("nan")
    # 最大回撤(季度末)
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    calmar = ann_ret / abs(mdd) if mdd else float("nan")
    # 下行偏差(MAR=0, 季度) -> 年化
    downside = [min(r, 0.0) for r in rets]
    dd_ann = math.sqrt(sum(d * d for d in downside) / n) * 2
    sortino = (ann_ret - RF) / dd_ann if dd_ann else float("nan")
    win = sum(1 for r in rets if r > 0) / n
    new_high = sum(1 for i, v in enumerate(curve[1:], 1) if v >= max(curve[:i])) / n
    skew = (sum((r - statistics.mean(rets)) ** 3 for r in rets) / n) / (q_std ** 3) if q_std else 0.0
    srt = sorted(rets)
    var5 = srt[max(0, int(0.05 * n))]  # 最差5%分位
    es5 = statistics.mean(srt[: max(1, int(0.05 * n) + 1)])  # 尾部期望
    # 盈亏比
    pos = [r for r in rets if r > 0]; neg = [r for r in rets if r < 0]
    pl_ratio = (statistics.mean(pos) / abs(statistics.mean(neg))) if pos and neg else float("nan")
    # 回撤形态: 最长水下时长 / 当前水下 / 最长连亏 / 最深单季
    uw, uw_max = 0, 0
    peak = curve[0]
    for v in curve[1:]:
        if v >= peak:
            peak, uw = v, 0
        else:
            uw += 1; uw_max = max(uw_max, uw)
    cur_uw = 1 if curve[-1] < max(curve[:-1]) else 0
    # 当前水下需连续计算
    pk = curve[0]; cu = 0
    for v in curve[1:]:
        if v >= pk: pk, cu = v, 0
        else: cu += 1
    streak, max_streak = 0, 0
    for r in rets:
        if r < 0: streak += 1; max_streak = max(max_streak, streak)
        else: streak = 0
    return dict(n=n, total_ret=total_ret, ann_ret=ann_ret, ann_vol=ann_vol,
                sharpe=sharpe, mdd=mdd, calmar=calmar, sortino=sortino,
                win=win, new_high=new_high, skew=skew, var5=var5, es5=es5,
                worst=srt[0], pl_ratio=pl_ratio, uw_max=uw_max, uw_cur=cu,
                streak_max=max_streak)


def crisis_metrics(rets, qkeys, crisis_set):
    """危机阿尔法: 危机季度=沪深300/中证500/创业板指任一个当季下跌"""
    down_keys = [k for k in qkeys if k in crisis_set]
    if len(qkeys) < 4 or not down_keys:
        return {}
    fr = dict(zip(qkeys, rets))
    down = [fr[k] for k in down_keys]
    return dict(
        crisis_n=len(down),
        crisis_avg=statistics.mean(down),
        crisis_win=sum(1 for f in down if f > 0) / len(down),
    )


def _reasons(r):
    """根据指标自动生成适配理由(pros)与风险点(cons)"""
    pros, cons = [], []
    if r["_v3_sortino"] >= 0.8:
        pros.append(f"索提诺 {r['sortino']:.1f}（池内前20%），下行偏差仅 {r.get('dd_ann', 0)*100:.1f}%" if r.get("dd_ann") else f"索提诺 {r['sortino']:.1f}（池内前20%）")
    if r.get("crisis_low_n"):
        cons.append(f"危机季度样本不足（仅{r.get('crisis_n',0)}个），危机指标不参与评价")
    elif r.get("crisis_win", 0) >= 0.99:
        pros.append(f"危机季度全胜（{r['crisis_n']}个危机季度均正收益，平均{r['crisis_avg']*100:+.1f}%）")
    elif r.get("crisis_win", 0) >= 0.75:
        pros.append(f"危机胜率 {r['crisis_win']*100:.0f}%，危机均季 {r['crisis_avg']*100:+.1f}%")
    if r["uw_max"] <= 1:
        pros.append("回撤从未超过一个季度即修复")
    if r["_v3_pl_ratio"] >= 0.8:
        pros.append(f"盈亏比 {r['pl_ratio']:.1f}，尾部风险在上涨侧")
    if r["_v3_new_high"] >= 0.8:
        pros.append(f"新高占比 {r['new_high']*100:.0f}%，持有体验好")
    if r["age"] >= 7:
        pros.append(f"{r['age']}年长样本，业绩可信度高")
    if r["age"] < 3:
        cons.append(f"成立仅{r['age']}年，未经历完整商品周期，可靠性折扣后排名被压低")
    if r["uw_max"] >= 9:
        cons.append(f"最长{r['uw_max']}个季度未创新高，持有体验差")
    if r["mdd"] <= -0.15:
        cons.append(f"季度口径最大回撤已达{r['mdd']*100:.0f}%，真实回撤更深")
    if r.get("crisis_win", 1) < 0.55:
        cons.append(f"危机胜率仅{r['crisis_win']*100:.0f}%，危机保护能力弱")
    if r["pl_ratio"] < 1.5:
        cons.append(f"盈亏比仅{r['pl_ratio']:.1f}，赚小亏大")
    if r["sharpe"] < 0.8:
        cons.append(f"夏普{r['sharpe']:.2f}偏低")
    if "c_sharpe" in r and r["sharpe"] > 0 and r["c_sharpe"] < r["sharpe"] * 0.7:
        cons.append("近2年收益明显钝化（同周期窗口夏普低于全历史）")
    if r.get("mgr_unverified") or (r.get("mgr") and r.get("mgr_aum") is None):
        cons.append(f"管理人（{r.get('mgr', '?')}）成立年份/规模未核实，需补协会备案数据")
    if r.get("mgr_note"):
        cons.append(f"管理人合规关注: {r['mgr_note']}")
    return pros or ["各项指标均居中游"], cons


def generate_report(pool, excluded):
    """每次评估自动生成报告: 每只产品的适配/排除理由(适配度框架, 不作买卖建议)"""
    L = ["# CTA 私募适配度评估报告",
         f"> 生成时间: {date.today()} | 数据窗口: 截至2026Q3 | 评分池 {len(pool)} 只 / 排除 {len(excluded)} 只",
         "> 方法见 cta_evaluation_plan.md（v3: 门槛过滤 + 指标加权 + 短样本可靠性折扣）",
         "> 分层为适配度评级（与投资者画像的匹配程度），不构成投资建议",
         "> 注意: 蛋卷季度序列存在分红口径/份额类别不一致风险（远澜银杏1号、均成CTA增强25号1期、"
         "因诺CTA2号B 与排排网净值对不上），季度口径仅作辅助，以排排网周频口径为准"
         "（量派CTA七号C，见方案文档第五节与 simuwang_weekly_report.md）", ""]
    tiers = [("高适配（核心候选）", 0.70, 99), ("中适配（备选）", 0.62, 0.70),
             ("观察", 0.50, 0.62), ("低适配", -1, 0.50)]
    # F3整改: 高适配层需同时过绝对阈值线, 避免纯相对排名失真
    def pass_absolute(r):
        if r["sharpe"] <= 1.0:
            return False, f"夏普{r['sharpe']:.2f}未达1.0"
        if r["mdd"] < -0.15:
            return False, f"季度口径回撤{r['mdd']*100:.0f}%超-15%"
        if not r.get("crisis_low_n") and r.get("crisis_win", 0) < 0.6:
            return False, f"危机胜率{r.get('crisis_win',0)*100:.0f}%未达60%"
        return True, ""
    for title, lo, hi in tiers:
        grp = []
        for r in pool:
            if not (lo <= r["score_v3"] < hi):
                continue
            if title.startswith("高适配"):
                ok, why = pass_absolute(r)
                if not ok:
                    r["_demoted"] = why
                    continue
            grp.append(r)
        if title.startswith("中适配"):
            grp += [r for r in pool if r.get("_demoted") and r["score_v3"] >= 0.70]
        if not grp:
            continue
        L.append(f"## {title}（{len(grp)}只）\n")
        for i, r in enumerate(grp, 1):
            pros, cons = _reasons(r)
            L.append(f"### {r['name']}（{r['symbol']}，{r['age']}年，适配度得分{r['score_v3']:.2f}）")
            L.append(f"- 年化{r['ann_ret']*100:+.1f}% / 回撤{r['mdd']*100:.1f}%* / 夏普{r['sharpe']:.2f} / "
                     f"索提诺{r['sortino']:.2f} / 胜率{r['win']*100:.0f}% / 盈亏比{r['pl_ratio']:.2f} / "
                     f"新高{r['new_high']*100:.0f}% / 危机均季{r.get('crisis_avg', float('nan'))*100:+.1f}% / "
                     f"危机胜率{r.get('crisis_win', float('nan'))*100:.0f}% / 最长水下{r['uw_max']}季 / "
                     f"与沪深300相关性{r.get('mkt_corr', 0):+.2f}")
            if r.get("mgr"):
                mgr_desc = r["mgr"]
                extra = []
                if r.get("mgr_found"):
                    extra.append(f"{r['mgr_found']}年成立")
                if r.get("mgr_aum_text"):
                    extra.append(f"规模{r['mgr_aum_text']}")
                L.append(f"- 管理人: {mgr_desc}" + (f"（{'，'.join(extra)}）" if extra else ""))
            if r.get("pm_start"):
                L.append(f"- 指标统计区间: 现任基金经理 {r.get('pm', '?')} 任职期（{r['pm_start']} 起，{r['n']}个季度）")
            if r.get("_demoted"):
                L.append(f"- ⚠ 得分达高适配线但未过绝对阈值（{r['_demoted']}），列入备选")
            L.append("- 适配理由: " + "；".join(pros))
            if cons:
                L.append("- 风险点: " + "；".join(cons))
            L.append("")
    L.append("## 排除名单（不参与评分）\n")
    for name, sym, reason in excluded:
        L.append(f"- **{name}**（{sym}）：{reason}")
    L.append("")
    L.append("\\* 回撤为季度口径，真实最大回撤约为该值的2~4倍（排排网高频数据实证）")
    L.append("")
    L.append("---")
    L.append("> 免责声明： 本报告仅作信息整理与适配度分析，不构成任何投资建议、要约或收益承诺。"
             "私募证券基金过往业绩不预示未来表现，投资者应自行承担投资风险。"
             "数据来源于蛋卷基金公开货架信息及第三方平台截图，可能存在口径偏差或滞后，"
             "最终以基金管理人正式披露文件及基金合同为准。合格投资者认定与适当性匹配请以持牌销售机构流程为准。")
    with open("docs/danjuan_cta_report.md", "w", encoding="utf-8") as fp:
        fp.write("\n".join(L))
    print(f"\n报告已生成: danjuan_cta_report.md（{len(pool)}只评分 + {len(excluded)}只排除）")


def main():
    info = {i["symbol"]: i for i in json.load(open("data/danjuan_cta_info.json", encoding="utf-8"))}
    hs300 = json.load(open("data/hs300_quarterly.json", encoding="utf-8"))
    multi = json.load(open("data/idx_quarterly_multi.json", encoding="utf-8"))
    # 管理人(公司)层面信息: 成立年份/管理规模, 用于公司层面门槛过滤
    # data/manager_info.json 格式: {"公司全名": {"found_year": 2014, "aum_yi": 100, "aum_text": "100亿+", "source": "URL"},
    #   "_symbol_override": {"基金代码": "公司全名"}  # 货架 keeper 字段缺失/错误时按代码指定
    mgr_info = {}
    mgr_override = {}
    if os.path.exists("data/manager_info.json"):
        raw = json.load(open("data/manager_info.json", encoding="utf-8"))
        mgr_override = raw.pop("_symbol_override", {})
        mgr_info = raw
    MGR_MIN_AGE = 5    # 管理人成立不足5年剔除
    MGR_MIN_AUM = 10   # 管理规模不足10亿剔除(亿元); 规模未知不剔除但标记
    # 现任基金经理任职区间(who-is-the-best-manager 借鉴: 只统计在任期间业绩)
    # data/pm_tenure.json 格式: {"代码": {"pm": "姓名", "pm_start": "YYYY-MM-DD"}}
    pm_tenure = json.load(open("data/pm_tenure.json", encoding="utf-8")) \
        if os.path.exists("data/pm_tenure.json") else {}
    # 危机季度: 沪深300/中证500/中证1000/创业板指 任一个当季跌超-3%
    crisis_set = {k for k, v in hs300.items() if v < -0.03}
    for idx in multi.values():
        crisis_set |= {k for k, v in idx.items() if v < -0.03}
    crisis_set = {k for k in crisis_set if k <= "2026Q3"}
    print(f"危机季度(四指数并集): {len(crisis_set)} 个")
    all_q = [k for k in hs300 if k <= "2026Q3"]  # 基金季度序列末项=2026Q3
    today = date.today()
    rows = []
    excluded = []  # (名称, 代码, 理由)
    NOT_PURE_CTA = {"中性": "中性多策略非CTA: 主体为股票中性/多策略, 非管理期货策略",
                    "指数": "被动指数/指数增强: 非主动管理CTA, 无双向多空能力",
                    "指增": "指数增强: 收益主体为股票beta+alpha, 无危机保护功能"}
    for f in load_funds():
        kw = next((k for k in NOT_PURE_CTA if k in f["fund_name"]), None)
        if kw:
            excluded.append((f["fund_name"], f["symbol"], NOT_PURE_CTA[kw]))
            continue
        # 管理人(公司)门槛: 成立年份过短 / 规模过小 直接剔除
        mgr_name = mgr_override.get(f["symbol"]) or (info.get(f["symbol"]) or {}).get("keeper", "")
        mi = mgr_info.get(mgr_name)
        if mi is None and mgr_name:
            # 货架 keeper 字段可能截断(如"因诺（上海）资产管理有限"), 前缀匹配补全
            k = next((k for k in mgr_info if k.startswith(mgr_name) or mgr_name.startswith(k)), None)
            if k:
                mgr_name, mi = k, mgr_info[k]
        if mi:
            if mi.get("found_year") and today.year - mi["found_year"] < MGR_MIN_AGE:
                excluded.append((f["fund_name"], f["symbol"],
                                 f"管理人门槛: {mgr_name} 成立于{mi['found_year']}年, "
                                 f"不足{MGR_MIN_AGE}年, 公司存续期太短"))
                continue
            if mi.get("aum_yi") is not None and mi["aum_yi"] < MGR_MIN_AUM:
                excluded.append((f["fund_name"], f["symbol"],
                                 f"管理人门槛: {mgr_name} 管理规模约{mi['aum_yi']}亿, "
                                 f"低于{MGR_MIN_AUM}亿, 抗风险能力与运营稳定性不足"))
                continue
        dl = f["fund_index_info"]["data_list"]
        if dl and "percent" not in dl[0]:
            # fund_point 字段 = 新基金周度净值点, 口径不同, 剔除
            reason = "新基金样本不足: 成立不足1年, 仅有周度数据, 且与App展示收益存在矛盾, 列入观察名单"
            print(f"[剔除] {f['fund_name']}: {reason}")
            excluded.append((f["fund_name"], f["symbol"], reason))
            continue
        rets = [float(x["percent"]) for x in dl][::-1]  # data_list为倒序(最新季度在前), 反转还原
        qkeys = all_q[-len(rets):]
        # 任职区间过滤: 有PM任职起点时, 仅统计其任职后的季度
        ten = pm_tenure.get(f["symbol"])
        if ten and ten.get("pm_start"):
            y, mo = map(int, ten["pm_start"].split("-")[:2])
            start_q = f"{y}Q{(mo - 1) // 3 + 1}"
            pairs = [(k, r) for k, r in zip(qkeys, rets) if k >= start_q]
            if len(pairs) < 4:
                excluded.append((f["fund_name"], f["symbol"],
                                 f"现任基金经理({ten.get('pm', '?')}){ten['pm_start']}任职以来仅"
                                 f"{len(pairs)}个季度(<4), 任职期样本不足"))
                continue
            qkeys = [k for k, _ in pairs]
            rets = [r for _, r in pairs]
        m = metrics(rets)
        if not m:
            excluded.append((f["fund_name"], f["symbol"],
                             f"样本不足: 仅{len(rets)}个季度(<4), 无法计算指标"))
            continue
        m.update(name=f["fund_name"], symbol=f["symbol"],
                 ta=f["ta_private_fund_code"],
                 summary=f["fund_summary_info"].get("first_format_value", ""))
        m["mgr"] = mgr_name
        if mi:
            m["mgr_found"] = mi.get("found_year")
            m["mgr_aum"] = mi.get("aum_yi")
            m["mgr_aum_text"] = mi.get("aum_text", "")
            if mi.get("note"):
                m["mgr_note"] = mi["note"]
        else:
            m["mgr_unverified"] = True
        # 成立年数: 优先用接口的成立日期, 否则用季度数/4
        fd = (info.get(f["symbol"]) or {}).get("found", "")
        if fd:
            y, mo, d = map(int, fd.split("-"))
            m["age"] = round((today - date(y, mo, d)).days / 365.25, 1)
        else:
            m["age"] = round(m["n"] / 4, 1)
        # 同周期窗口: 最近 COMMON_Q 个季度
        c = metrics(rets[-COMMON_Q:]) if len(rets) >= COMMON_Q else None
        if c:
            m.update(c_ann=c["ann_ret"], c_mdd=c["mdd"], c_sharpe=c["sharpe"],
                     c_win=c["win"], c_new_high=c["new_high"])
        # 危机阿尔法: 与指数季度序列逐季对齐(qkeys 已在任职区间过滤时确定)
        if ten and ten.get("pm_start"):
            m.update(pm=ten.get("pm", ""), pm_start=ten["pm_start"])
        m.update(crisis_metrics(rets, qkeys, crisis_set))
        # 股票敞口检测: 与沪深300/中证500/中证1000/创业板指 季度收益相关系数取最大(S3整改)
        corrs = {}
        for iname, idata in [("hs300", hs300)] + list(multi.items()):
            aligned = [(r, idata[k]) for r, k in zip(rets, qkeys) if k in idata]
            if len(aligned) >= 4:
                fr = [a for a, _ in aligned]; mr = [b for _, b in aligned]
                mf, mm = statistics.mean(fr), statistics.mean(mr)
                sd = statistics.pstdev(fr) * statistics.pstdev(mr)
                if sd:
                    corrs[iname] = sum((a-mf)*(b-mm) for a, b in aligned) / len(aligned) / sd
        m["mkt_corr_detail"] = corrs
        m["mkt_corr"] = max(corrs.values()) if corrs else 0.0
        rows.append(m)

    # 综合评分v2: 索提诺/危机阿尔法/新高占比/盈亏比/回撤形态 为主, 夏普/卡玛为辅
    def rank_score(rs):
        def pct(key, reverse=False):
            vals = sorted(r[key] for r in rs if key in r and r[key] == r[key])
            if not vals:
                return
            for r in rs:
                if key not in r or r[key] != r[key]:
                    r["_s_" + key] = 0.5  # 缺失取中位
                else:
                    rk = vals.index(r[key]) / max(len(vals) - 1, 1)
                    r["_s_" + key] = 1 - rk if reverse else rk
        for k in ["sortino", "crisis_avg", "new_high", "pl_ratio", "sharpe",
                  "calmar", "crisis_win"]:
            pct(k)
        for k in ["uw_max", "streak_max"]:  # 越小越好
            pct(k, reverse=True)
        for r in rs:
            r["score"] = (0.20 * r["_s_sortino"] + 0.15 * r["_s_crisis_avg"]
                          + 0.15 * r["_s_new_high"] + 0.15 * r["_s_pl_ratio"]
                          + 0.10 * r["_s_sharpe"] + 0.05 * r["_s_calmar"]
                          + 0.05 * r["_s_crisis_win"]
                          + 0.10 * r["_s_uw_max"] + 0.05 * r["_s_streak_max"])

    rank_score(rows)
    rows.sort(key=lambda r: -r["score"])

    # ===== 优化方案 v3: 门槛 + 加权 + 可靠性折扣 =====
    CORR_LIMIT = 0.30  # 与沪深300相关性≥0.30 视为股票敞口过高
    pool = []
    for r in rows:
        if r.get("mkt_corr", 0) >= CORR_LIMIT:
            reason = f"股票敞口过高: 与股票指数(沪深300/中证500/中证1000/创业板)最大相关性 {r['mkt_corr']:+.2f} ≥ {CORR_LIMIT}, 危机场景下可能跟随股市亏损"
            print(f"[剔除] {r['name']}: {reason}")
            excluded.append((r["name"], r["symbol"], reason))
            continue
        pool.append(r)

    def v3_pct(rs, key, reverse=False):
        vals = sorted(r[key] for r in rs if r.get(key) is not None and r[key] == r[key])
        for r in rs:
            v = r.get(key)
            if v is None or v != v or not vals:
                r["_v3_" + key] = 0.5
            else:
                rk = vals.index(v) / max(len(vals) - 1, 1)
                r["_v3_" + key] = 1 - rk if reverse else rk

    for k in ["sortino", "crisis_avg", "crisis_win", "new_high", "pl_ratio",
              "sharpe", "calmar", "win"]:
        v3_pct(pool, k)
    for k in ["uw_max", "streak_max"]:
        v3_pct(pool, k, reverse=True)

    MIN_CRISIS_N = 4  # 危机季度样本门槛: 不足则危机指标按中性0.5处理(F2整改)
    for r in pool:
        if r.get("crisis_n", 0) < MIN_CRISIS_N:
            r["_v3_crisis_avg"] = 0.5
            r["_v3_crisis_win"] = 0.5
            r["crisis_low_n"] = True

    for r in pool:
        raw = (0.25 * r["_v3_sortino"]
               + 0.10 * r["_v3_crisis_avg"] + 0.10 * r["_v3_crisis_win"]
               + 0.10 * r["_v3_uw_max"] + 0.05 * r["_v3_streak_max"]
               + 0.10 * r["_v3_new_high"] + 0.10 * r["_v3_pl_ratio"]
               + 0.10 * r["_v3_sharpe"] + 0.05 * r["_v3_calmar"]
               + 0.05 * r["_v3_win"])
        reliability = min(1.0, r["n"] / 24)  # 6年(24季)满信度
        r["score_v3"] = raw * reliability + 0.5 * (1 - reliability)
        r["raw_v3"] = raw
    pool.sort(key=lambda r: -r["score_v3"])

    print("\n=== 优化方案v3: 剔除假CTA + 新权重 + 短样本折扣 ===")
    print(f"{'排名':<3}{'产品':<15}{'年数':>4} {'索提诺':>6} {'危机均季':>7} {'危机胜率':>6} {'水下':>4} {'新高%':>6} {'盈亏比':>6} {'夏普':>6} {'原始分':>6} {'折扣后':>6}")
    for i, r in enumerate(pool, 1):
        print(f"{i:<4}{r['name'][:13]:<15}{r['age']:>4.1f} {r['sortino']:>6.2f} "
              f"{r.get('crisis_avg', float('nan'))*100:>6.1f}% {r.get('crisis_win', float('nan'))*100:>5.0f}% "
              f"{r['uw_max']:>3d}季 {r['new_high']*100:>5.0f}% {r['pl_ratio']:>6.2f} "
              f"{r['sharpe']:>6.2f} {r['raw_v3']:>6.2f} {r['score_v3']:>6.2f}")
    with open("data/danjuan_cta_metrics_v3.csv", "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=list(pool[0].keys()), extrasaction="ignore")
        w.writeheader()
        for r in pool:
            w.writerow(r)

    generate_report(pool, excluded)

    fields = ["name", "symbol", "n", "age", "ann_ret", "mdd", "calmar", "sharpe",
              "sortino", "win", "new_high", "skew", "var5", "es5", "worst",
              "pl_ratio", "uw_max", "uw_cur", "streak_max",
              "mkt_corr", "crisis_n", "crisis_avg", "crisis_win", "crisis_big_avg",
              "ann_vol", "total_ret", "score", "summary", "ta",
              "c_ann", "c_mdd", "c_sharpe", "c_win", "c_new_high"]
    with open("data/danjuan_cta_metrics.csv", "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"{'排名':<3}{'产品':<15}{'年数':>4} {'年化':>7} {'回撤':>7} {'索提诺':>6} {'新高%':>6} {'盈亏比':>6} {'危机均季':>7} {'危机胜率':>6} {'最长水下':>6} {'最长连亏':>6} {'综合':>5}")
    for i, r in enumerate(rows, 1):
        print(f"{i:<4}{r['name'][:13]:<15}{r['age']:>4.1f} "
              f"{r['ann_ret']*100:>6.1f}% {r['mdd']*100:>6.1f}% {r['sortino']:>6.2f} "
              f"{r['new_high']*100:>5.0f}% {r['pl_ratio']:>6.2f} "
              f"{r.get('crisis_avg', float('nan'))*100:>6.1f}% {r.get('crisis_win', float('nan'))*100:>5.0f}% "
              f"{r['uw_max']:>5d}季 {r['streak_max']:>5d}季 {r['score']:>5.2f}")

    # 同周期对比: 最近 COMMON_Q 个季度, 仅含样本足够的产品
    common = [r for r in rows if "c_ann" in r]
    common.sort(key=lambda r: -r["c_sharpe"])
    print(f"\n=== 同周期对比: 最近 {COMMON_Q} 个季度(约{COMMON_Q//4}年), 按夏普排序, 共{len(common)}只 ===")
    print(f"{'产品':<16}{'年化':>7} {'回撤':>7} {'夏普':>6} {'胜率':>6} {'新高%':>6}")
    for r in common:
        print(f"{r['name'][:14]:<16}{r['c_ann']*100:>6.1f}% {r['c_mdd']*100:>6.1f}% "
              f"{r['c_sharpe']:>6.2f} {r['c_win']*100:>5.0f}% {r['c_new_high']*100:>5.0f}%")


if __name__ == "__main__":
    main()
