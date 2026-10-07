#!/usr/bin/env python3
"""抓取知名主观股票多头私募代表产品(用于国源水下期的同业对比)。

用法: python3 fetch_peer_equity.py
输出: data/simuwang/peer/<产品名>.json (结构同主抓取)
说明: 存 peer/ 子目录, 避免 analyze_weekly.py 的 data/simuwang/*.json glob 误纳CTA评估池。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_simuwang
from fetch_simuwang import Simuwang, COOKIE_FILE, OUT_DIR
from fetch_simuwang_extra import fetch_fund_by_id

fetch_simuwang.polite_sleep = lambda: time.sleep(6.5)  # 频率限制: >=6秒/请求(曾触发"访问频率过高")

PEER_DIR = OUT_DIR / "peer"

# (搜索关键词, 公司匹配词): 知名主观股票多头私募
PEER_PLANS = [
    ("淡水泉成长1期", "淡水泉"),
    ("景林稳健", "景林"),
    ("高毅邻山1号", "高毅"),   # 冯柳
    ("高毅晓峰", "高毅"),       # 邓晓峰
    ("源乐晟", "源乐晟"),
    ("拾贝", "拾贝"),
    ("星石", "星石"),
    ("重阳", "重阳"),
    ("盘京", "盘京"),
    ("宁泉", "宁泉"),
    ("睿郡", "睿郡"),
    ("希瓦", "希瓦"),
    ("聚鸣", "聚鸣"),
    ("泓澄", "泓澄"),
    ("中欧瑞博", "中欧瑞博"),
    ("林园", "林园"),
    ("东方港湾", "东方港湾"),
    # 第二批(补样本)
    ("明河投资", "明河"),
    ("沣京资本", "沣京"),
    ("汐泰投资", "汐泰"),
    ("仁桥", "仁桥"),
    ("汉和", "汉和"),
    ("明达资产", "明达"),
    ("世诚", "世诚"),
    ("望正", "望正"),
    ("相聚资本", "相聚"),
    ("少薮派", "少薮派"),
    ("宽远", "宽远"),
    ("同犇", "同犇"),
    # 第三批(零售可见概率高的主观股多, 供限频恢复后补抓)
    ("神农投资", "神农"),
    ("新思哲", "新思哲"),
    ("格雷资产", "格雷"),
    ("榕树投资", "榕树"),
    ("赛亚资本", "赛亚"),
    ("森瑞投资", "森瑞"),
    ("翼虎投资", "翼虎"),
    ("理成资产", "理成"),
]
MAX_TRIES = 4  # 每家公司最多试的候选数(净值门控常见, 控制请求量防限频)
WINDOW_START = "2021-12-01"  # 国源水下期起点(窗口对比需覆盖此日)


def pick_peer(matches, company_kw):
    """同公司、股票策略、成立早于窗口起点、未清盘, 按成立日期升序。"""
    cands = []
    for m in matches:
        if company_kw not in (m.get("company_short_name") or ""):
            continue
        if str(m.get("first_strategy")) != "1001":  # 股票策略
            continue
        if m.get("is_liquidate") == "1":
            continue
        if (m.get("inception_date") or "9999") > WINDOW_START:
            continue
        cands.append(m)
    cands.sort(key=lambda m: m.get("inception_date") or "9999")
    return cands


def main():
    cli = Simuwang(COOKIE_FILE.read_text().strip())
    print(f"已登录 uid={cli.uid}")
    PEER_DIR.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    done_companies = set()
    for f in PEER_DIR.glob("*.json"):
        ci = ((json.loads(f.read_text(encoding="utf-8")).get("detail") or {})
              .get("companyInfo") or {}).get("company_short_name", "")
        if ci:
            done_companies.add(ci)
    only = set(a for a in sys.argv[1:] if not a.startswith("--"))
    plans = [p for p in PEER_PLANS if not only or p[1] in only]
    for kw, ckw in plans:
        if any(ckw in c for c in done_companies):
            print(f"[{ckw}] 已有数据, 跳过")
            continue
        matches = cli.search(kw)
        time.sleep(3.2)
        cands = pick_peer(matches, ckw)
        # 精确名优先, 其后按成立最早
        exact = [m for m in cands if kw in (m.get("fund_short_name") or "")]
        cands = exact + [m for m in cands if m not in exact]
        done = False
        for m in cands[:MAX_TRIES]:
            fid, fname = m["query_id"], m.get("fund_short_name")
            print(f"[{ckw}] 试 {fname} ({fid}, 成立 {m.get('inception_date')})")
            try:
                raw = fetch_fund_by_id(cli, fid, fname)
                # 限频检测: nav_trend 返回"访问频率过高" -> 冷却90秒后重试一次
                if (raw.get("nav_trend_raw") or {}).get("status") == 0 and \
                   "频率" in str((raw.get("nav_trend_raw") or {}).get("msg")):
                    print("  -> 触发限频, 冷却90秒重试")
                    time.sleep(90)
                    raw = fetch_fund_by_id(cli, fid, fname)
            except Exception as e:
                print(f"  -> 抓取失败: {e}")
                continue
            ns = [x for x in (raw.get("nav_series") or []) if x.get("cum_nav") is not None]
            if len(ns) < 50 or ns[0]["date"] > WINDOW_START or ns[-1]["date"] < "2024-04-30":
                print(f"  -> 净值不可得/窗口不足({len(ns)}点"
                      f"{(' ' + ns[0]['date'] + '~' + ns[-1]['date']) if ns else ''}), 换候选")
                continue
            (PEER_DIR / f"{fname.replace('/', '_')}.json").write_text(
                json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
            bi = (raw.get("detail") or {}).get("baseInfo") or {}
            print(f"  -> 成功: {len(ns)}点 {ns[0]['date']}~{ns[-1]['date']}, "
                  f"经理 {bi.get('managers_name')}")
            ok += 1
            done = True
            break
        if not done:
            print(f"[{ckw}] 无可用产品")
            fail += 1
    print(f"\n完成: {ok} 成功 / {fail} 无可用")


if __name__ == "__main__":
    main()
