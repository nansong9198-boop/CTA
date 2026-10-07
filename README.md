# CTA Private Fund Evaluation System

A quantitative screening and evaluation toolkit for CTA (managed futures) private funds, cross-validating two data sources: Danjuan Funds (Xueqiu) shelf data and Simuwang (licensed private-fund data platform) authenticated data.

## Features

- **Dual data sources**: Danjuan quarterly return series (shelf API) + Simuwang weekly/daily NAV (authenticated, AES-encrypted XHR API — reverse-engineered fetcher included)
- Computes full metrics: annualized return, max drawdown, Sharpe, Sortino, Calmar, win rate, profit/loss ratio, new-high ratio, crisis alpha (union of four equity indices), drawdown shape (time under water / losing streaks), equity exposure correlation
- Three-layer evaluation: hard gates (non-pure-CTA / excessive equity exposure / insufficient history / manager-company age & AUM / PM tenure) → weighted scoring → small-sample reliability shrinkage
- Auto-generates per-fund suitability reports (tiered: high/medium suitability, watchlist, low suitability) with per-fund reasons and a fixed disclaimer — information organization only, no investment advice
- Independent audit script: data consistency checks + score composition verification
- Cross-validation report: quarterly vs weekly frequency, quantifying drawdown understatement (median ~2x)
- Manager-company gate: companies younger than 5 years or with AUM under CNY 1bn are excluded; regulatory sanctions surfaced as risk flags

## Usage

```bash
cd ~/work/cta_eval
python3 scripts/extract_danjuan_cta.py '<danjuan cookie>'   # 1. Pull Danjuan shelf data
python3 scripts/analyze_cta.py                              # 2. Quarterly evaluation -> docs/danjuan_cta_report.md
python3 scripts/audit_cta.py                                # 3. Independent audit
python3 scripts/fetch_simuwang.py                           # 4. Pull Simuwang weekly NAV (cookie: data/simuwang_cookie.txt)
python3 scripts/fetch_simuwang_extra.py --companies         # 5. Company AUM/founding details
python3 scripts/analyze_weekly.py                           # 6. Weekly-frequency evaluation (authoritative) -> docs/simuwang_weekly_report.md
python3 scripts/compare_simuwang.py                         # 7. Cross-validation -> docs/cross_validation.md
python3 scripts/fetch_index_weekly.py                       # 8. Index/ETF daily data for equity sleeve (akshare/Sina)
python3 scripts/analyze_equity.py                           # 9. Equity-fund evaluation -> docs/equity_evaluation_report.md
```

Cookies: log in at the respective site → F12 → Network → any API request → copy the full Cookie request header. Cookie files are gitignored.

## Repository Layout

```
├── scripts/   extract_danjuan_cta.py / analyze_cta.py / audit_cta.py /
│              fetch_simuwang.py / fetch_simuwang_extra.py / analyze_weekly.py / compare_simuwang.py /
│              fetch_index_weekly.py / analyze_equity.py (equity sleeve, separate methodology)
├── docs/      cta_evaluation_plan.md (methodology) / cta_evaluation_review.md (institutional-grade review) /
│              danjuan_cta_report.md (quarterly) / simuwang_weekly_report.md (weekly, authoritative) /
│              cross_validation.md (dual-source comparison) /
│              equity_evaluation_plan.md + equity_evaluation_report.md (equity sleeve)
└── data/      raw JSON, metric CSVs, index benchmark caches, simuwang/ (per-fund weekly data),
               manager_info.json (company gate), pm_tenure.json (PM tenure)
```

## Dependencies

`requests` + `cryptography` (`pip install -r requirements.txt`), plus system `node` (evaluates Simuwang's per-request response-key snippets). Everything else is stdlib. Python 3.10+.

## Important Notes

- **The weekly-frequency Simuwang report is authoritative for final decisions.** The Danjuan quarterly series is display-grade data: it was found to be newest-first (fixed in code), and for some funds it diverges from official NAV (dividend handling / share-class differences).
- Quarterly-frequency max drawdown understates true values: measured median ~2.1x across the pool, 2.7–4.1x for low-volatility funds.
- Credentials (cookies) are read from gitignored files only. **Never hardcode or commit them.**
