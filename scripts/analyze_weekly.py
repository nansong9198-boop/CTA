#!/usr/bin/env python3
"""基于排排网周/日频净值序列, 重算CTA私募筛选指标并排名(高频口径)。

数据源: data/simuwang/<产品名>.json 中 nav_series (累计净值, 周频或日频)
输出: docs/simuwang_weekly_report.md + data/simuwang_weekly_metrics.csv + 终端排名表

口径说明:
- 所有序列统一重采样为周频: 按ISO周分组取每周最后一个净值, 年化 = n/52
  (瑞达为真实日频; 明睿骁云/量派聚核20号名义日频实为周频+零星日内披露, 重采样后口径一致)
- 最大回撤为周频真实回撤, 可见季度内回撤, 与 analyze_cta.py 的季度口径不可直接比分
- 夏普=(年化收益-RF)/年化波动, RF=1.5%; 最长水下期以周数计, 另报最长不创新高天数
- 危机阿尔法: 周净值聚合为自然季度收益, 危机季度集合与 analyze_cta.py 一致
  (沪深300/中证500/中证1000/创业板指任一个当季跌超-3%, 上限2026Q3)
"""
import csv
import glob
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drawdown_utils import drawdown_shape  # 回撤分布口径与股票类体系共用(2026-10-07统一)

RF = 0.015  # 无风险利率(年化)
WEEKS_PER_YEAR = 52
MIN_TENURE_WEEKS = 26   # 现任基金经理任职以来不足26周(半年)剔除
FULL_RELIABILITY_WEEKS = 312  # 6年满信度(对应 analyze_cta.py 的24季)
CORR_LIMIT = 0.30  # 与股票指数最大相关性≥0.30 视为股票敞口过高
MIN_CRISIS_N = 4   # 危机季度样本门槛: 不足则危机指标按中性0.5处理
MGR_MIN_AGE = 5    # 管理人成立不足5年剔除
MGR_MIN_AUM = 10   # 管理规模不足10亿剔除(亿元)
MAX_MIN_INV = 200  # 起购金额上限(万元, 2026-10-07用户拍板; 量派CTA七号C 500万起购触发此约束)
# 用户股票类持仓(非CTA候选), 不进入本评估池
NON_CTA_HOLDINGS = {"国源拾金3号", "龙旗红利科技轮动平衡5号",
                    "龙旗X计划12号1期", "龙旗红利科技轮动平衡5号1期"}
# 同策略验证对(货架外候选 vs 已验证基准产品): 重叠窗口周频相关性>0.9视为同策略实锤
SAME_STRATEGY_PAIRS = [("量派CTA十号2期", "量派CTA七号C"),
                       ("量派CTA十号2期C类份额", "量派CTA十号2期")]


def load_danjuan_symbols():
    """蛋卷产品名 -> 基金代码(symbol)"""
    m = {}
    for p in ("data/danjuan_cta_page1.json", "data/danjuan_cta_page2.json"):
        d = json.load(open(p, encoding="utf-8"))
        for f in d["data"]["fund_datas"]:
            m[f["fund_name"]] = f["symbol"]
    return m


def match_symbol(name, name2symbol):
    """排排网文件名(=蛋卷产品名)匹配基金代码; 蛋卷名可能带'集合资产管理计划'后缀"""
    if name in name2symbol:
        return name2symbol[name]
    for dn, sym in name2symbol.items():
        if dn.startswith(name) or name.startswith(dn):
            return sym
    return ""


