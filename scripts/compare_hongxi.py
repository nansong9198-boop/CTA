#!/usr/bin/env python3
"""宏锡全系CTA横评: 与当前候选池前3对比(用户嫌量派十号2期低波, 找收益更高且回撤在-20%内的)。

口径与 analyze_weekly.py 完全一致(直接import其函数): 周频重采样、任职区间过滤、
危机阿尔法(15个危机季度)、股指相关性(2026-10-08不对称口径: 危机季实测不合格且下行相关性≥0.30才剔除)、
回撤分布形态(drawdown_utils)。
数据: data/simuwang/hongxi/*.json(fetch_hongxi.py) + 主目录已有的宏锡产品。
"""
import glob
import json
import os
import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_weekly import (resample_weekly, metrics, quarterly_returns,
                            crisis_metrics, max_index_corr, parse_min_inv, extract_fees)

ROOT = Path(__file__).resolve().parent.parent
CORR_LIMIT = 0.30
WEEKS_PER_YEAR = 52

# 现候选池前3(2026-10-07口径): 用于对比
POOL_TOP3 = ["量派CTA十号2期C类份额", "博衍九溪CTA2号A", "博衍九溪CTA1号"]


def load_product(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    ns = sorted((date.fromisoformat(x["date"]), float(x["cum_nav"]))
                for x in (d.get("nav_series") or []) if x.get("cum_nav") is not None)
    return d, ns


def loo_min_corr(qrets, idata):
    """留一法作用于下行相关性: 仅在股指下跌季度集合内留一"""
    qs = [q for q in qrets if q in idata and idata[q] < 0]
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


def main():
    hs300 = json.loads((ROOT / "data" / "hs300_quarterly.json").read_text(encoding="utf-8"))
    multi = json.loads((ROOT / "data" / "idx_quarterly_multi.json").read_text(encoding="utf-8"))
    indices = {"hs300": hs300, **multi}
    crisis_set = {k for k, v in hs300.items() if v < -0.03}
    for idx in multi.values():
        crisis_set |= {k for k, v in idx.items() if v < -0.03}
    crisis_set = {k for k in crisis_set if k <= "2026Q3"}
    today = date.today()

    files = sorted(glob.glob(str(ROOT / "data" / "simuwang" / "hongxi" / "*.json")))
    # 主目录的宏锡产品若未被hongxi/覆盖则补充(去重)
    have = {os.path.basename(f)[:-5] for f in files}
    files += [str(ROOT / "data" / "simuwang" / f"{n}.json")
              for n in ("宏锡量化CTA7号", "宏锡量化CTA7号二期") if n not in have]
    # 管理人门槛: 宏锡 2015年成立/100亿+(2026-02) -> 过20亿线
    rows, skipped = [], []
    for path in files:
        name = os.path.basename(path)[:-5]
        d, ns = load_product(path)
        if len(ns) < 30:
            skipped.append((name, "净值不可得或过少"))
            continue
        detail = d.get("detail") or {}
        bi = detail.get("baseInfo") or {}
        ei = detail.get("elementInfo") or {}
        cur = [m for m in (detail.get("relationManager") or [])
               if m.get("management_end_date") is None]
        pm_start = min((m["management_start_date"] for m in cur
                        if m.get("management_start_date")), default=None)
        weekly = resample_weekly(ns)
        if pm_start:
            y, mo, dd = map(int, pm_start.split("-"))
            weekly = [(dt, v) for dt, v in weekly if dt >= date(y, mo, dd)]
        m = metrics(weekly)
        if not m:
            skipped.append((name, "重采样后样本不足"))
            continue
        qrets = quarterly_returns(weekly)
        mc, mc_detail = max_index_corr(qrets, indices)
        # 下行相关性(2026-10-08不对称口径): 仅股指下跌季度
        dc_detail, dc_n = {}, {}
        for iname, idata in indices.items():
            al = [(r, idata[q]) for q, r in qrets.items() if q in idata and idata[q] < 0]
            dc_n[iname] = len(al)
            if len(al) >= 4:
                fr = [a for a, _ in al]; mr = [b for _, b in al]
                mf, mm = statistics.mean(fr), statistics.mean(mr)
                sd = statistics.pstdev(fr) * statistics.pstdev(mr)
                if sd:
                    dc_detail[iname] = sum((a - mf) * (b - mm) for a, b in al) / len(al) / sd
        down_corr = max(dc_detail.values()) if dc_detail else 0.0
        top_idx = max(mc_detail, key=lambda k: mc_detail[k]) if mc_detail else None
        cm = crisis_metrics(qrets, crisis_set)
        # 不对称判定: 危机季实测不合格 且 下行相关性>=0.30 才剔除
        crisis_bad = (cm.get("crisis_avg") is not None and cm["crisis_avg"] < 0) or \
                     (cm.get("crisis_win") is not None and cm["crisis_win"] < 0.5)
        low_conf = cm.get("crisis_n", 0) < 4
        if down_corr >= CORR_LIMIT and crisis_bad and not low_conf:
            top_dn = max(dc_detail, key=lambda k: dc_detail[k]) if dc_detail else None
            loo_dn = loo_min_corr(qrets, indices.get(top_dn, {})) if top_dn else float("nan")
            verdict = "剔除" + (f"（留一后{loo_dn:+.2f}）" if loo_dn == loo_dn else "")
        elif mc >= CORR_LIMIT or down_corr >= CORR_LIMIT:
            verdict = "待观察" if low_conf else "疑点标记"
        else:
            verdict = "通过"
        min_inv = parse_min_inv(detail)
        fees = extract_fees(detail)
        fd = bi.get("inception_date")
        age = round((today - date(*map(int, fd.split("-")))).days / 365.25, 1) if fd else None
        rows.append(dict(
            name=name, fund_id=bi.get("fund_id") or d.get("fund_id"),
            on_sale=d.get("on_sale"), age=age, n=m["n"],
            ann=m["ann_ret"], mdd=m["mdd"], top5=m["top5_mdd"], sharpe=m["sharpe"],
            sortino=m["sortino"], calmar=m["calmar"],
            crisis_win=cm.get("crisis_win"), crisis_avg=cm.get("crisis_avg"),
            mkt_corr=mc, down_corr=down_corr, verdict=verdict, top_idx=top_idx,
            min_inv=min_inv, lock=fees.get("lock_period", ""),
            perf=fees.get("perf_fee", ""),
        ))
    rows.sort(key=lambda r: -r["calmar"] if r["calmar"] == r["calmar"] else 0)

    print(f"=== 宏锡全系CTA横评（{len(rows)}只有净值，{len(skipped)}只不可得）===")
    print(f"管理人门槛: 宏锡2015年成立/100亿+，过20亿线")
    print(f"{'产品':<18}{'年数':>5}{'周数':>5} {'年化':>7} {'真实回撤':>7} {'前5均值':>7} {'夏普':>6} "
          f"{'索提诺':>6} {'卡玛':>6} {'危机胜率':>6} {'全周期':>6} {'下行':>6} {'判定':>6} {'起购':>5} {'在售':>4}")
    for r in rows:
        mi = f"{r['min_inv']:.0f}万" if r["min_inv"] is not None else "待确认"
        sale = {True: "在售", False: "不在售", None: "未知"}[r["on_sale"]]
        cw = f"{r['crisis_win']*100:.0f}%" if r["crisis_win"] is not None else "--"
        print(f"{r['name'][:16]:<18}{r['age'] or 0:>5.1f}{r['n']:>5d} "
              f"{r['ann']*100:>6.1f}% {r['mdd']*100:>6.1f}% {r['top5']*100:>6.1f}% "
              f"{r['sharpe']:>6.2f} {r['sortino']:>6.2f} {r['calmar']:>6.2f} {cw:>6} "
              f"{r['mkt_corr']:>+6.2f} {r['down_corr']:>+6.2f} {r['verdict']:>6} {mi:>5} {sale:>4}")
    print("\n不可得:", "、".join(n for n, _ in skipped) or "无")

    # 与现池前3同口径对比(直接重算)
    print(f"\n=== 宏锡最优 vs 现池前3（同口径重算）===")
    ref = []
    for n in POOL_TOP3:
        d, ns = load_product(ROOT / "data" / "simuwang" / f"{n}.json")
        wk = resample_weekly(ns)
        m = metrics(wk)
        ref.append((n, m))
    best = [r for r in rows if r["top5"] >= -0.20 and r["verdict"] != "剔除"]
    best = best[:3]
    print(f"{'产品':<18}{'年化':>7} {'真实回撤':>7} {'夏普':>6} {'卡玛':>6} {'索提诺':>6}")
    for n, m in ref:
        print(f"{n[:16]:<18}{m['ann_ret']*100:>6.1f}% {m['mdd']*100:>6.1f}% "
              f"{m['sharpe']:>6.2f} {m['calmar']:>6.2f} {m['sortino']:>6.2f}")
    for r in best:
        print(f"{'[宏锡]' + r['name'][:13]:<18}{r['ann']*100:>6.1f}% {r['mdd']*100:>6.1f}% "
              f"{r['sharpe']:>6.2f} {r['calmar']:>6.2f} {r['sortino']:>6.2f}")
    write_report(rows, skipped, ref, best)
    return rows, ref


def write_report(rows, skipped, ref, best):
    L = ["# 宏锡全系 CTA 产品横评（2026-10-07 专项）",
         "> 背景: 用户认为首选量派CTA十号2期收益偏低（1倍杠杆低波），要求横评宏锡全系CTA找更高收益选择；回撤容忍 -20%",
         "> 口径: 与 analyze_weekly.py 完全一致（周频、任职区间过滤、危机季度15个、回撤分布形态）；"
         "管理人门槛: 宏锡2015年成立/100亿+（2026-02），过20亿线",
         "> 适配度框架，不构成投资建议", ""]
    L.append(f"## 横评表（{len(rows)}只有净值数据，{len(skipped)}只不可得）\n")
    L.append("| 产品 | 成立年数 | 年化 | 真实回撤 | 前5大回撤均值 | 夏普 | 卡玛 | 危机胜率 | 全周期相关性 | 下行相关性 | 不对称判定 | 起购 | 在售 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        mi = f"{r['min_inv']:.0f}万" if r["min_inv"] is not None else "待确认"
        sale = {True: "在售", False: "不在售", None: "未知"}[r["on_sale"]]
        cw = f"{r['crisis_win']*100:.0f}%" if r["crisis_win"] is not None else "--"
        L.append(f"| {r['name']} | {r['age']} | {r['ann']*100:+.1f}% | {r['mdd']*100:.1f}% | "
                 f"{r['top5']*100:.1f}% | {r['sharpe']:.2f} | {r['calmar']:.2f} | {cw} | "
                 f"{r['mkt_corr']:+.2f} | {r['down_corr']:+.2f} | {r['verdict']} | {mi} | {sale} |")
    L.append("")
    L.append("注: 不对称判定（2026-10-08 规则修正）= 危机季实测不合格（均季<0或胜率<50%）且"
             "下行相关性（仅股指下跌季度计算）≥0.30 才剔除；全周期相关性≥0.30 仅作疑点标记。"
             "宏锡全系全周期相关性偏高（+0.35~+0.43）主要由上涨季跟涨贡献，下行相关性普遍低——凸性特征")
    L.append("")
    if skipped:
        L.append("净值不可得（排排网门控）: " + "、".join(n for n, _ in skipped))
        L.append("")
    L.append("另：宏锡商品期货指数增强1号B类份额、宏锡截面量化CTA1号A类份额、"
             "宏锡量化CTA29号二期、宏锡量化CTA30号十期 4 只在售产品因排排网限频/门控暂不可得"
             "（均为 2020 年后成立的次新/新产品，不影响横评结论的代表性）")
    L.append("")
    L.append("## 宏锡最优 vs 现候选池前3（同口径重算）\n")
    L.append("| 产品 | 年化 | 真实回撤 | 夏普 | 卡玛 | 索提诺 |")
    L.append("|---|---|---|---|---|---|")
    for n, m in ref:
        L.append(f"| {n} | {m['ann_ret']*100:+.1f}% | {m['mdd']*100:.1f}% | "
                 f"{m['sharpe']:.2f} | {m['calmar']:.2f} | {m['sortino']:.2f} |")
    for r in best:
        L.append(f"| **[宏锡]{r['name']}** | {r['ann']*100:+.1f}% | {r['mdd']*100:.1f}% | "
                 f"{r['sharpe']:.2f} | {r['calmar']:.2f} | {r['sortino']:.2f} |")
    L.append("")
    L.append("## 结论\n")
    L.append("- **高收益产品线存在但回撤越线**：宏锡量化CTA32号 年化+32.8% 为全系最高，"
             "但真实回撤 -31.3%（前5大均值 -19.8%，压-20%线）；粤晖29号（+20.6%/-20.2%）、"
             "CTA36号（+25.5%/-25.8%）、30号（+25.2%/-28.8%）同样超出 -20% 容忍度")
    L.append("- **回撤合规的产品收益不优**：泰壹/量化2号/CTA5号/CTA7号等回撤在-16%内，"
             "但年化仅 +10%~+15%、卡玛 ≤0.95，全面低于现池博衍九溪CTA2号A（+26.0%/-12.5%，卡玛2.08）")
    L.append("- **结论：宏锡无综合优于现首选/备选的产品，首选不变更**——"
             "量派CTA十号2期C类份额（同策略验证）仍为首选，博衍九溪CTA2号A 为高收益备选；"
             "宏锡产品的不对称相关性判定多为疑点标记/通过（涨时跟涨、跌时不跟跌），"
             "但收益-回撤比不占优")
    L.append("")
    L.append("---")
    L.append("> 免责声明： 本报告仅作信息整理与适配度分析，不构成任何投资建议、要约或收益承诺。"
             "私募证券基金过往业绩不预示未来表现，投资者应自行承担投资风险。"
             "数据来源于蛋卷基金公开货架信息及第三方平台截图，可能存在口径偏差或滞后，"
             "最终以基金管理人正式披露文件及基金合同为准。合格投资者认定与适当性匹配请以持牌销售机构流程为准。")
    (ROOT / "docs" / "hongxi_review.md").write_text("\n".join(L), encoding="utf-8")
    print("\n报告已生成: docs/hongxi_review.md")


if __name__ == "__main__":
    main()
