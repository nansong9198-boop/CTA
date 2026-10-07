#!/usr/bin/env python3
"""从项目数据自动生成 ASR 专有名词词典 data/asr_vocab.json。

数据来源:
- data/simuwang/summary.csv  产品名 / 管理公司 / 管理公司全称
- data/manager_info.json     管理公司全称
- 内置人物名表与金融术语谐音纠错规则
"""
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "asr_vocab.json"

# 人物名表（基金经理 / 路演嘉宾）
PERSON_NAMES = [
    "史江辉", "徐吟嘉", "孙林", "徐书楠", "刘恒一", "刘骅飞", "刘锡斌",
    "谢冬", "潘正寰", "程余", "林华", "王凯", "王思达", "司维", "朱晓康",
]

# 人物名常见谐音误转 → 正确写法（只收录明确错误、不误伤正常用语的）
PERSON_CORRECTIONS = {
    "石江辉": "史江辉", "史江飞": "史江辉", "施江辉": "史江辉",
    "史江晖": "史江辉", "史佳辉": "史江辉", "史加辉": "史江辉",
    "徐银嘉": "徐吟嘉", "徐银佳": "徐吟嘉", "徐吟佳": "徐吟嘉",
    "刘华飞": "刘骅飞", "刘桦飞": "刘骅飞",
    "刘锡彬": "刘锡斌", "刘西斌": "刘锡斌",
    "刘恒毅": "刘恒一",
    "潘正环": "潘正寰", "潘正桓": "潘正寰",
    "徐蜀楠": "徐书楠", "徐舒楠": "徐书楠",
    "朱小康": "朱晓康",
}

# 金融术语常见谐音误转 → 正确写法
TERM_CORRECTIONS = {
    "十金": "拾金", "时金": "拾金", "石金": "拾金",  # 国源信达"拾金"系列
    "止增": "指增", "只增": "指增", "纸增": "指增",  # 指数增强
    "中政": "中证", "中正": "中证",          # 中证500/1000
    "回彻": "回撤", "回澈": "回撤",
    "夏谱": "夏普", "下普": "夏普",
    "静值": "净值",
    "年话": "年化",
    "高水卫": "高水位", "高水味": "高水位",
    "阿尔发": "阿尔法",
    "背塔": "贝塔", "贝它": "贝塔",
    "申够": "申购", "赎会": "赎回",
    "量话": "量化",
    "超鳄": "超额",
    "私募金": "私募",
    "拍拍网": "排排网", "排牌网": "排排网",
    "国元信达": "国源信达", "国原信达": "国源信达",
    "国院西南": "国源信达", "国院信达": "国源信达",
    "谢百森": "谢百三",
}


def is_company_name(s: str) -> bool:
    """过滤出公司全称：含‘公司/合伙’且不含网址与长度异常的备注。"""
    if not s or "http" in s or len(s) < 6 or len(s) > 40:
        return False
    return ("公司" in s or "合伙" in s) and re.fullmatch(r"[一-鿿（）()A-Za-z0-9]+", s) is not None


def short_name(full: str) -> str:
    """从公司全称提取常用简称：去城市前缀与组织后缀。"""
    s = re.sub(r"^(北京|上海|深圳|广州|杭州|厦门|广东|珠海|海南|宁波|成都|南京|天津|重庆|浙江|江苏|青岛|武汉|西安)", "", full)
    s = re.sub(r"(私募基金管理|私募证券基金管理|资产管理|基金管理|投资管理|投资|资本管理|资本)?(有限公司|有限责任公司|合伙企业（有限合伙）|合伙企业\(有限合伙\))$", "", s)
    return s if 2 <= len(s) <= 10 else ""


def main() -> None:
    companies_full, companies_short, products = set(), set(), set()

    csv_path = ROOT / "data" / "simuwang" / "summary.csv"
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            p = (row.get("产品名") or "").strip()
            if p:
                products.add(p)
            short = (row.get("管理公司") or row.get("优先级公司关键词") or "").strip()
            if short:
                companies_short.add(short)
            full = (row.get("管理公司全称") or "").strip()
            if is_company_name(full):
                companies_full.add(full)

    mi = json.load(open(ROOT / "data" / "manager_info.json"))
    for key, val in mi.items():
        if key == "_symbol_override" and isinstance(val, dict):
            for v in val.values():
                if is_company_name(v):
                    companies_full.add(v)
        elif is_company_name(key):
            companies_full.add(key)

    for full in companies_full:
        sn = short_name(full)
        if sn:
            companies_short.add(sn)

    corrections = dict(TERM_CORRECTIONS)
    corrections.update(PERSON_CORRECTIONS)
    # 纠错目标词不得再被其他规则替换，避免链式误改
    assert not any(v in corrections for v in corrections.values() if isinstance(v, str))

    vocab = {
        "_comment": "ASR 专有名词词典，由 scripts/build_asr_vocab.py 自动生成；corrections 为转写后文本替换规则（误转→正确）",
        "company_names": sorted(companies_short | companies_full),
        "product_names": sorted(products),
        "person_names": PERSON_NAMES,
        "corrections": corrections,
    }
    OUT.write_text(json.dumps(vocab, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写入 {OUT}: 公司 {len(vocab['company_names'])}，产品 {len(products)}，人物 {len(PERSON_NAMES)}，纠错规则 {len(corrections)}")


if __name__ == "__main__":
    main()