def load_summary():
    """summary.csv: 产品名 -> 附加信息(策略/净值频率/成立日期)"""
    info = {}
    p = "data/simuwang/summary.csv"
    if not os.path.exists(p):
        return info
    with open(p, encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            info[row["产品名"]] = row
    return info


def resample_weekly(series):
    """[(date, nav), ...] -> 周频序列: 每个ISO周取最后一个净值点"""
    weeks = {}
    for d, v in series:
        iso = d.isocalendar()
        key = (iso[0], iso[1])
        if key not in weeks or d > weeks[key][0]:
            weeks[key] = (d, v)
    return [weeks[k] for k in sorted(weeks)]


def metrics(points):
    """周频净值点 [(date, nav)] -> 指标; 年化按 n/52"""
    n = len(points) - 1
    if n < 4:
        return None
    curve = [v for _, v in points]
    dates = [d for d, _ in points]
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]
    years = n / WEEKS_PER_YEAR
    total_ret = curve[-1] / curve[0] - 1
    ann_ret = (1 + total_ret) ** (1 / years) - 1
    w_std = statistics.stdev(rets)
    ann_vol = w_std * math.sqrt(WEEKS_PER_YEAR)
    sharpe = (ann_ret - RF) / ann_vol if ann_vol else float("nan")
    # 真实最大回撤(周频)
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    calmar = ann_ret / abs(mdd) if mdd else float("nan")
    # 下行偏差(MAR=0, 周) -> 年化
    downside = [min(r, 0.0) for r in rets]
    dd_ann = math.sqrt(sum(d * d for d in downside) / n) * math.sqrt(WEEKS_PER_YEAR)
    sortino = (ann_ret - RF) / dd_ann if dd_ann else float("nan")
    win = sum(1 for r in rets if r > 0) / n
    new_high = sum(1 for i, v in enumerate(curve[1:], 1) if v >= max(curve[:i])) / n
    skew = (sum((r - statistics.mean(rets)) ** 3 for r in rets) / n) / (w_std ** 3) if w_std else 0.0
    srt = sorted(rets)
    var5 = srt[max(0, int(0.05 * n))]
    es5 = statistics.mean(srt[: max(1, int(0.05 * n) + 1)])
    pos = [r for r in rets if r > 0]; neg = [r for r in rets if r < 0]
    pl_ratio = (statistics.mean(pos) / abs(statistics.mean(neg))) if pos and neg else float("nan")
    # 最长水下期: 周数 + 自然日天数(从不创新高的首点到修复点/序列末端)
    uw, uw_max = 0, 0
    peak, peak_date = curve[0], dates[0]
    cur_peak_date, uw_days_max = dates[0], 0
    for d, v in zip(dates[1:], curve[1:]):
        if v >= peak:
            peak, uw = v, 0
            cur_peak_date = d
        else:
            uw += 1
            uw_max = max(uw_max, uw)
            uw_days_max = max(uw_days_max, (d - cur_peak_date).days)
    # 当前连续水下周数
    pk, cu = curve[0], 0
    for v in curve[1:]:
        if v >= pk:
            pk, cu = v, 0
        else:
            cu += 1
    streak, max_streak = 0, 0
    for r in rets:
        if r < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return dict(n=n, total_ret=total_ret, ann_ret=ann_ret, ann_vol=ann_vol,
                sharpe=sharpe, mdd=mdd, calmar=calmar, sortino=sortino, dd_ann=dd_ann,
                win=win, new_high=new_high, skew=skew, var5=var5, es5=es5,
                worst=srt[0], pl_ratio=pl_ratio, uw_max=uw_max, uw_cur=cu,
                uw_days_max=uw_days_max, streak_max=max_streak,
                **drawdown_shape([d for d, _ in points], curve))  # 回撤分布口径


def quarterly_returns(points):
    """周频净值点 -> {季度key: 季度收益率}; 季内取最后一个净值点为季末净值"""
    qend = {}  # quarter -> (date, nav) 季内最后一点
    for d, v in points:
        q = f"{d.year}Q{(d.month - 1) // 3 + 1}"
        if q not in qend or d > qend[q][0]:
            qend[q] = (d, v)
    qs = sorted(qend)
    out = {}
    prev_nav = None
    for q in qs:
        if prev_nav is not None:
            out[q] = qend[q][1] / prev_nav - 1
        prev_nav = qend[q][1]
    return out


def crisis_metrics(qrets, crisis_set):
    """危机阿尔法: 基金季度收益与危机季度集合对齐(同 analyze_cta.py 口径)"""
    down = [r for q, r in qrets.items() if q in crisis_set]
    if len(qrets) < 4 or not down:
        return {}
    return dict(
        crisis_n=len(down),
        crisis_avg=statistics.mean(down),
        crisis_win=sum(1 for r in down if r > 0) / len(down),
    )


def max_index_corr(qrets, indices):
    """基金季度收益 vs 各股票指数季度收益 相关系数取最大(同 analyze_cta.py)"""
    corrs = {}
    for iname, idata in indices.items():
        aligned = [(r, idata[q]) for q, r in qrets.items() if q in idata]
        if len(aligned) >= 4:
            fr = [a for a, _ in aligned]; mr = [b for _, b in aligned]
            mf, mm = statistics.mean(fr), statistics.mean(mr)
            sd = statistics.pstdev(fr) * statistics.pstdev(mr)
            if sd:
                corrs[iname] = sum((a - mf) * (b - mm) for a, b in aligned) / len(aligned) / sd
    return (max(corrs.values()) if corrs else 0.0), corrs


def _norm_company(s):
    return (s or "").replace("(", "（").replace(")", "）").strip()


def find_mgr(company_name, mgr_info):
    """管理人名称匹配 manager_info.json(优先全称, 兼容半角括号与前缀截断)"""
    cn = _norm_company(company_name)
    for k in mgr_info:
        if _norm_company(k) == cn:
            return k, mgr_info[k]
    k = next((k for k in mgr_info
              if _norm_company(k).startswith(cn) or cn.startswith(_norm_company(k))), None)
    if k:
        return k, mgr_info[k]
    return company_name, None


