#!/usr/bin/env python3
"""提取蛋卷基金「管理期货(CTA)」私募货架产品列表。

用法:
    python3 extract_danjuan_cta.py '<cookie字符串>'

Cookie 获取方式:
    1. 浏览器打开 https://danjuanfunds.com 并登录
    2. F12 -> Application/存储 -> Cookies -> https://danjuanfunds.com
    3. 复制 xq_a_token 的值即可(也可以整段 cookie 粘贴)
输出:
    danjuan_cta_page*.json  原始接口返回
    danjuan_cta.csv         展平后的产品表
"""
import csv
import json
import sys
import time

import requests

API = "https://danjuanfunds.com/djapi/fundx/activity/x/web/c/index/dataByCode"
MODULE_CODE = "glqh"  # 管理期货(CTA)


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python3 extract_danjuan_cta.py '<cookie或xq_a_token值>'")
    cookie = sys.argv[1].strip()
    if "=" not in cookie:
        cookie = f"xq_a_token={cookie}"

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Cookie": cookie,
        "use-request-urlencoded": "true",
        "Referer": "https://danjuanfunds.com/rn/pf-shelves/all?tab=0&moduleCode=glqh",
    })

    all_funds = []
    page = 1
    total_page = 1
    while page <= total_page:
        r = s.post(API, data={
            "code": "FUND_PRIVATE_PRODUCT_MARKET_ALL_SHELF",
            "module_code": MODULE_CODE,
            "page_no": page,
        }, timeout=15)
        j = r.json()
        if j.get("result_code") != 0:
            sys.exit(f"接口报错: {j.get('result_code')} {j.get('message')}"
                     " —— 请检查 cookie 是否有效/已登录")
        data = j["data"]
        with open(f"data/danjuan_cta_page{page}.json", "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False, indent=2)
        total_page = data.get("total_page_count", 1)
        funds = data.get("fund_datas") or []
        print(f"第 {page}/{total_page} 页, {len(funds)} 条 (累计 {len(all_funds) + len(funds)}/{data.get('total_count')})")
        all_funds.extend(funds)
        page += 1
        time.sleep(0.5)

    if not all_funds:
        sys.exit("没有拿到任何产品数据")

    keys = sorted({k for row in all_funds for k in row})
    with open("danjuan_cta.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for row in all_funds:
            w.writerow({k: (json.dumps(v, ensure_ascii=False)
                            if isinstance(v, (dict, list)) else v)
                        for k, v in row.items()})
    print(f"完成: {len(all_funds)} 只产品 -> danjuan_cta.csv")
    print("字段:", ", ".join(keys))


if __name__ == "__main__":
    main()
