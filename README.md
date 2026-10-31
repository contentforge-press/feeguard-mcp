<!-- mcp-name: io.github.contentforge-press/feeguard-mcp -->

# FeeGuard

**An MCP server that reads your marketplace settlement report and tells you which SKUs had their fee rate changed — with the dollar amount.**

No LLM is involved in any number. Every figure in the output is division and multiplication, and every one of them can be re-derived by hand from the day-by-day table the server also emits.

---

## The problem it exists for

A seller's referral fee sits at 8% for months. Then one settlement cycle it's 15%. Nobody announces it. The seller notices a smaller payout, assumes sales were soft, and moves on.

By the time someone opens a case with the marketplace, the overcharge has compounded for weeks and the evidence is buried in a CSV nobody kept.

This server reads that CSV and returns:

```json
{
  "sku": "B0F2K9L1MX",
  "rule": "费率跳变",
  "base_rate": 0.0816,
  "current_rate": 0.15,
  "onset_date": "2026-10-15",
  "base_source": "该 SKU 跳变前 14 期的中位数",
  "leak_usd": 2061.05,
  "fact": "从 2026-10-15 起连续 7 天佣金费率 15.0%，跳变前基准 8.2%，高出 6.8 个百分点（涨 84%）；累计被多扣 7 天，平均每天 $294.44",
  "next_action": "拿 2026-10-15 之后的结算明细开 CASE，要求核对 B0F2K9L1MX 的类目归属；附上同节点其他 ASIN 的费率截图作证据。若判为类目误放，可申请重算并追回这段时间多扣的部分。"
}
```

Note what's in there: the **onset date**, the **baseline and where the baseline came from**, and the **exact next step**. The point is not "something looks wrong" — it's "here is the day to open the case, and here is what to attach."

---

## Three rules

