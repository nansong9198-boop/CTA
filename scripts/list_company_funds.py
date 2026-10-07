#!/usr/bin/env python3
"""按管理人枚举其全部产品(排排网)。

背景: /sun/company/fundList 接口存在但参数未探明(各参数组合均返回空);
公司页 SSR 的 manage_fund_list 为空(客户端渲染)。最终方案: 复用搜索接口
s.simuwang.com/app/fund/ 分页(q=公司关键词, 每页10条, 按相关度排序),
按 company_id 精确过滤——实测可枚举出量派CTA十号2期(HF0000CB7W)等货架外产品。

用法:
    python3 list_company_funds.py 量派            # 按公司名(短名/全名片段)
    python3 list_company_funds.py CO00003KC1      # 按 company_id
    python3 list_company_funds.py --all-pool      # 枚举 manager_info.json 涉及的全部公司

输出:
    终端清单 + data/simuwang/company_funds/<公司简称>.json
    (--all-pool 时汇总 _summary.json, 标注未进评估池的 CTA 策略产品)
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_simuwang
from fetch_simuwang import Simuwang, COOKIE_FILE, OUT_DIR

fetch_simuwang.polite_sleep = lambda: time.sleep(3.2)  # 请求间隔>=3秒

CF_DIR = OUT_DIR / "company_funds"
STRATEGY = {"1001": "股票策略", "1002": "债券策略", "1003": "期货及衍生品策略",
            "1004": "多资产策略", "1005": "组合基金"}
MAX_PAGES = 15
STOP_EMPTY = 2  # 连续N页无该公司新增即停止(相关度排序, 后面都是模糊匹配)


def clean(s):
    return re.sub(r"\[/?h\]", "", s or "")


def search_page(cli, q, page):
    r = cli._get(fetch_simuwang.SEARCH, q=q, search_type="fund,fund_public",
                 page=page, size=10)
    if str(r.get("status")) != "1":
        return []
    return [m for m in r.get("matches", []) if m.get("query_type") == "fund"]


def resolve_company(cli, query):
    """公司名/company_id -> (company_id, 简称)"""
    if re.match(r"^CO[0-9A-Z]{8}$", query):
        # 反查简称(供搜索关键词用)
        for f in list(OUT_DIR.glob("*.json")) + list((OUT_DIR / "companies").glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(d, dict):
                    continue
            except Exception:
                continue
            for ci in ((d.get("detail") or {}).get("companyInfo"), d.get("baseInfo")):
                if isinstance(ci, dict) and ci.get("company_id") == query:
                    return query, ci.get("company_short_name") or ""
        return query, ""
    qn = norm_co(query)
    # 先从已抓产品/公司详情里找(跳过非dict的JSON, 如_summary.json; 括号归一化匹配)
    for f in list(OUT_DIR.glob("*.json")) + list((OUT_DIR / "companies").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                continue
        except Exception:
            continue
        ci = ((d.get("detail") or {}).get("companyInfo") or {})
        if not isinstance(ci, dict) or not ci.get("company_id"):
            bi = d.get("baseInfo")
            ci = bi if isinstance(bi, dict) and bi.get("company_id") else {}
        if not ci:
            continue
        names = [norm_co(ci.get("company_name")), norm_co(ci.get("company_short_name"))]
        if any(qn == n or (n and (qn in n or n in qn)) for n in names):
            return ci["company_id"], ci.get("company_short_name") or query
    # 搜索接口兜底: 取匹配结果中 company_id 的众数
    counts = {}
    for m in search_page(cli, query, 1):
        if query in clean(m.get("company_short_name")) or query in clean(m.get("fund_short_name")):
            counts[m["company_id"]] = counts.get(m["company_id"], 0) + 1
    if counts:
        cid = max(counts, key=counts.get)
        short = next(clean(m.get("company_short_name")) for m in search_page(cli, query, 1)
                     if m["company_id"] == cid)
        return cid, short
    return None, None


def norm_co(s):
    return (s or "").replace("(", "（").replace(")", "）").strip()


def enum_company(cli, cid, short):
    """分页搜索并按 company_id 过滤 -> 产品清单"""
    base = re.sub(r"（.*?）", "", short or "")
    stripped = base
    for suf in ("私募", "投资", "基金", "资产管理", "管理", "资本", "期货", "有限公司"):
        if stripped.endswith(suf):
            stripped = stripped[:-len(suf)]
    queries = list(dict.fromkeys(q for q in [short, base, stripped] if q))
    found = {}
    for q in queries:
        empty_streak = 0
        for page in range(1, MAX_PAGES + 1):
            ms = search_page(cli, q, page)
            time.sleep(3.2)
            new = 0
            for m in ms:
                if m.get("company_id") != cid or m["query_id"] in found:
                    continue
                found[m["query_id"]] = m
                new += 1
            if not ms or (new == 0 and (empty_streak := empty_streak + 1) >= STOP_EMPTY):
                break
            empty_streak = 0
    out = []
    for m in sorted(found.values(), key=lambda x: x.get("inception_date") or ""):
        out.append({
            "fund_id": m["query_id"],
            "name": clean(m.get("fund_short_name")),
            "inception_date": m.get("inception_date"),
            "strategy": STRATEGY.get(str(m.get("first_strategy")), str(m.get("first_strategy"))),
            "register_number": m.get("register_number"),
            "ret_incep": m.get("ret_incep"),
            "is_liquidate": m.get("is_liquidate") == "1",
            "is_nav_visible": m.get("is_nav_visible") == 1,
            "is_daixiao": str(m.get("is_daixiao")) == "1",  # 排排在售(2026-10-07验证)
        })
    return out


def pool_product_ids():
    """已进评估池/已抓取的 fund_id 集合"""
    ids = set()
    for f in list(OUT_DIR.glob("*.json")) + list((OUT_DIR / "siblings").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        bi = (d.get("detail") or {}).get("baseInfo") or {}
        if bi.get("fund_id"):
            ids.add(bi["fund_id"])
        if d.get("fund_id"):
            ids.add(d["fund_id"])
    return ids


def run_one(cli, query, known_ids):
    cid, short = resolve_company(cli, query)
    if not cid:
        print(f"[{query}] 未能解析 company_id")
        return None
    funds = enum_company(cli, cid, short)
    rec = dict(company_id=cid, company_short_name=short, n_funds=len(funds), funds=funds)
    CF_DIR.mkdir(parents=True, exist_ok=True)
    (CF_DIR / f"{short or cid}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    n_cta = sum(1 for f in funds if f["strategy"] == "期货及衍生品策略" and not f["is_liquidate"])
    missed = [f for f in funds if f["strategy"] == "期货及衍生品策略"
              and not f["is_liquidate"] and f["fund_id"] not in known_ids]
    print(f"[{short}({cid})] 产品 {len(funds)} 只, 其中CTA策略 {n_cta} 只, "
          f"未进评估池的CTA {len(missed)} 只")
    for f in missed:
        print(f"    漏网CTA: {f['name']} ({f['fund_id']}, 成立{f['inception_date']}, "
              f"净值{'可见' if f['is_nav_visible'] else '不可见'})")
    return rec


def main():
    cli = Simuwang(COOKIE_FILE.read_text().strip())
    print(f"已登录 uid={cli.uid}")
    known_ids = pool_product_ids()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--all-pool" in sys.argv:
        mi = json.loads((ROOT / "data" / "manager_info.json").read_text(encoding="utf-8"))
        queries = [k for k in mi if k != "_symbol_override"]
        results = []
        for q in queries:
            try:
                rec = run_one(cli, q, known_ids)
            except Exception as e:
                print(f"[{q}] 异常: {e}")
                rec = None
            if rec:
                results.append(rec)
        (CF_DIR / "_summary.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n完成: {len(results)} 家公司, 明细 -> {CF_DIR}/")
        return
    if not args:
        sys.exit("用法: python3 list_company_funds.py <公司名或company_id>... | --all-pool")
    for q in args:
        try:
            run_one(cli, q, known_ids)
        except Exception as e:
            print(f"[{q}] 异常: {e}")


ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    main()