def extract_fees(detail):
    """elementInfo/feeList -> 费率信息"""
    ei = detail.get("elementInfo") or {}
    fl = detail.get("feeList") or {}

    def fee_tiers(node):
        if not node or not node.get("fee"):
            return ""
        parts = [f"{t['limit']}{t['fee']}" if t.get("limit") else t["fee"]
                 for t in node["fee"]]
        return "/".join(parts)

    perf_text = ei.get("performance_fee_text") or ""
    perf_note = (ei.get("performance_fee_deduction_frequency") or "") + \
                (ei.get("performance_fee_note_text") or "")
    high_water = "高水位" in perf_note
    lock = ei.get("lock_period_text") or ei.get("lockup_period_text") or ""
    return dict(
        mgmt_fee=ei.get("management_fee_text") or "",
        bank_fee=ei.get("managementfee_bank_text") or "",
        outsourcing_fee=ei.get("outsourcing_fee_text") or "",
        perf_fee=perf_text + ("（高水位法）" if high_water and perf_text else ""),
        purchase_fee=fee_tiers(fl.get("purchase")),
        redeem_fee=fee_tiers(fl.get("redeem")),
        subscription_fee=fee_tiers(fl.get("subscription")),
        lock_period=lock.split("，")[0] if lock else "",
        min_inv_text=ei.get("min_investment_share_text") or "",
    )


def parse_min_inv(detail):
    """elementInfo.min_investment_share -> 万元(float); '认证可见'/空/非数值 -> None(待确认)"""
    v = (detail.get("elementInfo") or {}).get("min_investment_share")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _reasons(r):
    """根据指标自动生成适配理由(pros)与风险点(cons)"""
    pros, cons = [], []
    if r["_v3_sortino"] >= 0.8:
        pros.append(f"索提诺 {r['sortino']:.1f}（池内前20%），下行偏差仅 {r.get('dd_ann', 0)*100:.1f}%")
    if r.get("crisis_low_n"):
        cons.append(f"危机季度样本不足（仅{r.get('crisis_n',0)}个），危机指标不参与评价")
    elif r.get("crisis_win", 0) >= 0.99:
        pros.append(f"危机季度全胜（{r['crisis_n']}个危机季度均正收益，平均{r['crisis_avg']*100:+.1f}%）")
    elif r.get("crisis_win", 0) >= 0.75:
        pros.append(f"危机胜率 {r['crisis_win']*100:.0f}%，危机均季 {r['crisis_avg']*100:+.1f}%")
    if r["uw_max"] <= 4:
        pros.append("回撤从未超过一个月即修复")
    if r["_v3_pl_ratio"] >= 0.8 and r["pl_ratio"] >= 1.5:
        pros.append(f"盈亏比 {r['pl_ratio']:.1f}，尾部风险在上涨侧")
    if r["_v3_new_high"] >= 0.8:
        pros.append(f"新高占比 {r['new_high']*100:.0f}%，持有体验好")
    if r["age"] >= 7:
        pros.append(f"{r['age']}年长样本，业绩可信度高")
    if r["age"] < 3:
        cons.append(f"成立仅{r['age']}年，未经历完整商品周期，可靠性折扣后排名被压低")
    if r["uw_max"] >= 39:
        cons.append(f"最长{r['uw_max']}周（约{r['uw_max']//4}个季度）未创新高，持有体验差")
    # 回撤分布口径(2026-10-07统一): 前5大回撤均值/集中度/未修复
    if r.get("top5_mdd", 0) > -0.20 and not r.get("dd_low_conf"):
        pros.append(f"前5大回撤均值{r['top5_mdd']*100:.1f}%，分布在-20%容忍度内")
    if r.get("top5_mdd", 0) < -0.20:
        cons.append(f"前5大回撤均值{r['top5_mdd']*100:.1f}%超-20%容忍线"
                    + ("（episode不足5个，低置信度）" if r.get("dd_low_conf") else ""))
    if r.get("dd_concentration") == r.get("dd_concentration") and r["dd_concentration"] <= 1.2 \
       and r.get("n_episodes", 0) >= 5 and r.get("n_deep", 0) >= 2:
        cons.append(f"回撤集中度{r['dd_concentration']:.2f}≈1，深度回撤是常态而非单次尾部")
    if r.get("n_unrepaired"):
        cons.append(f"当前仍有{r['n_unrepaired']}个回撤episode未修复，损失是现实的而非历史的")
    if r.get("crisis_win", 1) < 0.55:
        cons.append(f"危机胜率仅{r['crisis_win']*100:.0f}%，危机保护能力弱")
    if r["pl_ratio"] < 1.5:
        cons.append(f"盈亏比仅{r['pl_ratio']:.1f}，赚小亏大")
    if r["sharpe"] < 0.8:
        cons.append(f"夏普{r['sharpe']:.2f}偏低")
    if r.get("mgr_unverified"):
        cons.append(f"管理人（{r.get('mgr', '?')}）成立年份/规模未核实，需补协会备案数据")
    if r.get("mgr_note"):
        cons.append(f"管理人合规关注: {r['mgr_note']}")
    return pros or ["各项指标均居中游"], cons


