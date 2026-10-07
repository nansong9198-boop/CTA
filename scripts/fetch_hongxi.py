#!/usr/bin/env python3
"""抓取宏锡全系在售CTA产品净值(data/simuwang/hongxi/)。

背景: 搜索接口 is_nav_visible 对已登录账号不准(宏锡7号等标记不可见但nav_trend可抓),
故对在售CTA产品直接探 nav_trend。存 hongxi/ 子目录避免进入CTA主评估池glob。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_simuwang
from fetch_simuwang import Simuwang, COOKIE_FILE, OUT_DIR
from fetch_simuwang_extra import fetch_fund_by_id

fetch_simuwang.polite_sleep = lambda: time.sleep(3.2)

HX_DIR = OUT_DIR / "hongxi"


def main():
    cli = Simuwang(COOKIE_FILE.read_text().strip())
    print(f"已登录 uid={cli.uid}")
    enum = json.loads((OUT_DIR / "company_funds" / "宏锡基金.json").read_text(encoding="utf-8"))
    cta = [f for f in enum["funds"]
           if f["strategy"] == "期货及衍生品策略" and not f["is_liquidate"]]
    # 在售优先, 其次成立时间长
    cta.sort(key=lambda f: (not f["is_daixiao"], f["inception_date"] or "9999"))
    cands = cta[:15]
    HX_DIR.mkdir(parents=True, exist_ok=True)
    ok = skip = fail = 0
    for f in cands:
        fid, name = f["fund_id"], f["name"]
        out = HX_DIR / f"{name.replace('/', '_')}.json"
        if out.exists():
            print(f"[{name}] 已有, 跳过")
            skip += 1
            continue
        print(f"[{name}] ({fid}, 成立{f['inception_date']}, "
              f"{'在售' if f['is_daixiao'] else '不在售'}) 抓取中...")
        try:
            raw = fetch_fund_by_id(cli, fid, name)
            if (raw.get("nav_trend_raw") or {}).get("status") == 0 and \
               "频率" in str((raw.get("nav_trend_raw") or {}).get("msg")):
                print("  触发限频, 冷却90秒重试")
                time.sleep(90)
                raw = fetch_fund_by_id(cli, fid, name)
        except Exception as e:
            print(f"  -> 失败: {e}")
            fail += 1
            continue
        raw["on_sale"] = f["is_daixiao"]
        raw["search_nav_visible"] = f["is_nav_visible"]
        ns = [x for x in (raw.get("nav_series") or []) if x.get("cum_nav") is not None]
        out.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  -> {len(ns)}个净值点" + (f" {ns[0]['date']}~{ns[-1]['date']}" if ns else "(不可得)"))
        ok += 1 if ns else 0
    print(f"\n完成: {ok}只有净值 / {skip}只已有 / {fail}只失败")


if __name__ == "__main__":
    main()
