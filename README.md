# CTA 私募评估体系

蛋卷基金「管理期货(CTA)」私募货架产品的量化筛选与评估工具。

## 功能

- 从蛋卷基金货架接口拉取 CTA 私募产品列表及季度收益序列
- 计算全量指标：年化收益、最大回撤、夏普、索提诺、卡玛、胜率、盈亏比、新高占比、危机阿尔法（三指数并集）、回撤形态（水下时长/连亏）、股票敞口相关性
- 三层评估：硬门槛（假CTA/股票敞口/样本不足）→ 指标加权 → 短样本可靠性折扣
- 自动生成逐只产品的评估报告（推荐/备选/观察/不推荐 + 理由）
- 独立审计脚本：数据一致性检查 + 得分构成复核

## 用法

```bash
cd ~/work/cta_eval
python3 scripts/extract_danjuan_cta.py '<蛋卷Cookie>'   # 1. 拉取货架数据
python3 scripts/analyze_cta.py                          # 2. 评估并生成 docs/danjuan_cta_report.md
python3 scripts/audit_cta.py                            # 3. 独立审计复核
```

Cookie 获取：浏览器登录 danjuanfunds.com → F12 → Network → 任一 djapi 请求 → 复制 Request Headers 的 Cookie 整段。

## 目录结构

```
├── scripts/   extract_danjuan_cta.py / analyze_cta.py / audit_cta.py
├── docs/      cta_evaluation_plan.md(方案) / cta_evaluation_review.md(专业审查) / danjuan_cta_report.md(自动报告)
└── data/      原始json、各层指标csv、指数基准缓存
```

## 依赖

仅 `requests`（`pip install requests`），其余为标准库。Python 3.10+。

## 重要说明

- 数据源为销售平台展示数据，最终投资决策前需用托管估值/排排网高频净值复核（见 docs/cta_evaluation_review.md）
- 季度口径的最大回撤/卡玛低估真实值约 2~4 倍
- Cookie 等凭证通过命令行参数传入，**严禁写入代码或提交**
