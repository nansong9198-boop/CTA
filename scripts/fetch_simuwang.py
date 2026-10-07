#!/usr/bin/env python3
"""抓取私募排排网(simuwang.com)产品数据,用于与蛋卷基金数据交叉验证。

用法:
    python3 fetch_simuwang.py                    # 抓取全部目标产品
    python3 fetch_simuwang.py 量派CTA七号C        # 只抓指定产品(可多个)
    python3 fetch_simuwang.py --rebuild-summary  # 离线从已存 JSON 重建 summary.csv

Cookie:
    从 data/simuwang_cookie.txt 读取(已 gitignore,勿提交)。

依赖:
    requests、cryptography(AES 解密响应)、node(执行响应内嵌的 key 混淆 JS)。

接口(均为登录态 Cookie 认证):
    搜索  GET  https://s.simuwang.com/app/fund/?q=<kw>&search_type=fund,fund_public
    详情  GET  https://sppwapi.simuwang.com/sun/fund/detail?id=<fund_id>
    指标  GET  https://sppwapi.simuwang.com/sun/fund/fundIndexInfoV2?id=<fund_id>&index_id=&time_range=FromSetup
    净值  POST https://sppwapi.simuwang.com/sun/chart/fundNavTrend
               表单: sdata=<双层base64(AES-256-CBC(JSON))> + USER_ID=<uid>
               请求头 X-Requested-With = Cookie 中 8hIn9IA 的值
               加密 key = md5(该值) 的 hex 字符串(32字节), iv = key[16:32]
    响应统一为 {"data":{"encode":N,"data":<密文>,"key":<JS>,"id":<var>}}:
      key 是一段混淆 JS, 求值后给 window[id] 赋值得到密钥 k;
      encode=3..10 对 k 做字符串变换(反转/截断), encode>2 时
      明文 = AES-256-CBC(md5(k), iv=md5(k)[16:32]) 解密双层 base64 密文;
      encode<=2 时为 XOR + base64。

输出:
    data/simuwang/<产品名>.json   详情+指标+净值序列(解密后)及原始响应
    data/simuwang/summary.csv     汇总表
"""
import csv
import base64
import hashlib
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent.parent
COOKIE_FILE = ROOT / "data" / "simuwang_cookie.txt"
OUT_DIR = ROOT / "data" / "simuwang"

API = "https://sppwapi.simuwang.com"
SEARCH = "https://s.simuwang.com/app/fund/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DEFAULT_TOKEN = "814a43980a8952c35b75d1502c61ca84"  # 前端 JS 内置默认 key

# (目标产品名, 蛋卷侧管理公司关键词, 优先级)
TARGETS = [
    ("量派CTA七号C", "量派", 1),
    ("因诺CTA2号B", "因诺", 1),
    ("明睿骁云策略", "明睿", 1),
    ("量派聚核20号", "量派", 1),
    ("宏锡量化CTA7号", "宏锡", 1),
    ("润洲正行一号A", "润洲", 1),
    ("思瑞二号", "思瑞", 1),
    ("量派CTA八号C", "量派", 1),
    ("旭诺CTA一号", "旭诺", 1),
    ("博衍九溪CTA2号A", "博衍", 1),
    ("博衍九溪CTA1号", "博衍", 2),
    ("洛书裕和延平", "洛书", 2),
    ("远澜银杏1号", "远澜", 2),
    ("均成CTA增强25号1期", "均成", 2),
    ("博孚利CTA5号", "博孚利", 2),
    ("瑞达期货-瑞智进取共赢5号", "瑞达", 2),
    ("宏锡量化CTA7号二期", "宏锡", 2),
    ("会世元丰CTA2号", "会世", 2),
    ("洛书裕和建安", "洛书", 2),
    ("远澜云杉2号", "远澜", 2),
    ("洛书管理期货拾壹号", "洛书", 2),
    ("润洲复利一号A", "润洲", 2),
    ("量道CTA精选1号", "量道", 2),
    ("信达诚睿进取1号", "信达", 2),
]