def generate_report(pool, excluded, siblings_note):
    """每次评估自动生成报告: 每只产品的适配/排除理由(适配度框架, 不作买卖建议)"""
    L = ["# CTA 私募适配度评估报告（排排网周频口径）",
         f"> 生成时间: {date.today()} | 数据窗口: 截至2026年9月末 | 评分池 {len(pool)} 只 / 排除 {len(excluded)} 只",
         "> 方法见 cta_evaluation_plan.md（v3: 门槛过滤 + 指标加权 + 短样本可靠性折扣），指标口径与 danjuan_cta_report.md 可对照",
         "> 分层为适配度评级（与投资者画像的匹配程度），不构成投资建议",
         "> **注意: 本口径为周/日频净值（统一重采样为周频，年化按 n/52），回撤为真实回撤，"
         "与 danjuan_cta_report.md 的季度口径不可直接比分**",
         "> 预算约束（2026-10-07 用户拍板）: 起购金额≤200万（单只预算100万、最高接受200万起购）；"
         "量派CTA七号C/量派CTA八号C（500万起购）、瑞达瑞智进取共赢5号（300万起购）因此剔除",
         "> " + siblings_note, ""]
    tiers = [("高适配（核心候选）", 0.70, 99), ("中适配（备选）", 0.62, 0.70),
             ("观察", 0.50, 0.62), ("低适配", -1, 0.50)]

    # 高适配层需同时过绝对阈值线(周频口径), 避免纯相对排名失真
    # 回撤为分布口径(2026-10-07用户拍板, 与股票类体系统一): 前5大回撤episode均值 ≥ -20%
    def pass_absolute(r):
        if r["sharpe"] <= 1.0:
            return False, f"夏普{r['sharpe']:.2f}未达1.0"
        if r.get("top5_mdd", r["mdd"]) < -0.20:
            return False, f"前5大回撤均值{r.get('top5_mdd', r['mdd'])*100:.0f}%超-20%"
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
        for r in grp:
            pros, cons = _reasons(r)
            L.append(f"### {r['name']}（{r['symbol']}，{r['age']}年，适配度得分{r['score_v3']:.2f}）")
            L.append(f"- 年化{r['ann_ret']*100:+.1f}% / 真实回撤{r['mdd']*100:.1f}% / 夏普{r['sharpe']:.2f} / "
                     f"索提诺{r['sortino']:.2f} / 胜率{r['win']*100:.0f}% / 盈亏比{r['pl_ratio']:.2f} / "
                     f"新高{r['new_high']*100:.0f}% / 危机均季{r.get('crisis_avg', float('nan'))*100:+.1f}% / "
                     f"危机胜率{r.get('crisis_win', float('nan'))*100:.0f}% / 最长水下{r['uw_max']}周"
                     f"（最长{r['uw_days_max']}天未创新高） / 与股票指数最大相关性{r.get('mkt_corr', 0):+.2f}")
            if r.get("mgr"):
                extra = []
                if r.get("mgr_found"):
                    extra.append(f"{r['mgr_found']}年成立")
                if r.get("mgr_aum_text"):
                    extra.append(f"规模{r['mgr_aum_text']}")
                L.append(f"- 管理人: {r['mgr']}" + (f"（{'，'.join(extra)}）" if extra else ""))
            if r.get("pm_start"):
                L.append(f"- 基金经理: {r.get('pm', '?')}（任职 {r['pm_start']} 起至今，"
                         f"指标统计区间为其任职以来 {r['n']} 周）")
            fees = r.get("fees", {})
            if any(fees.get(k) for k in ("mgmt_fee", "perf_fee", "purchase_fee", "redeem_fee")):
                L.append(f"- 费率: 管理费{fees.get('mgmt_fee') or '-'} / 托管{fees.get('bank_fee') or '-'}"
                         f"+外包{fees.get('outsourcing_fee') or '-'} / 业绩报酬{fees.get('perf_fee') or '-'} / "
                         f"申购{fees.get('purchase_fee') or '-'} / 认购{fees.get('subscription_fee') or '-'} / "
                         f"赎回{fees.get('redeem_fee') or '-'} / 锁定期{fees.get('lock_period') or '-'} / "
                         f"起购{fees.get('min_inv_text') or '-'}")
            if r.get("min_inv_pending"):
                L.append("- ⚠ 起购金额待确认（排排网显示'认证可见'或空缺，申购前需向管理人/销售机构核实）")
            # 回撤形态四指标(分布口径, 与股票类体系统一, 2026-10-07)
            dd_line = (f"前5大回撤均值{r['top5_mdd']*100:.1f}%（{r['n_episodes']}个独立episode"
                       f"{'，不足5个低置信度' if r['dd_low_conf'] else ''}） / "
                       f"回撤集中度{r['dd_concentration']:.2f} / "
                       f"深度回撤(谷底<-10%)次数{r['n_deep']} / "
                       + (f"实质回撤(<-5%)平均修复{r['avg_repair_weeks']:.0f}周"
                          if r.get("avg_repair_weeks") is not None else "实质回撤(<-5%)均无已修复样本"))
            L.append(f"- 回撤形态: {dd_line}")
            if r.get("n_unrepaired"):
                cur = next((e for e in r["top5"] if not e["repaired"]), None)
                L.append(f"- ⚠ 未修复回撤: {r['n_unrepaired']}个episode仍在水下"
                         + (f"（当前回撤最深{cur['depth']*100:.1f}%，始于{cur['peak_date']}，"
                            f"谷底{cur['trough_date']}）——未修复意味着损失是现实的而非历史的"
                            if cur else ""))
            if r.get("_demoted"):
                L.append(f"- ⚠ 得分达高适配线但未过绝对阈值（{r['_demoted']}），列入备选")
            L.append("- 适配理由: " + "；".join(pros))
            if cons:
                L.append("- 风险点: " + "；".join(cons))
            if r.get("sibling_ref"):
                L.append(f"- 同系长样本参照: {r['sibling_ref']}")
            if r.get("same_strategy_note"):
                L.append(f"- 同策略验证: {r['same_strategy_note']}")
            L.append("")
    L.append("## 排除名单（不参与评分）\n")
    for name, sym, reason in excluded:
        L.append(f"- **{name}**（{sym or '—'}）：{reason}")
    L.append("")
    L.append("注: 本口径为周/日频净值，回撤为真实回撤（可见季度内回撤），"
             "与 danjuan_cta_report.md 的季度口径不可直接比分；季度口径回撤约为真实值的1/2~1/4。")
    L.append("")
    L.append("---")
    L.append("> 免责声明： 本报告仅作信息整理与适配度分析，不构成任何投资建议、要约或收益承诺。"
             "私募证券基金过往业绩不预示未来表现，投资者应自行承担投资风险。"
             "数据来源于蛋卷基金公开货架信息及第三方平台截图，可能存在口径偏差或滞后，"
             "最终以基金管理人正式披露文件及基金合同为准。合格投资者认定与适当性匹配请以持牌销售机构流程为准。")
    with open("docs/simuwang_weekly_report.md", "w", encoding="utf-8") as fp:
        fp.write("\n".join(L))
    print(f"\n报告已生成: docs/simuwang_weekly_report.md（{len(pool)}只评分 + {len(excluded)}只排除）")


