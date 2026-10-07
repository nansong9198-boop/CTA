#!/usr/bin/env python3
"""F1 交叉验证: 蛋卷季度口径 vs 排排网周度/日度口径。

输入: data/danjuan_cta_metrics_v3.csv + data/simuwang/summary.csv
输出: docs/cross_validation.md + 终端对比表

目的:
- 量化季度口径对最大回撤/波动率的低估倍数
- 发现口径矛盾(排名失真、策略类型不符)的产品
"""
import csv
import statistics
from datetime import date


def f(x):
    """宽松 float 解析: '--' / '' / None -> None"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    dj = {r["name"]: r for r in csv.DictReader(
        open("data/danjuan_cta_metrics_v3.csv", encoding="utf-8-sig"))}
    sw = {r["产品名"]: r for r in csv.DictReader(
        open("data/simuwang/summary.csv", encoding="utf-8-sig"))}

    rows = []
    for name, d in dj.items():
        s = sw.get(name)
        row = dict(name=name, score=f(d.get("score_v3")),
                   dj_ann=f(d.get("ann_ret")), dj_mdd=f(d.get("mdd")),
                   dj_sharpe=f(d.get("sharpe")), dj_sortino=f(d.get("sortino")))
        if s and s.get("状态") == "成功":
            row.update(sw_name=s.get("排排网名称", ""), strategy=s.get("策略", ""),
                       pm=s.get("基金经理", ""), sw_found=s.get("成立日期", ""),
                       sw_scale_yi=(f(s.get("最新规模(万)")) or 0) / 1e4 or None,
                       sw_ann=(f(s.get("累计收益%")) and f(s.get("年化收益%"))),
                       sw_mdd=f(s.get("最大回撤%")), sw_sharpe=f(s.get("夏普(成立来)")),
                       sw_sharpe_1y=f(s.get("夏普(近1年)")), sw_vol=f(s.get("年化波动率%")),
                       sw_sortino=f(s.get("索提诺")), sw_calmar=f(s.get("卡玛比率")))
        else:
            row["sw_name"] = "未找到" if s else ""
        rows.append(row)
    rows.sort(key=lambda r: -(r["score"] or 0))

    # 回撤低估倍数
    ratios = [abs(r["sw_mdd"]) / abs(r["dj_mdd"] * 100)
              for r in rows
              if r.get("sw_mdd") and r.get("dj_mdd") and r["dj_mdd"] != 0]
    med = statistics.median(ratios) if ratios else None

    L = ["# F1 交叉验证报告: 蛋卷(季度) vs 排排网(周/日频)",
         f"> 生成时间: {date.today()} | 配对成功 {sum(1 for r in rows if r.get('sw_mdd') is not None)} 只",
         "> 蛋卷口径: 季度收益序列(看不到季度内回撤); 排排网口径: 周/日频净值(真实回撤)",
         f"> **回撤低估倍数中位数: {med:.1f}x**（排排网最大回撤 / 蛋卷季度口径回撤）" if med else "",
         "",
         "| 排名 | 产品 | 蛋卷年化 | 排排网年化 | 蛋卷回撤 | 排排网回撤 | 低估倍数 | 蛋卷夏普 | 排排网夏普 | 排排网策略 | 基金经理 | 规模(亿) |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        if r.get("sw_mdd") is None:
            L.append(f"| {i} | {r['name']} | {r['dj_ann']*100:+.1f}% | -- | "
                     f"{r['dj_mdd']*100:.1f}% | -- | -- | {r['dj_sharpe']:.2f} | -- | "
                     f"{r.get('strategy') or '无数据'} | {r.get('pm') or '--'} | -- |")
            continue
        ratio = abs(r["sw_mdd"]) / abs(r["dj_mdd"] * 100) if r["dj_mdd"] else None
        scale = f"{r['sw_scale_yi']:.1f}" if r.get("sw_scale_yi") else "--"
        L.append(f"| {i} | {r['name']} | {r['dj_ann']*100:+.1f}% | {r['sw_ann']:+.1f}% | "
                 f"{r['dj_mdd']*100:.1f}% | {r['sw_mdd']:.1f}% | {ratio:.1f}x | "
                 f"{r['dj_sharpe']:.2f} | {r['sw_sharpe']:.2f} | {r['strategy']} | "
                 f"{r['pm']} | {scale} |")
    L.append("")
    # 口径矛盾警示: 排排网回撤/夏普显著差于蛋卷排名暗示的
    L.append("## 口径矛盾与策略纯度警示\n")
    L.append("> 注: 规模为排排网份额类别口径（如仅 C 类），同产品其他份额可能另有规模，申购前需核实产品总规模\n")
    for r in rows:
        warns = []
        if r.get("sw_mdd") is not None and r.get("dj_mdd"):
            ratio = abs(r["sw_mdd"]) / abs(r["dj_mdd"] * 100) if r["dj_mdd"] else 0
            if ratio > 3:
                warns.append(f"真实回撤是季度口径的 {ratio:.1f} 倍")
            if r.get("sw_sharpe") is not None and r.get("dj_sharpe") is not None \
               and r["sw_sharpe"] < r["dj_sharpe"] * 0.6:
                warns.append(f"排排网夏普 {r['sw_sharpe']:.2f} 远低于蛋卷口径 {r['dj_sharpe']:.2f}")
        if r.get("strategy") and "CTA" not in r["strategy"]:
            warns.append(f"排排网策略分类为「{r['strategy']}」，非纯CTA")
        if r.get("sw_scale_yi") is not None and r["sw_scale_yi"] < 1:
            warns.append(f"产品规模仅 {r['sw_scale_yi']:.2f} 亿，有清盘风险")
        if warns:
            L.append(f"- **{r['name']}**：" + "；".join(warns))
    L.append("")
    L.append("> 数据源: 蛋卷货架API(季度) + 排排网认证接口(周/日频, scripts/fetch_simuwang.py 抓取)")
    with open("docs/cross_validation.md", "w", encoding="utf-8") as fp:
        fp.write("\n".join(L))

    # 终端摘要
    print(f"{'产品':<16}{'蛋卷回撤':>8}{'排排网回撤':>9}{'倍数':>6}{'蛋卷夏普':>8}{'排排网夏普':>9}")
    for r in rows:
        if r.get("sw_mdd") is None:
            continue
        ratio = abs(r["sw_mdd"]) / abs(r["dj_mdd"] * 100) if r["dj_mdd"] else 0
        print(f"{r['name'][:14]:<16}{r['dj_mdd']*100:>7.1f}%{r['sw_mdd']:>8.1f}%"
              f"{ratio:>5.1f}x{r['dj_sharpe']:>8.2f}{r['sw_sharpe']:>9.2f}")
    print(f"\n回撤低估倍数中位数: {med:.1f}x (n={len(ratios)})")
    print(f"报告: docs/cross_validation.md")


if __name__ == "__main__":
    main()