def polite_sleep():
    time.sleep(1.5)


def aes_cbc(key_hex: str, data: bytes, decrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key_hex.encode()),
                    modes.CBC(key_hex[16:32].encode()))
    ctx = cipher.decryptor() if decrypt else cipher.encryptor()
    return ctx.update(data) + ctx.finalize()


def encrypt_payload(obj, token: str) -> str:
    """前端 Crypto.encrypt: AES-256-CBC(md5(token)) -> base64 -> 再 base64。"""
    token_hex = hashlib.md5(token.encode()).hexdigest() if token else DEFAULT_TOKEN
    raw = json.dumps(obj, separators=(",", ":")).encode()
    pad = 16 - len(raw) % 16
    ct = aes_cbc(token_hex, raw + bytes([pad]) * pad, decrypt=False)
    return base64.b64encode(base64.b64encode(ct)).decode()


def _eval_key_js(key_js: str, var_id: str) -> str:
    """响应里的 key 字段是混淆 JS, 执行后给 window[var_id] 赋值。"""
    script = f"var window={{}};{key_js};process.stdout.write(String(window[{json.dumps(var_id)}]));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError(f"node 执行 key JS 失败: {out.stderr[:200]}")
    return out.stdout


def decrypt_payload(payload):
    """解密排排网接口响应的 data 字段; 非加密结构原样返回。"""
    if not isinstance(payload, dict) or "encode" not in payload:
        return payload
    encode = payload.get("encode")
    key = _eval_key_js(payload["key"], payload["id"])
    n = len(key)
    transforms = {
        3: key[::-1], 4: key[2:], 5: key[:-2], 6: key[1:n - 1],
        7: key[2:n - 1], 8: key[1:n - 2], 9: key[0] + key[2:],
        10: key[:n - 2] + key[n - 1],
    }
    k = transforms.get(encode, key)
    if isinstance(encode, int) and encode > 2:
        ct = base64.b64decode(payload["data"])
        # CryptoJS 会把 atob 结果再按 OpenSSL base64 解析一次 -> 双层 base64
        try:
            inner = ct.decode("ascii")
            if len(inner) % 4 == 0:
                ct = base64.b64decode(inner, validate=True)
        except (UnicodeDecodeError, ValueError):
            pass
        pt = aes_cbc(hashlib.md5(k.encode()).hexdigest(), ct, decrypt=True)
        return json.loads(pt[:-pt[-1]])
    raw = base64.b64decode(payload["data"])
    xored = bytes(b ^ ord(k[i % len(k)]) for i, b in enumerate(raw))
    return json.loads(base64.b64decode(xored))


class Simuwang:
    def __init__(self, cookie: str):
        self.session = requests.Session()
        cookies = dict(p.split("=", 1) for p in cookie.split("; ") if "=" in p)
        self.token = urllib.parse.unquote(cookies["8hIn9IA"])
        self.session.headers.update({
            "User-Agent": UA,
            "Cookie": cookie,
            "Referer": "https://dc.simuwang.com/",
            "X-Requested-With": self.token,
        })
        self.uid = self._fetch_uid()

    def _get(self, url, **params):
        r = self.session.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def _fetch_uid(self) -> str:
        r = self._get(f"{API}/sun/member/getUserInfoApi")
        if r.get("status") != 1:
            raise RuntimeError(f"获取用户信息失败(Cookie 可能失效): {r.get('msg')}")
        return str(decrypt_payload(r["data"])["uid"])

    def search(self, keyword: str):
        r = self._get(SEARCH, q=keyword, search_type="fund,fund_public", page=1, size=10)
        if str(r.get("status")) != "1":
            return []
        for m in r.get("matches", []):
            for f in ("fund_short_name", "query_name"):
                if isinstance(m.get(f), str):
                    m[f] = m[f].replace("[h]", "").replace("[/h]", "")
        return [m for m in r.get("matches", []) if m.get("query_type") == "fund"]

    def detail(self, fund_id: str):
        r = self._get(f"{API}/sun/fund/detail", id=fund_id)
        return r, decrypt_payload(r["data"]) if r.get("status") == 1 else None

    def index_info(self, fund_id: str, time_range="FromSetup"):
        r = self._get(f"{API}/sun/fund/fundIndexInfoV2",
                      id=fund_id, index_id="", time_range=time_range)
        return r, decrypt_payload(r["data"]) if r.get("status") == 1 else None

    def nav_trend(self, fund_id: str):
        sdata = encrypt_payload({"fund_id": fund_id, "is_dynamic": 1}, self.token)
        r = self.session.post(f"{API}/sun/chart/fundNavTrend",
                              data={"sdata": sdata, "USER_ID": self.uid},
                              headers={"Content-Type": "application/x-www-form-urlencoded"},
                              timeout=20)
        r.raise_for_status()
        r = r.json()
        return r, decrypt_payload(r["data"]) if r.get("status") == 1 else None


