#!/usr/bin/env python3
"""审计脚本: 从原始接口JSON独立重算全部指标, 校验一致性与排序。

检查项:
1. 季度数 vs 成立日期推导的预期季度数(数据对齐校验)
2. 全零收益季度(可能的数据填充)
3. 危机阿尔法的对齐季度区间
4. v3得分按文档权重逐项重算, 验证排序
输出: danjuan_cta_full_audit.csv + 终端审计报告
"""
import csv
import json
import math
import statistics
from datetime import date

RF = 0.015
END_Q = (2026, 2)  # 序列末项=2026Q2

NOT_PURE_CTA = ("中性", "指数", "指增")


def qkey(y, q):
    return f"{y}Q{q}"


def qkeys_ending(end, n):
    y, q = end
    keys = []
    for _ in range(n):
        keys.append(qkey(y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return keys[::-1]


def load_funds():
    funds = []
    for p in ("data/danjuan_cta_page1.json", "data/danjuan_cta_page2.json"):
        funds.extend(json.load(open(p, encoding="utf-8"))["data"]["fund_datas"])
    return funds


def core_metrics(rets, per_year=4):
    n = len(rets)
    nav, curve = 1.0, [1.0]
    for r in rets:
        nav *= 1 + r
        curve.append(nav)
    total = curve[-1] - 1
    years = n / per_year
    ann = (1 + total) ** (1 / years) - 1
    vol = statistics.stdev(rets) * math.sqrt(per_year)
    sharpe = (ann - RF) / vol if vol else float("nan")
    peak, mdd = 1.0, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    calmar = ann / abs(mdd) if mdd else float("nan")
    dd = math.sqrt(sum(min(r, 0) ** 2 for r in rets) / n) * math.sqrt(per_year)
    sortino = (ann - RF) / dd if dd else float("nan")
    win = sum(1 for r in rets if r > 0) / n
    pos = [r for r in rets if r > 0]
    neg = [r for r in rets if r < 0]
    pl = statistics.mean(pos) / abs(statistics.mean(neg)) if pos and neg else float("nan")
    nh = sum(1 for i, v in enumerate(curve[1:], 1) if v >= max(curve[:i])) / n
    skew = (sum((r - statistics.mean(rets)) ** 3 for r in rets) / n) / (statistics.stdev(rets) ** 3)
    srt = sorted(rets)
    var5 = srt[max(0, int(0.05 * n))]
    es5 = statistics.mean(srt[: max(1, int(0.05 * n) + 1)])
    # 回撤形态
    pk, uw, uw_max = 1.0, 0, 0
    for v in curve[1:]:
        if v >= pk:
            pk, uw = v, 0
        else:
            uw += 1
            uw_max = max(uw_max, uw)
    pk, cu = 1.0, 0
    for v in curve[1:]:
        if v >= pk:
            pk, cu = v, 0
        else:
            cu += 1
    streak, streak_max = 0, 0
    for r in rets:
        streak = streak + 1 if r < 0 else 0
        streak_max = max(streak_max, streak)
    zero_q = sum(1 for r in rets if r == 0)
    return dict(n=n, total=total, ann=ann, vol=vol, sharpe=sharpe, mdd=mdd,
                calmar=calmar, dd=dd, sortino=sortino, win=win, pl=pl, nh=nh,
                skew=skew, var5=var5, es5=es5, worst=srt[0],
                uw_max=uw_max, uw_cur=cu, streak_max=streak_max, zero_q=zero_q)


def crisis(rets, qks, crisis_set):
    down_keys = [k for k in qks if k in crisis_set]
    if len(qks) < 4 or not down_keys:
        return {}
    fr = dict(zip(qks, rets))
    down = [fr[k] for k in down_keys]
    return dict(crisis_n=len(down),
                crisis_avg=statistics.mean(down),
                crisis_win=sum(1 for f in down if f > 0) / len(down))


def main():
    hs300 = json.load(open("data/hs300_quarterly.json", encoding="utf-8"))
    multi = json.load(open("data/idx_quarterly_multi.json", encoding="utf-8"))
    crisis_set = {k for k, v in hs300.items() if v < -0.03}
    for idx in multi.values():
        crisis_set |= {k for k, v in idx.items() if v < -0.03}
    crisis_set = {k for k in crisis_set if k <= "2026Q2"}
    info = {i["symbol"]: i for i in json.load(open("data/danjuan_cta_info.json", encoding="utf-8"))}
    rows, anomalies = [], []
    for f in load_funds():
        dl = f["fund_index_info"]["data_list"]
        if not dl or "percent" not in dl[0]:
            continue
        rets = [float(x["percent"]) for x in dl]
        if len(rets) < 4:
            continue
        m = core_metrics(rets)
        qks = qkeys_ending(END_Q, len(rets))
        m.update(crisis(rets, qks, crisis_set))
        corrs = {}
        for iname, idata in [("hs300", hs300)] + list(multi.items()):
            aligned = [(r_, idata[k]) for r_, k in zip(rets, qks) if k in idata]
            if len(aligned) >= 4:
                fr = [a for a, _ in aligned]; mr = [b for _, b in aligned]
                mf, mm = statistics.mean(fr), statistics.mean(mr)
                sd = statistics.pstdev(fr) * statistics.pstdev(mr)
                if sd:
                    corrs[iname] = sum((a-mf)*(b-mm) for a, b in aligned) / len(aligned) / sd
        m["mkt_corr"] = max(corrs.values()) if corrs else 0.0
        m.update(name=f["fund_name"], symbol=f["symbol"], span=f"{qks[0]}~{qks[-1]}")
        fd = (info.get(f["symbol"]) or {}).get("found", "")
        if fd:
            y, mo, d_ = map(int, fd.split("-"))
            m["age"] = round((date.today() - date(y, mo, d_)).days / 365.25, 1)
            fq = (mo - 1) // 3 + 1
            expect = (END_Q[0] - y) * 4 + (END_Q[1] - fq) + 1
            if expect != len(rets):
                anomalies.append(f"{f['fund_name']}: 成立{fd}推导应{expect}季, 实际{len(rets)}季")
        else:
            m["age"] = round(len(rets) / 4, 1)
            anomalies.append(f"{f['fund_name']}: 无成立日期, 年数按季度数推算({m['age']}年), 无法交叉验证")
        if m["zero_q"]:
            anomalies.append(f"{f['fund_name']}: 含{m['zero_q']}个零收益季度(可能为数据填充)")
        rows.append(m)

    pool = [r for r in rows if not any(k in r["name"] for k in NOT_PURE_CTA)
            and r.get("mkt_corr", 0) < 0.30]

    # v3 评分独立重算
    def pct(key, reverse=False):
        vals = sorted(r[key] for r in pool if key in r and r[key] == r[key])
        for r in pool:
            v = r.get(key)
            if v is None or v != v:
                r["s_" + key] = 0.5
            else:
                rk = vals.index(v) / max(len(vals) - 1, 1)
                r["s_" + key] = 1 - rk if reverse else rk

    for k in ["sortino", "crisis_avg", "crisis_win", "nh", "pl", "sharpe", "calmar", "win"]:
        pct(k)
    for k in ["uw_max", "streak_max"]:
        pct(k, reverse=True)
    for r in pool:
        raw = (0.25 * r["s_sortino"] + 0.10 * r["s_crisis_avg"] + 0.10 * r["s_crisis_win"]
               + 0.10 * r["s_uw_max"] + 0.05 * r["s_streak_max"]
               + 0.10 * r["s_nh"] + 0.10 * r["s_pl"]
               + 0.10 * r["s_sharpe"] + 0.05 * r["s_calmar"] + 0.05 * r["s_win"])
        rel = min(1.0, r["n"] / 24)
        r["raw"] = raw
        r["final"] = raw * rel + 0.5 * (1 - rel)
    pool.sort(key=lambda r: -r["final"])

    fields = ["name", "symbol", "age", "n", "span", "ann", "mdd", "dd", "vol",
              "sharpe", "sortino", "calmar", "win", "pl", "nh",
              "skew", "var5", "es5", "worst", "uw_max", "uw_cur", "streak_max",
              "crisis_n", "crisis_avg", "crisis_win", "mkt_corr",
              "raw", "final"]
    with open("data/danjuan_cta_full_audit.csv", "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(pool)

    print(f"评分池 {len(pool)} 只, 剔除 {len(rows) - len(pool)} 只(假CTA+高敞口)\n")
    print("== 一致性检查 ==")
    print("\n".join(anomalies) if anomalies else "无异常")
    print("\n== 排序复核: 前8名的得分构成(分位数加权) ==")
    hdr = ["产品", "索提诺", "危机均季", "危机胜率", "水下", "连亏", "新高", "盈亏比", "夏普", "卡玛", "胜率", "原始分", "信度", "最终分"]
    print(" ".join(h.rjust(6) if i else h.ljust(14) for i, h in enumerate(hdr)))
    for r in pool[:8]:
        rel = min(1.0, r["n"] / 24)
        cells = [f"{r['s_sortino']:.2f}", f"{r['s_crisis_avg']:.2f}", f"{r['s_crisis_win']:.2f}",
                 f"{r['s_uw_max']:.2f}", f"{r['s_streak_max']:.2f}", f"{r['s_nh']:.2f}",
                 f"{r['s_pl']:.2f}", f"{r['s_sharpe']:.2f}", f"{r['s_calmar']:.2f}",
                 f"{r['s_win']:.2f}", f"{r['raw']:.3f}", f"{rel:.2f}", f"{r['final']:.3f}"]
        print(r["name"][:13].ljust(14) + " ".join(c.rjust(6) for c in cells))


if __name__ == "__main__":
    main()