def main():
    name2symbol = load_danjuan_symbols()
    summary = load_summary()
    hs300 = json.load(open("data/hs300_quarterly.json", encoding="utf-8"))
    multi = json.load(open("data/idx_quarterly_multi.json", encoding="utf-8"))
    indices = {"hs300": hs300, **multi}
    # 管理人门槛数据(同 analyze_cta.py)
    mgr_info, mgr_override = {}, {}
    if os.path.exists("data/manager_info.json"):
        raw = json.load(open("data/manager_info.json", encoding="utf-8"))
        mgr_override = raw.pop("_symbol_override", {})
        mgr_info = raw
    # 危机季度: 沪深300/中证500/中证1000/创业板指 任一个当季跌超-3%(同 analyze_cta.py)
    crisis_set = {k for k, v in hs300.items() if v < -0.03}
    for idx in multi.values():
        crisis_set |= {k for k, v in idx.items() if v < -0.03}
    crisis_set = {k for k in crisis_set if k <= "2026Q3"}
    print(f"危机季度(四指数并集): {len(crisis_set)} 个: {sorted(crisis_set)}")

    # 同系长样本代理(siblings 目录由另一任务抓取, 可能不存在)
    siblings_by_company = defaultdict(list)
    siblings_note = "同系长样本参照: data/simuwang/siblings/ 目录不存在（抓取任务未完成），本栏目跳过"
    if os.path.isdir("data/simuwang/siblings"):
        for p in glob.glob("data/simuwang/siblings/*.json"):
            try:
                d = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            ns = d.get("nav_series") or []
            ci = (d.get("detail") or {}).get("companyInfo") or {}
            if len(ns) >= 52 and ci.get("company_name"):
                siblings_by_company[_norm_company(ci["company_name"])].append(
                    (os.path.basename(p)[:-5], ns))
        if siblings_by_company:
            siblings_note = "同系长样本参照: 成立不足3年的产品附同管理人长样本产品参照"
        else:
            siblings_note = "同系长样本参照: siblings 目录无有效净值数据，本栏目跳过"
    print(siblings_note)

    today = date.today()
    rows = []
    excluded = []  # (名称, 代码, 理由)
    pm_extracted = {}  # symbol -> {pm, pm_start} 写回 pm_tenure.json
    for path in sorted(glob.glob("data/simuwang/*.json")):
        name = os.path.basename(path)[:-5]
        d = json.load(open(path, encoding="utf-8"))
        detail = d.get("detail") or {}
        ci = detail.get("companyInfo") or {}
        symbol = match_symbol(name, name2symbol)
        if not symbol:
            if name in NON_CTA_HOLDINGS:
                # 用户股票类持仓, 不属于CTA评估池
                print(f"[跳过] {name}: 非蛋卷CTA货架产品（股票类持仓），不参与CTA评估")
                continue
            # 货架外CTA候选(如量派CTA十号2期): 用排排网fund_id作代码纳入评估
            symbol = (detail.get("baseInfo") or {}).get("fund_id") or ""
            print(f"[货架外候选] {name}: 不在蛋卷货架，以排排网ID {symbol} 纳入评估")
        # 现任基金经理任职信息(无论净值是否可用都提取)
        cur_mgrs = [m for m in (detail.get("relationManager") or [])
                    if m.get("management_end_date") is None]
        pm, pm_start = "", ""
        if cur_mgrs:
            pm = "、".join(m["personnel_name"] for m in cur_mgrs)
            pm_start = min(m["management_start_date"] for m in cur_mgrs
                           if m.get("management_start_date"))
            if symbol and pm_start:
                pm_extracted[symbol] = {"pm": pm, "pm_start": pm_start}
        # 无有效净值序列: 跳过并记录
        raw_ns = [(date.fromisoformat(x["date"]), float(x["cum_nav"]))
                  for x in (d.get("nav_series") or [])]
        if len(raw_ns) < 5:
            reason = ("排排网无有效净值数据（指标为'--'或'认证可见'），无法计算高频指标"
                      if name != "远澜云杉2号" else "排排网未找到该产品（无可靠匹配），无净值数据")
            print(f"[剔除] {name}: {reason}")
            excluded.append((name, symbol, reason))
            continue
        raw_ns.sort()
        # 管理人(公司)门槛: 成立过短 / 规模过小 直接剔除(公司名优先用 companyInfo)
        mgr_name = mgr_override.get(symbol) or ci.get("company_name", "")
        mgr_name, mi = find_mgr(mgr_name, mgr_info)
        if mi:
            if mi.get("found_year") and today.year - mi["found_year"] < MGR_MIN_AGE:
                excluded.append((name, symbol,
                                 f"管理人门槛: {mgr_name} 成立于{mi['found_year']}年, "
                                 f"不足{MGR_MIN_AGE}年, 公司存续期太短"))
                continue
            if mi.get("aum_yi") is not None and mi["aum_yi"] < MGR_MIN_AUM:
                excluded.append((name, symbol,
                                 f"管理人门槛: {mgr_name} 管理规模约{mi['aum_yi']}亿, "
                                 f"低于{MGR_MIN_AUM}亿, 抗风险能力与运营稳定性不足"))
                continue
        # 起购金额门槛(2026-10-07用户拍板): >200万剔除; 认证可见/空缺保留但标注待确认
        min_inv = parse_min_inv(detail)
        if min_inv is not None and min_inv > MAX_MIN_INV:
            excluded.append((name, symbol,
                             f"起购金额门槛: {min_inv:.0f}万起购 > {MAX_MIN_INV}万预算约束"
                             f"（2026-10-07用户拍板，单只预算100万、最高接受200万起购）"))
            print(f"[剔除] {name}: 起购{min_inv:.0f}万超预算")
            continue
        min_inv_pending = min_inv is None
        # 统一重采样为周频(每周最后一个净值点)
        weekly = resample_weekly(raw_ns)
        # 任职区间过滤: 仅统计现任基金经理任职以来的净值
        if pm_start:
            y, mo, dd = map(int, pm_start.split("-"))
            weekly = [(dt, v) for dt, v in weekly if dt >= date(y, mo, dd)]
            if len(weekly) - 1 < MIN_TENURE_WEEKS:
                excluded.append((name, symbol,
                                 f"现任基金经理({pm}){pm_start}任职以来仅{len(weekly) - 1}周"
                                 f"(<{MIN_TENURE_WEEKS}周), 任职期样本不足"))
                continue
        m = metrics(weekly)
        if not m:
            excluded.append((name, symbol,
                             f"样本不足: 重采样后仅{len(weekly) - 1}周(<4), 无法计算指标"))
            continue
        m.update(name=name, symbol=symbol, pm=pm, pm_start=pm_start,
                 mgr=mgr_name, fees=extract_fees(detail),
                 min_inv_pending=min_inv_pending,
                 freq=resample_note(raw_ns),
                 strategy=(summary.get(name) or {}).get("策略", ""))
        if mi:
            m["mgr_found"] = mi.get("found_year")
            m["mgr_aum"] = mi.get("aum_yi")
            m["mgr_aum_text"] = mi.get("aum_text", "")
            if mi.get("note"):
                m["mgr_note"] = mi["note"]
        else:
            m["mgr_unverified"] = True
        # 成立年数: 优先用 baseInfo 的成立日期
        bi = detail.get("baseInfo") or {}
        fd = bi.get("inception_date") or (summary.get(name) or {}).get("成立日期", "")
        if fd:
            y, mo, dd = map(int, fd.split("-"))
            m["age"] = round((today - date(y, mo, dd)).days / 365.25, 1)
        else:
            m["age"] = round(m["n"] / WEEKS_PER_YEAR, 1)
        # 危机阿尔法 + 股票敞口: 周净值聚合为自然季度收益(同 analyze_cta.py 口径)
        qrets = quarterly_returns(weekly)
        m["qrets"] = qrets
        m.update(crisis_metrics(qrets, crisis_set))
        m["mkt_corr"], m["mkt_corr_detail"] = max_index_corr(qrets, indices)
        # 同系长样本参照: 成立不足3年的产品找同管理人长样本
        if m["age"] < 3 and siblings_by_company:
            refs = []
            for sib_name, sib_ns in siblings_by_company.get(_norm_company(ci.get("company_name", "")), []):
                if sib_name == name:
                    continue
                pts = resample_weekly(sorted((date.fromisoformat(x["date"]), float(x["cum_nav"]))
                                             for x in sib_ns))
                sm = metrics(pts)
                if sm and sm["n"] >= 52:
                    sib_age = round(sm["n"] / WEEKS_PER_YEAR, 1)
                    refs.append(f"{sib_name}（成立{sib_age}年，年化{sm['ann_ret']*100:.1f}%，"
                                f"回撤{sm['mdd']*100:.1f}%，夏普{sm['sharpe']:.2f}）")
            if refs:
                m["sibling_ref"] = "；".join(refs)
        rows.append(m)

    # 同策略验证: 货架外候选 vs 已验证基准产品(重叠窗口周频收益相关性>0.9为实锤)
    def _weekly_points(prod):
        d = json.load(open(f"data/simuwang/{prod}.json", encoding="utf-8"))
        ns = sorted((date.fromisoformat(x["date"]), float(x["cum_nav"]))
                    for x in (d.get("nav_series") or []))
        return resample_weekly(ns)

    by_name = {r["name"]: r for r in rows}
    for a, b in SAME_STRATEGY_PAIRS:
        try:
            wa, wb = _weekly_points(a), _weekly_points(b)
        except FileNotFoundError:
            continue
        if len(wa) < 9 or len(wb) < 9:
            continue
        ra = {(w[0].isocalendar()[0], w[0].isocalendar()[1]): wa[i][1] / wa[i - 1][1] - 1
              for i, w in enumerate(wa) if i > 0}
        rb = {(w[0].isocalendar()[0], w[0].isocalendar()[1]): wb[i][1] / wb[i - 1][1] - 1
              for i, w in enumerate(wb) if i > 0}
        common = sorted(set(ra) & set(rb))
        if len(common) < 8:
            continue
        fa = [ra[k] for k in common]; fb = [rb[k] for k in common]
        ma_, mb_ = statistics.mean(fa), statistics.mean(fb)
        sd = statistics.pstdev(fa) * statistics.pstdev(fb)
        c = sum((x - ma_) * (y - mb_) for x, y in zip(fa, fb)) / len(fa) / sd if sd else float("nan")
        verdict = "同策略实锤（>0.9）" if c > 0.9 else "相关性不足0.9，非同策略或仓位/杠杆不同"
        note = f"与{b}重叠{len(common)}周的周频收益相关性 {c:+.2f} → {verdict}"
        # 同窗口指标对照(排除成立窗口差异的干扰)
        if c > 0.9:
            lo = max(wa[0][0], wb[0][0])
            ca = [(d, v) for d, v in wa if d >= lo]
            cb = [(d, v) for d, v in wb if d >= lo]
            ma2, mb2 = metrics(ca), metrics(cb)
            if ma2 and mb2:
                std_ratio = statistics.pstdev(fa) / statistics.pstdev(fb) if statistics.pstdev(fb) else float("nan")
                note += (f"；同窗口（{ca[0][0]}起{ma2['n']}周）对照: 本产品夏普{ma2['sharpe']:.2f}/"
                         f"回撤{ma2['mdd']*100:.1f}% vs {b}夏普{mb2['sharpe']:.2f}/回撤{mb2['mdd']*100:.1f}%，"
                         f"周收益std比{std_ratio:.2f}（≈1为同杠杆）")
        print(f"[同策略验证] {a} vs {b}: {c:+.3f} ({len(common)}周) {verdict}")
        if a in by_name:
            by_name[a]["same_strategy_note"] = note

    # ===== 优化方案 v3: 门槛 + 加权 + 可靠性折扣(权重同 analyze_cta.py) =====
    IDX_CN = {"hs300": "沪深300", "zz500": "中证500", "zz1000": "中证1000", "cyb": "创业板指"}
    # 相关性剔除的产品级核实注释(逐季对照+留一法验证, 见 docs/cta_evaluation_plan.md)
    EXCL_NOTES = {
        "因诺CTA2号B": "核实: 相关性主要由股市大涨季同涨贡献（2024Q3/2025Q3/2026Q2 股指+16%~+50%时基金+3%~+10%），"
                       "危机季度保护尚可（任职以来7个危机季度4正，负季仅-1.4%~-2.8%；"
                       "2026Q3 股指-12%~-28%时仅-1.4%）；按规则剔除，但风险性质偏'牛市同涨'而非'危机跟跌'",
    }

    def loo_min_corr(qrets, idata):
        """留一法: 去掉贡献最大的单个季度后的相关性(相关性稳健性检验)"""
        qs = [q for q in qrets if q in idata]
        if len(qs) < 5:
            return float("nan")
        vals = []
        for drop in qs:
            al = [(qrets[q], idata[q]) for q in qs if q != drop]
            fr = [a for a, _ in al]; mr = [b for _, b in al]
            mf, mm = statistics.mean(fr), statistics.mean(mr)
            sd = statistics.pstdev(fr) * statistics.pstdev(mr)
            if sd:
                vals.append(sum((a - mf) * (b - mm) for a, b in al) / len(al) / sd)
        return min(vals) if vals else float("nan")

    pool = []
    for r in rows:
        if r.get("mkt_corr", 0) >= CORR_LIMIT:
            detail = r.get("mkt_corr_detail", {})
            top_idx = max(detail, key=lambda k: detail[k]) if detail else None
            loo = loo_min_corr(r.get("qrets", {}), indices.get(top_idx, {})) if top_idx else float("nan")
            robust = (f"剔除贡献最大单季后仍{loo:+.2f}≥{CORR_LIMIT}，剔除稳健"
                      if loo == loo and loo >= CORR_LIMIT else
                      f"剔除贡献最大单季后降至{loo:+.2f}，处于临界"
                      if loo == loo else "样本过少无法做留一检验")
            reason = (f"股票敞口过高: 与股票指数(沪深300/中证500/中证1000/创业板)最大相关性 "
                      f"{r['mkt_corr']:+.2f} ≥ {CORR_LIMIT}（主要由{IDX_CN.get(top_idx, '?')}贡献，{robust}）, "
                      f"危机场景下可能跟随股市亏损")
            if r["name"] in EXCL_NOTES:
                reason += "。" + EXCL_NOTES[r["name"]]
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
        reliability = min(1.0, r["n"] / FULL_RELIABILITY_WEEKS)  # 6年(312周)满信度
        r["score_v3"] = raw * reliability + 0.5 * (1 - reliability)
        r["raw_v3"] = raw
    pool.sort(key=lambda r: -r["score_v3"])

    print("\n=== 排排网周频口径 v3 排名（真实回撤 / 任职区间过滤 / 短样本折扣）===")
    print(f"{'排名':<3}{'产品':<16}{'年数':>4} {'周数':>4} {'年化':>7} {'真实回撤':>7} {'索提诺':>6} "
          f"{'危机均季':>7} {'危机胜率':>6} {'水下':>5} {'新高%':>6} {'盈亏比':>6} {'夏普':>6} {'原始分':>6} {'折扣后':>6}")
    for i, r in enumerate(pool, 1):
        print(f"{i:<4}{r['name'][:14]:<16}{r['age']:>4.1f} {r['n']:>4d} "
              f"{r['ann_ret']*100:>6.1f}% {r['mdd']*100:>6.1f}% {r['sortino']:>6.2f} "
              f"{r.get('crisis_avg', float('nan'))*100:>6.1f}% {r.get('crisis_win', float('nan'))*100:>5.0f}% "
              f"{r['uw_max']:>4d}周 {r['new_high']*100:>5.0f}% {r['pl_ratio']:>6.2f} "
              f"{r['sharpe']:>6.2f} {r['raw_v3']:>6.2f} {r['score_v3']:>6.2f}")

    fields = ["name", "symbol", "age", "n", "freq", "strategy", "ann_ret", "ann_vol", "mdd",
              "calmar", "sharpe", "sortino", "win", "new_high", "skew", "pl_ratio",
              "uw_max", "uw_days_max", "uw_cur", "streak_max", "worst", "var5", "es5",
              "mkt_corr", "crisis_n", "crisis_avg", "crisis_win", "pm", "pm_start",
              "mgr", "mgr_found", "mgr_aum", "raw_v3", "score_v3", "total_ret"]
    with open("data/simuwang_weekly_metrics.csv", "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in pool:
            w.writerow(r)
    print(f"\n指标表已生成: data/simuwang_weekly_metrics.csv（{len(pool)}只）")

    generate_report(pool, excluded, siblings_note)

    # 基金经理任职信息写回 pm_tenure.json(合并已有内容, 不覆盖非空条目)
    pm_path = "data/pm_tenure.json"
    existing = json.load(open(pm_path, encoding="utf-8")) if os.path.exists(pm_path) else {}
    merged = dict(existing)
    added = []
    for sym, ten in pm_extracted.items():
        old = merged.get(sym)
        if old and old.get("pm") and old.get("pm_start"):
            continue  # 已有非空条目, 不覆盖
        merged[sym] = ten
        added.append(sym)
    with open(pm_path, "w", encoding="utf-8") as fp:
        json.dump(merged, fp, ensure_ascii=False, indent=1, sort_keys=True)
        fp.write("\n")
    print(f"pm_tenure.json: 共{len(merged)}条（本次新增/更新{len(added)}条）")


def resample_note(raw_ns):
    """识别原始频率, 用于报告标注"""
    ds = [d for d, _ in raw_ns]
    gaps = sorted((b - a).days for a, b in zip(ds, ds[1:]))
    med = gaps[len(gaps) // 2] if gaps else 7
    return "日频(已重采样为周频)" if med <= 1 else "周频"


if __name__ == "__main__":
    main()
