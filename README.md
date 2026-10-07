# CTA Private Fund Evaluation System

A quantitative screening and evaluation toolkit for CTA (managed futures) private funds listed on Danjuan Funds (Xueqiu).

## Features

- Pulls the CTA private fund shelf list and quarterly return series from the Danjuan API
- Computes full metrics: annualized return, max drawdown, Sharpe, Sortino, Calmar, win rate, profit/loss ratio, new-high ratio, crisis alpha (union of three equity indices), drawdown shape (time under water / losing streaks), equity exposure correlation
- Three-layer evaluation: hard gates (non-pure-CTA / excessive equity exposure / insufficient history) → weighted scoring → small-sample reliability shrinkage
- Auto-generates a per-fund evaluation report (Recommended / Alternative / Watchlist / Not Recommended, with reasons)
- Independent audit script: data consistency checks + score composition verification

## Usage

```bash
cd ~/work/cta_eval
python3 scripts/extract_danjuan_cta.py '<danjuan cookie>'   # 1. Pull shelf data
python3 scripts/analyze_cta.py                              # 2. Evaluate & generate docs/danjuan_cta_report.md
python3 scripts/audit_cta.py                                # 3. Independent audit
```

Cookie: log in at danjuanfunds.com → F12 → Network → any `djapi` request → copy the full Cookie request header.

## Repository Layout

```
├── scripts/   extract_danjuan_cta.py / analyze_cta.py / audit_cta.py
├── docs/      cta_evaluation_plan.md (methodology) / cta_evaluation_review.md (institutional-grade review) / danjuan_cta_report.md (auto-generated report)
└── data/      raw JSON, metric CSVs, index benchmark caches
```

## Dependencies

Only `requests` (`pip install requests`); everything else is stdlib. Python 3.10+.

## Important Notes

- The data source is a sales-platform display API, not official custodian valuations. Verify top candidates with weekly NAV from official sources before investing (see docs/cta_evaluation_review.md).
- Quarterly-frequency max drawdown / Calmar understate true values by roughly 2–4x.
- Credentials (cookies) are passed via command-line arguments only. **Never hardcode or commit them.**
