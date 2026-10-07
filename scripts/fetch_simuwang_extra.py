#!/usr/bin/env python3
"""排排网补充抓取: 公司详情 + 同策略长样本产品(siblings)。

用法:
    python3 fetch_simuwang_extra.py            # 公司 + siblings 全部
    python3 fetch_simuwang_extra.py --companies
    python3 fetch_simuwang_extra.py --siblings

输出:
    data/simuwang/companies/<公司简称>.json   公司详情(baseInfo+profile, 含原始响应)
    data/simuwang/siblings/<产品名>.json      同策略长样本产品(结构同主抓取)
    终端简报: 公司规模区间 vs manager_info.json 对比表; sibling 清单
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_simuwang import (Simuwang, COOKIE_FILE, OUT_DIR, TARGETS,
                            decrypt_payload, flat_metrics, polite_sleep,
                            row_from_raw, write_summary)

ROOT = Path(__file__).resolve().parent.parent
COMP_DIR = OUT_DIR / "companies"
SIB_DIR = OUT_DIR / "siblings"

# manager_info.json 里的公司 -> (搜索关键词, 公司名匹配关键词); 已抓产品能提供的后面自动覆盖
COMPANY_SEARCH = {
    "上海黑翼资产管理有限公司": ("黑翼", "黑翼"),
    "上海千象资产管理有限公司": ("千象", "千象"),
    "上海富善投资有限公司": ("富善", "富善"),
    "北京佑维投资管理有限公司": ("佑维", "佑维"),
    "上海文谛资产管理有限公司": ("文谛", "文谛"),
    "南华期货股份有限公司": ("南华商品指数", "南华"),
    "上海悬铃私募基金管理有限公司": ("悬铃", "悬铃"),
    "北京安贤私募基金管理有限公司": ("安贤", "安贤"),
}

# sibling 检索计划: (管理人, [候选搜索词], 排除已抓主池产品ID)
SIBLING_PLANS = [
    ("量派", ["量派CTA二号", "量派CTA三号", "量派CTA五号", "量派CTA一号"]),
    ("因诺", ["因诺CTA1号", "因诺CTA"]),
    ("博衍", ["博衍九溪CTA"]),
    ("宏锡", ["宏锡量化CTA"]),
    ("明睿", ["明睿CTA", "明睿管理期货", "明睿"]),
]


def norm(name: str) -> str:
    return (name or "").replace("（", "(").replace("）", ")")


def company_ids_from_products() -> dict:
    """已抓产品 JSON 里的 companyInfo -> {规范化公司名: company_id}"""
    out = {}
    for f in sorted(OUT_DIR.glob("*.json")):
        ci = ((json.load(open(f)).get("detail") or {}).get("companyInfo") or {})
        if ci.get("company_id"):
            out[norm(ci.get("company_name"))] = ci["company_id"]
    return out


def find_company_id(cli: Simuwang, kw: str, company_kw: str):
    for m in cli.search(kw):
        if company_kw in (m.get("company_short_name") or ""):
            return m.get("company_id"), m.get("company_short_name")
    return None, None


def fetch_company(cli: Simuwang, company_id: str) -> dict:
    raw = {"company_id": company_id}
    r = cli._get("https://sppwapi.simuwang.com/sun/company/baseInfo", id=company_id)
    raw["baseInfo_raw"] = r
    raw["baseInfo"] = decrypt_payload(r["data"]) if r.get("status") == 1 else None
    polite_sleep()
    r = cli._get("https://sppwapi.simuwang.com/sun/company/profile", id=company_id)
    raw["profile_raw"] = r
    raw["profile"] = decrypt_payload(r["data"]) if r.get("status") == 1 else None
    polite_sleep()
    return raw


def run_companies(cli: Simuwang):
    mi = json.load(open(ROOT / "data" / "manager_info.json"))
    known = company_ids_from_products()
    COMP_DIR.mkdir(parents=True, exist_ok=True)

    print("\n=== 公司详情: 排排网 vs manager_info.json ===")
    print(f"{'公司':<28} {'排排网规模':<10} {'排排网成立':<12} {'备案号':<10} {'产品数':<6} 现值")
    results = []
    for name, info in mi.items():
        if name == "_symbol_override":
            continue
        cid = known.get(norm(name))
        if not cid:
            kw, ckw = COMPANY_SEARCH.get(name, (name[:4], name[2:4]))
            cid, _sn = find_company_id(cli, kw, ckw)
            polite_sleep()
        if not cid:
            print(f"{name:<28} 未找到公司ID")
            results.append({"company": name, "error": "未找到公司ID"})
            continue
        try:
            raw = fetch_company(cli, company_id=cid)
        except Exception as e:
            print(f"{name:<28} 抓取失败: {e}")
            results.append({"company": name, "company_id": cid, "error": str(e)})
            continue
        bi = raw.get("baseInfo") or {}
        short = bi.get("company_short_name") or name[:6]
        (COMP_DIR / f"{short}.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        row = {
            "company": name,
            "company_id": cid,
            "matched_name": bi.get("company_name"),
            "asset_size": bi.get("company_asset_size"),
            "establish_date": bi.get("establish_date"),
            "register_number": bi.get("register_number"),
            "fund_cnt": bi.get("cnt"),
            "city": bi.get("registered_city") or bi.get("city"),
            "address": bi.get("company_address"),
            "current_aum_text": info.get("aum_text"),
        }
        results.append(row)
        name_ok = "✓" if norm(bi.get("company_name")) == norm(name) else f"✗{bi.get('company_name')}"
        print(f"{name[:26]:<28} {str(bi.get('company_asset_size')):<10} "
              f"{str(bi.get('establish_date')):<12} {str(bi.get('register_number')):<10} "
              f"{str(bi.get('cnt')):<6} {info.get('aum_text','')[:30]} {name_ok}")
    (COMP_DIR / "_summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return results


def pick_sibling(matches, company_kw: str, exclude_ids: set, want_cta=True):
    """从搜索结果挑同公司、CTA策略、成立最早且不在主池的产品。"""
    cands = []
    for m in matches:
        if m.get("query_id") in exclude_ids:
            continue
        if company_kw not in (m.get("company_short_name") or ""):
            continue
        if want_cta and str(m.get("first_strategy")) != "1003":  # 期货及衍生品策略
            continue
        if m.get("is_liquidate") == "1":
            continue
        cands.append(m)
    cands.sort(key=lambda m: m.get("inception_date") or "9999")
    return cands


def fetch_fund_by_id(cli: Simuwang, fund_id: str, name: str) -> dict:
    raw = {"target": name, "fund_id": fund_id}
    detail_raw, detail = cli.detail(fund_id)
    polite_sleep()
    raw["detail_raw"], raw["detail"] = detail_raw, detail
    idx_raw, idx = cli.index_info(fund_id)
    polite_sleep()
    raw["index_info_raw"], raw["index_info"] = idx_raw, idx
    nav_raw, nav = cli.nav_trend(fund_id)
    polite_sleep()
    raw["nav_trend_raw"], raw["nav_trend"] = nav_raw, nav
    if nav and nav.get("categories"):
        series = (nav.get("data") or {}).get(fund_id, {})
        raw["nav_series"] = [
            {"date": d, "cum_ret_pct": r_,
             "cum_nav": round(1 + r_ / 100, 6) if isinstance(r_, (int, float)) else None}
            for d, r_ in zip(nav["categories"], series.get("ret") or [])
        ]
    return raw


def run_siblings(cli: Simuwang):
    exclude = set()
    for f in OUT_DIR.glob("*.json"):
        d = json.load(open(f))
        bi = (d.get("detail") or {}).get("baseInfo") or {}
        if bi.get("fund_id"):
            exclude.add(bi["fund_id"])
    SIB_DIR.mkdir(parents=True, exist_ok=True)

    picked = {}  # fund_id -> raw
    rows = []
    print("\n=== 同策略长样本产品(siblings) ===")
    for company_kw, keywords in SIBLING_PLANS:
        found = []
        seen_ids = set()
        for kw in keywords:
            matches = cli.search(kw)
            polite_sleep()
            cands = pick_sibling(matches, company_kw, exclude | seen_ids | set(picked))
            # 显式点名(如"量派CTA二号")优先精确/包含匹配, 系列词取最早2只
            exact = [m for m in cands if kw in (m.get("fund_short_name") or "")]
            for m in (exact or cands[:2]):
                if m["query_id"] not in seen_ids:
                    seen_ids.add(m["query_id"])
                    found.append(m)
        for m in found[:3]:  # 每个管理人最多留3只
            fid, fname = m["query_id"], m.get("fund_short_name")
            print(f"[{company_kw}] {fname} ({fid}, 成立 {m.get('inception_date')}) 抓取中...")
            try:
                raw = fetch_fund_by_id(cli, fid, fname)
            except Exception as e:
                print(f"  -> 失败: {e}")
                continue
            picked[fid] = raw
            (SIB_DIR / f"{fname.replace('/', '_')}.json").write_text(
                json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
            row = row_from_raw(raw, fname, company_kw)
            rows.append(row)
            print(f"  -> 年化 {row.get('年化收益%')}% 回撤 {row.get('最大回撤%')}% "
                  f"夏普 {row.get('夏普(成立来)')} 净值 {row.get('净值起始')}~{row.get('净值截止')}"
                  f"({row.get('净值点数')}点)")
        if not found:
            print(f"[{company_kw}] 未找到更长的同策略产品")
    if rows:
        write_summary(rows, SIB_DIR / "summary.csv")
    return rows


def main():
    cli = Simuwang(COOKIE_FILE.read_text().strip())
    print(f"已登录 uid={cli.uid}")
    args = set(sys.argv[1:])
    do_all = not ({"--companies", "--siblings"} & args)
    if do_all or "--companies" in args:
        run_companies(cli)
    if do_all or "--siblings" in args:
        run_siblings(cli)


if __name__ == "__main__":
    main()