def pick_match(target: str, company_kw: str, matches):
    """按名称/公司挑选最可能的候选, 返回 (match, note)。"""
    if not matches:
        return None, "搜索无结果"
    def name_of(m):
        return m.get("fund_short_name", "")
    exact = [m for m in matches if name_of(m) == target]
    if len(exact) == 1:
        return exact[0], "名称精确匹配"
    if exact:
        c = [m for m in exact if company_kw and company_kw in m.get("company_short_name", "")]
        return (c or exact)[0], "名称精确匹配(多家同名,按公司筛选)" if c else "名称精确匹配(多家同名,取首个)"
    contains = [m for m in matches if target in name_of(m) or name_of(m) in target]
    c = [m for m in contains if company_kw and company_kw in m.get("company_short_name", "")]
    if c:
        return c[0], f"名称模糊+公司匹配: {name_of(c[0])}"
    if contains:
        return contains[0], f"名称模糊匹配(未匹配公司): {name_of(contains[0])}"
    c = [m for m in matches if company_kw and company_kw in m.get("company_short_name", "")]
    if len(c) == 1:
        return c[0], f"仅公司匹配: {name_of(c[0])}"
    return None, "无可靠匹配: " + "; ".join(f"{name_of(m)}({m.get('company_short_name')})" for m in matches[:5])


def flat_metrics(index_info: dict) -> dict:
    """fundIndexInfoV2 返回分组指标列表, 展平成 {key: value}。"""
    out = {}
    if isinstance(index_info, dict):
        for group in index_info.values():
            if isinstance(group, list):
                for item in group:
                    if isinstance(item, dict) and item.get("key"):
                        out[item["key"]] = item.get("value")
    return out


def fee_text(fee_group) -> str:
    """feeList 里的费率组 -> 可读文本, 如 '持有期限<180天:1%; 持有期限≥180天:0%'。"""
    if not isinstance(fee_group, dict):
        return ""
    parts = [f"{f.get('limit')}:{f.get('fee')}" if f.get("limit") else str(f.get("fee"))
             for f in fee_group.get("fee", []) if isinstance(f, dict)]
    return "; ".join(parts)


def element_fields(detail: dict) -> dict:
    """从 detail.elementInfo / feeList / recentOpenDate 提取产品要素列。"""
    if not detail:
        return {}
    ei = detail.get("elementInfo") or {}
    fees = detail.get("feeList") or {}
    rod = detail.get("recentOpenDate") or {}
    return {
        "管理费": ei.get("management_fee_text") or "",
        "托管外包费": "".join(filter(None, [ei.get("managementfee_bank_text") or "",
                                          ei.get("outsourcing_fee_text") or ""])),
        "业绩报酬": ei.get("performance_fee_text") or "",
        "认购费": fee_text(fees.get("subscription")),
        "申购费": fee_text(fees.get("purchase")),
        "赎回费": fee_text(fees.get("redeem")),
        "封闭期": ei.get("lockup_period_text") or "",
        "锁定期": ei.get("lock_period_text") or "",
        "开放日": "; ".join(filter(None, [ei.get("open_day_text") or "",
                                        ei.get("redemption_day_text") or ""])),
        "近期开放日": ",".join((rod.get("recent") or [])[:4]),
        "预警线": ei.get("guard_line_text") or "",
        "止损线": ei.get("stop_loss_line_text") or "",
        "起购金额": ei.get("min_investment_share_text") or "",
    }


