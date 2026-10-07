#!/usr/bin/env python3
"""抓取股票类评估所需指数/ETF的日K收盘价, 存 data/idx_daily.json。

口径说明:
- 数据源: 新浪全历史日K(经 akshare: stock_zh_index_daily / fund_etf_hist_sina)
  注: 东财 push2his 本网络不可达(连接被拒), 网易 chddata 502, 故全历史改用新浪;
  新浪为项目已验证数据源(见 cta_evaluation_plan.md 第三节), 腾讯 ifzq 错位已弃用
- 指数: 沪深300/中证500/中证1000/创业板指/中证红利 + 黄金ETF(518880, 黄金基准代理)
- 校验: 聚合为季度收益后与 hs300_quarterly.json / idx_quarterly_multi.json 对照
"""
import json
import sys
import time
from pathlib import Path

import akshare as ak

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "idx_daily.json"

# key -> (symbol, 名称, 是否ETF)
INDICES = {
    "hs300": ("sh000300", "沪深300", False),
    "zz500": ("sh000905", "中证500", False),
    "zz1000": ("sh000852", "中证1000", False),
    "cyb": ("sz399006", "创业板指", False),
    "cndiv": ("sh510880", "红利ETF(510880, 中证红利代理; 新浪中证红利指数数据止于2019-01弃用)", True),
    "gold518880": ("sh518880", "黄金ETF(518880)", True),
}


def fetch_daily(symbol: str, is_etf: bool) -> dict:
    """-> {date: close}"""
    df = ak.fund_etf_hist_sina(symbol=symbol) if is_etf else ak.stock_zh_index_daily(symbol=symbol)
    return {str(d)[:10]: float(c) for d, c in zip(df["date"], df["close"])}


def to_quarterly(daily: dict) -> dict:
    """日收盘 -> 季度收益(每季最后一个交易日收盘环比)"""
    qend = {}
    for d, c in sorted(daily.items()):
        y, m = int(d[:4]), int(d[5:7])
        qend[f"{y}Q{(m - 1) // 3 + 1}"] = c
    out, prev = {}, None
    for q, c in sorted(qend.items()):
        if prev is not None:
            out[q] = c / prev - 1
        prev = c
    return out


def main():
    data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    for key, (symbol, cn, is_etf) in INDICES.items():
        print(f"[{cn}] 抓取中...")
        daily = fetch_daily(symbol, is_etf)
        if len(daily) < 100:
            sys.exit(f"{cn} 数据异常: 仅{len(daily)}条, 停止")
        data[key] = daily
        print(f"  -> {len(daily)}条, {min(daily)} ~ {max(daily)}")
        time.sleep(3.2)  # 请求间隔>=3秒
    OUT.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"\n已写入 {OUT}")

    # 校验: 与已验证过的季度数据对照
    hs_ref = json.loads((ROOT / "data" / "hs300_quarterly.json").read_text(encoding="utf-8"))
    multi_ref = json.loads((ROOT / "data" / "idx_quarterly_multi.json").read_text(encoding="utf-8"))
    refs = {"hs300": hs_ref, **multi_ref}
    print("\n== 季度聚合校验(新浪日K聚合 vs 已存季度数据, 最近6个共有季度) ==")
    ok = True
    for key, ref in refs.items():
        if key not in data:
            continue
        mine = to_quarterly(data[key])
        common = sorted(set(mine) & set(ref))[-6:]
        for q in common:
            diff = abs(mine[q] - ref[q])
            flag = "OK " if diff < 0.005 else "DIFF"
            if diff >= 0.005:
                ok = False
            print(f"  {key:<8} {q}: {mine[q]*100:+6.2f}% vs {ref[q]*100:+6.2f}%  {flag}")
    print("校验通过" if ok else "存在偏差>0.5pp的季度, 请人工复核")


if __name__ == "__main__":
    main()