| Rule | Fires when | Baseline | Leak formula |
|---|---|---|---|
| **Rate jump** (费率跳变) | rate up ≥ 25% **relative** to baseline **and** ≥ 3pp **absolute**, sustained over consecutive days | median of that SKU's own earlier periods | `(actual − baseline) × daily sales`, summed |
| **Cross-SKU outlier** (费率离群) | today's rate ≥ 1.6× the median rate of other SKUs that day | median of other SKUs on the same date | `(actual − median) × that day's sales` |
| **Hard ceiling** (费率触顶) | rate ≥ 45% | absolute threshold, no comparison needed | `0` — deliberately not estimated |

Priority is jump → outlier → ceiling. One SKU reports at most one finding: the most severe.

Sample minimums are enforced, not suggested: a rate jump needs **≥ 3 data points**, an outlier needs **≥ 2 comparable SKUs**. Below that the rule returns nothing rather than a weak guess.

### What it deliberately does NOT do

This list is the product:

- **Never estimates a missing field.** Missing required columns raise an error. It does not return "no anomaly found."
- **Never uses an industry average.** The baseline comes only from the customer's own rows.
- **Never estimates the leak on a ceiling finding.** Without the correct category rate there is no honest number, so it returns `0` and says so.
- **Never lets a model decide a number.** The model orchestrates; the math is Python.
- **No network calls, no uploads, no writes to your files.**

---

## Install

Requires Python 3.9+. **No third-party dependencies** — the MCP transport is standard-library JSON-RPC 2.0 over stdio. This is intentional: the shorter the dependency tree, the faster a security team can sign off on it.

```bash
git clone https://github.com/contentforge-press/feeguard-mcp.git
cd feeguard-mcp
python3 -m feeguard_mcp --csv examples/sample_settlement.csv   # try it without any client
```

## Configure in your client

Claude Desktop / Cursor / Windsurf / VS Code — same shape everywhere:

```json
{
  "mcpServers": {
    "feeguard": {
      "command": "python3",
      "args": ["-m", "feeguard_mcp"],
      "cwd": "/absolute/path/to/feeguard-mcp"
    }
  }
}
```

Gemini CLI:

```bash
gemini extensions install https://github.com/contentforge-press/feeguard-mcp
```

---

## Tools

| Tool | What it's for |
|---|---|
| `feeguard_inspect` | Check whether the columns are recognised, **before** running anything. Use this first on an unfamiliar export. |
| `feeguard_scan` | The audit. Returns findings with USD amounts and the next action for each. |
| `feeguard_verify` | Recomputes everything into a per-SKU per-day rate table, so a human can check the arithmetic independently. **This is the evidence package, not the conclusion.** |
| `feeguard_explain` | The exact definition of each rule and every default threshold, so any number in a report can be traced to an equation. |

All paths must be absolute. This server is read-only.

### Column recognition

You do not need to rename your columns. Recognised aliases include the native Amazon settlement report headers:

```
date/time        → date          (timezone is normalised; 2026-10-15T00:00:00+00:00 == 2026-10-15)
seller-sku       → sku
product-sales    → sales
selling-fees     → commission    (negative values are taken as absolute)
```

Also handled: `1,000.00` thousand separators, `$` prefixes, `M/D/YYYY` US dates, Chinese column names (日期 / SKU / 销售额 / 佣金), and rows whose field count exceeds the header.

If a required column can't be found, `feeguard_scan` raises:

```
ERROR: 这张表缺 1 列：佣金 / 平台费。判定不了，也不会假装查过。
```

---

## Thresholds

Override per call with `{"path": "...", "config": {"fee_jump_ratio": 0.30}}`.

| Key | Default | Meaning |
|---|---|---|
| `fee_jump_ratio` | `0.25` | relative increase to count as a jump |
| `fee_jump_abs` | `0.03` | absolute increase in percentage points, required **in addition** |
| `outlier_mult` | `1.6` | multiple of the same-day median |
| `hard_ceiling` | `0.45` | absolute rate ceiling |
| `min_sales` | `200.0` | SKUs below this daily sales are not judged (noise floor) |

Defaults are tuned to under-report rather than over-report. A false accusation against a marketplace costs more than a missed one.

**Known behaviour, not a bug:** when a relative jump lands exactly on 25%, floating-point arithmetic can produce `0.24999999999999994` and the rule stays silent. No epsilon compensation is applied on purpose — a case sitting exactly on the threshold should be reviewed by a human, not decided by rounding.

---

## Try it

`examples/sample_settlement.csv` has 4 SKUs × 21 days in native Amazon format. Three of them jump; one stays flat.

```
$ python3 -m feeguard_mcp --csv examples/sample_settlement.csv

rows 84 | skus 4 | 2026-10-01 → 2026-10-21
total sales $216,825.00 | total fees $21,953.62 | blended 10.12%
TOTAL LEAK: $3,985.68

  B0F2K9L1MX  费率跳变  8.16% → 15.00%   $2,061.05   onset 2026-10-15
  B0C7T2N4QP  费率跳变  8.99% → 16.50%   $1,066.40   onset 2026-10-17
  B0E9K1P6XZ  费率跳变  7.97% → 25.00%     $858.24   onset 2026-10-18
```

The fourth SKU (`B0D8M5R3VW`, flat 8%) is **not** reported — that's the control.

## Tests

```bash
python3 tests/test_rules.py    # 19 edge cases: thresholds, sample minimums, missing columns, timezones
python3 tests/test_mcp.py      # 26 checks: real subprocess, real handshake, every tool
```

Both run on stdlib only.

---

## Enterprise deployment

The open-source server reads a CSV from local disk over stdio. That's the right shape for one person with one file. It is not the right shape for a team where settlement data lives in a warehouse and every access has to be attributable.

The licensed deployment package adds:

- **Connectors** to your data source (S3 / warehouse / marketplace API) instead of a local path
- **OAuth 2.1** resource-server behaviour, so access is per-identity
- **Audit log** (`--audit-log`): append-only JSONL, metadata only, no row contents
- **Scheduled scans** with findings delivered to Slack / email / webhook
- **White-label reports** under your own brand

One-time license, deployed in your own infrastructure or ours. Pricing and the evidence package: **open an issue** or reach the maintainer.

*Not included: custom feature development billed by the hour.*

---

## License

MIT — see [LICENSE](LICENSE).