def row_from_raw(raw: dict, target: str, company_kw: str, match_note: str = "") -> dict:
    """从已保存的原始 JSON 重建汇总行(离线, 不发请求)。"""
    row = {"产品名": target, "优先级公司关键词": company_kw}
    detail = raw.get("detail")
    if not detail:
        row["状态"] = raw.get("error", "无数据")
        return row
    bi = detail.get("baseInfo", {})
    mgr = (detail.get("relationManager") or [{}])[0]
    comp = detail.get("companyInfo", {})
    row.update({
        "匹配说明": match_note,
        "排排网ID": bi.get("fund_id"),
        "排排网名称": bi.get("fund_short_name"),
        "管理公司": comp.get("company_short_name"),
        "成立日期": bi.get("inception_date"),
        "净值频率": bi.get("nav_frequency"),
        "基金经理": bi.get("managers_name") or mgr.get("personnel_name"),
        "管理公司全称": comp.get("company_name") or bi.get("trust_name"),
        "策略": "/".join(filter(None, [bi.get("first_strategy"), bi.get("second_strategy")])),
        "最新净值日期": bi.get("full_price_date"),
        "最新规模(万)": bi.get("fund_asset_size"),
        "基金状态": bi.get("fund_status"),
        "累计收益%": bi.get("ret_incep"),
        "年化收益%": bi.get("ret_incep_a"),
        "近1年收益%": bi.get("ret_1y"),
        "今年来收益%": bi.get("ret_ytd"),
        "最大回撤%": bi.get("maxdrawdown_incep"),
        "夏普(成立来)": bi.get("sharperatio_incep"),
        "夏普(近1年)": bi.get("sharperatio_1y"),
    })
    met = flat_metrics(raw.get("index_info"))
    for k, label in [("stddev", "年化波动率%"), ("sortinoratio", "索提诺"),
                     ("calmarratio", "卡玛比率"), ("alpha", "Alpha%")]:
        if met.get(k) not in (None, "--"):
            row[label] = met[k]
    nav = raw.get("nav_trend")
    if nav and nav.get("categories"):
        cats = nav["categories"]
        row["净值点数"] = len(cats)
        row["净值起始"] = cats[0]
        row["净值截止"] = cats[-1]
    row.update(element_fields(detail))
    row["状态"] = "成功"
    return row


def write_summary(rows: list, path: Path):
    cols = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def fetch_one(cli: Simuwang, target: str, company_kw: str) -> dict:
    row = {"产品名": target, "优先级公司关键词": company_kw}
    raw = {"target": target}
    matches = cli.search(target)
    polite_sleep()
    raw["search_matches"] = matches
    match, note = pick_match(target, company_kw, matches)
    row["匹配说明"] = note
    if not match:
        row["状态"] = "未找到"
        return row, raw
    fund_id = match["query_id"]
    row.update({
        "排排网ID": fund_id,
        "排排网名称": match.get("fund_short_name"),
        "管理公司": match.get("company_short_name"),
    })

    detail_raw, detail = cli.detail(fund_id)
    polite_sleep()
    raw["detail_raw"] = detail_raw
    raw["detail"] = detail
    if not detail:
        row["状态"] = f"详情失败: {detail_raw.get('msg')}"
        return row, raw
    bi = detail.get("baseInfo", {})
    mgr = (detail.get("relationManager") or [{}])[0]
    comp = detail.get("companyInfo", {})
    row.update({
        "成立日期": bi.get("inception_date"),
        "净值频率": bi.get("nav_frequency"),
        "基金经理": bi.get("managers_name") or mgr.get("personnel_name"),
        "管理公司全称": comp.get("company_name") or bi.get("trust_name"),
        "策略": "/".join(filter(None, [bi.get("first_strategy"), bi.get("second_strategy")])),
        "最新净值日期": bi.get("full_price_date"),
        "最新规模(万)": bi.get("fund_asset_size"),
        "基金状态": bi.get("fund_status"),
        "累计收益%": bi.get("ret_incep"),
        "年化收益%": bi.get("ret_incep_a"),
        "近1年收益%": bi.get("ret_1y"),
        "今年来收益%": bi.get("ret_ytd"),
        "最大回撤%": bi.get("maxdrawdown_incep"),
        "夏普(成立来)": bi.get("sharperatio_incep"),
        "夏普(近1年)": bi.get("sharperatio_1y"),
    })

    idx_raw, idx = cli.index_info(fund_id)
    polite_sleep()
    raw["index_info_raw"] = idx_raw
    raw["index_info"] = idx
    met = flat_metrics(idx)
    for k, label in [("stddev", "年化波动率%"), ("sortinoratio", "索提诺"),
                     ("calmarratio", "卡玛比率"), ("alpha", "Alpha%")]:
        if met.get(k) not in (None, "--"):
            row[label] = met[k]

    nav_raw, nav = cli.nav_trend(fund_id)
    polite_sleep()
    raw["nav_trend_raw"] = nav_raw
    raw["nav_trend"] = nav
    if nav and nav.get("categories"):
        cats = nav["categories"]
        series = (nav.get("data") or {}).get(fund_id, {})
        rets = series.get("ret") or []
        # ret 为累计收益%, 换算累计净值
        nav_points = [
            {"date": d, "cum_ret_pct": r,
             "cum_nav": round(1 + r / 100, 6) if isinstance(r, (int, float)) else None}
            for d, r in zip(cats, rets)
        ]
        raw["nav_series"] = nav_points
        row["净值点数"] = len(nav_points)
        row["净值起始"] = cats[0]
        row["净值截止"] = cats[-1]
    row.update(element_fields(detail))
    row["状态"] = "成功"
    return row, raw


def main():
    if "--rebuild-summary" in sys.argv:
        # 离线模式: 从已保存的 data/simuwang/*.json 重建 summary.csv(补新列, 不发请求)
        kw_map = {t: kw for t, kw, _ in TARGETS}
        rows = []
        for f in sorted(OUT_DIR.glob("*.json")):
            raw = json.loads(f.read_text(encoding="utf-8"))
            target = raw.get("target") or f.stem
            rows.append(row_from_raw(raw, target, kw_map.get(target, "")))
        write_summary(rows, OUT_DIR / "summary.csv")
        print(f"已从 {len(rows)} 个本地 JSON 重建 {OUT_DIR/'summary.csv'}")
        return

    if not COOKIE_FILE.exists():
        sys.exit(f"未找到 {COOKIE_FILE}, 请先写入登录 Cookie")
    cookie = COOKIE_FILE.read_text().strip()
    cli = Simuwang(cookie)
    print(f"已登录 uid={cli.uid}")

    only = set(a for a in sys.argv[1:] if not a.startswith("--"))
    targets = [t for t in TARGETS if not only or t[0] in only]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    for target, kw, _prio in targets:
        print(f"[{target}] 搜索+抓取中...")
        try:
            row, raw = fetch_one(cli, target, kw)
        except Exception as e:
            row, raw = {"产品名": target, "状态": f"异常: {e}"}, {"target": target, "error": str(e)}
        rows.append(row)
        safe = target.replace("/", "_")
        (OUT_DIR / f"{safe}.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  -> {row.get('状态')} | {row.get('匹配说明','')} | {row.get('排排网ID','')}")

    if rows:
        write_summary(rows, OUT_DIR / "summary.csv")
        ok = sum(1 for r in rows if r.get("状态") == "成功")
        print(f"\n完成: {ok}/{len(rows)} 成功, 汇总 -> {OUT_DIR/'summary.csv'}")


if __name__ == "__main__":
    main()
