# -*- coding: utf-8 -*-
"""FeeGuard 判定内核 —— 三条确定性规则，零第三方依赖。

铁律（改动前请先读）：
  1. 费率 = commission / sales，纯除法。AI 不参与任何一个数字的判定。
  2. 基准费率必须来自数据本身（历史同 SKU 中位数 → 横向同期中位数）。
     数据不够就返回 None（不判定），绝不拍脑袋给行业均值。
  3. 漏损 = (实际费率 - 基准费率) × 销售额，确定性乘法。
  4. 缺列必须报错停机。静默返回「无异常」等于对客户撒谎。

这四条是产品的全部价值所在，不是注释。
"""
import csv as _csv
import os
import re
import statistics

__all__ = [
    "DEFAULT_CFG", "REQUIRED", "norm_date", "normalize", "read_any_csv",
    "detect_columns", "load_rate_rows", "build_cells", "safe_rate", "money",
    "rule_jump", "rule_outlier", "rule_ceiling", "scan", "audit_file",
]

# 阈值：正式版按客户画像调。默认值偏保守——宁可漏报，不可误报。
DEFAULT_CFG = {
    "fee_jump_ratio": 0.25,        # 相对跳变：相对基准上涨 ≥ 25%
    "fee_jump_abs": 0.03,          # 且绝对跳变 ≥ 3 个百分点
    "outlier_mult": 1.6,           # 横向离群：≥ 同期其他 SKU 中位数的 1.6 倍
    "hard_ceiling": 0.45,          # 绝对红线：费率 ≥ 45% 必报
    "min_sales": 200.0,            # 销售额太小的 SKU 不判定（噪音）
    "exchange_rate": 7.12,
}

REQUIRED = {
    "date": "日期",
    "sku": "SKU",
    "sales": "销售额",
    "commission": "佣金 / 平台费",
}

# 客户给的表不可能是我们定好的列名，必须容错。
# 左边是内部键，右边是见过的各种写法（含亚马逊结算报告原生列名、中文导出名）。
ALIASES = {
    "date": ["date", "日期", "交易日期", "settlement start date", "posted date", "datetime",
             "date/time", "date_time", "transaction date"],
    "sku": ["sku", "seller sku", "商品编码", "sku编码", "msku", "asin", "seller-sku"],
    "sales": ["sales_amount", "sales", "product sales", "gross sales", "product sales amount",
              "product-sales", "sales_amount_usd",
              "销售额", "销售金额", "总销售额", "gross revenue", "revenue"],
    "commission": ["platform_commission", "commission", "referral fee", "referral fee amount",
                   "commission fee", "佣金", "平台佣金", "推荐费", "commission amount",
                   "selling-fees", "selling fees", "total fees", "fees"],
    "fba": ["fba_fee", "fba fee", "fulfillment fee", "fba fees", "配送费", "亚马逊配送费"],
    "orders": ["orders", "order count", "quantity", "units", "订单数", "订单量", "units sold"],
    "ad": ["ad_spend", "ad spend", "advertising cost", "spend", "广告费", "广告花费"],
    "refund": ["refund_amount", "refund", "refunds", "退款", "退款金额", "refund amount"],
    "storage": ["storage_fee", "storage fee", "仓储费", "storage"],
    "payout": ["payout_usd", "payout", "total payout", "回款", "结算金额", "net proceeds"],
    "inv_units": ["inventory_units", "inventory", "库存", "available inventory"],
    "odr": ["odr", "order defect rate"],
}


def norm_date(v):
    """把 2026-10-15T00:00:00+00:00 / 2026/10/15 / 10/15/2026 一律压成 2026-10-15。

    时区必须压掉。带时区的原始串会让同一天裂成两天，聚合就错了。
    """
    t = str(v or "").strip()
    if not t:
        return ""
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", t)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)   # 美式 M/D/YYYY
    if m:
        return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))
    return t


def normalize(lc_row):
    """把任意写法的表头映射到内部键。"""
    found = {}
    for key, names in ALIASES.items():
        for nm in names:
            if nm in lc_row:
                found[key] = lc_row[nm]
                break
    return found


def read_any_csv(path):
    """容错读取任意列名的结算表。"""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for raw in _csv.DictReader(f):
            # 字段数比表头多的行会被 DictReader 塞进 None 键，值是个 list。
            # 客户导出的脏 CSV 常见这种情况。置空，交给后面的「全是 0」校验兜底——
            # 绝不猜它是哪一列。
            low = {}
            for k, v in raw.items():
                if isinstance(v, list):
                    continue
                low[(k or "").strip().lower()] = v.strip() if v else ""
            m = normalize(low)

            def num(key, default=0.0):
                try:
                    s = str(m.get(key, "") or default).replace(",", "").replace("$", "")
                    return float(s)
                except (ValueError, AttributeError):
                    return default

            def opt(key):
                v = m.get(key, "")
                if v in ("", None):
                    return None
                try:
                    return float(str(v).replace(",", "").replace("$", ""))
                except ValueError:
                    return None

            rows.append({
                "date": norm_date(m.get("date", "")),
                "sku": (m.get("sku") or "?").strip(),
                "orders": int(num("orders")),
                "sales": abs(num("sales")),
                # 亚马逊原生报告里费用记负数，取绝对值统一口径
                "commission": abs(num("commission")),
                "fba": abs(num("fba")),
                "ad": num("ad"),
                "returns": 0,
                "refund": num("refund"),
                "storage": num("storage"),
                "payout": num("payout"),
                "inv_units": opt("inv_units"),
                "odr": opt("odr"),
            })
    return rows


def detect_columns(path):
    """返回表头里认得出来的内部键。用来判断缺列，防止静默输出「没问题」。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = _csv.reader(f)
        try:
            head = next(r)
        except StopIteration:
            return set()
    low = {(c or "").strip().lower(): "" for c in head}
    return set(normalize(low).keys())


def load_rate_rows(csv_path):
    """读不出数就报错停下。宁可不出货，不可出错货。

    只读出销售额、佣金却全是 0，说明费用列没认出来——这比读不出更危险，
    因为它会一路走完流程，最后输出一个「无异常」的假结论。
    """
    got = detect_columns(csv_path)
    missing = [k for k in REQUIRED if k not in got]
    if missing:
        raise ValueError(
            "这张表缺 {} 列：{}。判定不了，也不会假装查过。"
            "要么把列名改成常见写法，要么告诉我它们叫什么。".format(
                len(missing), "、".join(REQUIRED[m] for m in missing)))

    rows = read_any_csv(csv_path)
    if not rows:
        raise ValueError("表里一行数据都没有。")
    if not any(r["sales"] for r in rows) or not any(r["commission"] for r in rows):
        raise ValueError("销售额或佣金读出来全是 0，判定不了。检查一下是不是导错了文件。")
    return rows


def build_cells(rows):
    """聚合成 {sku: {date: {sales, commission, fba, orders}}}"""
    cells = {}
    for r in rows:
        c = cells.setdefault(r["sku"], {}).setdefault(
            r["date"], {"sales": 0.0, "commission": 0.0, "fba": 0.0, "orders": 0})
        c["sales"] += r["sales"]
        c["commission"] += r["commission"]
        c["fba"] += r["fba"]
        c["orders"] += r["orders"]
    return cells


def safe_rate(num, den):
    den = float(den or 0)
    if den <= 0:
        return None
    return num / den


def money(v, unit="$"):
    v = float(v)
    return "-{}{:,.2f}".format(unit, abs(v)) if v < 0 else "{}{:,.2f}".format(unit, v)


# ───────────────── 三条检测规则（全部确定性数学）─────────────────
def rule_jump(sku, dates, cells, cfg):
    """① 跨期跳变：找连续偏离段，累计这段时间被多扣的钱。

    单日跳变不算数——平台改了费率不会只改一天。所以从最新一天往前扫，
    只要连续每天都在高位，就一路累计到偏离开始的那天。
    """
    pts = []
    for d in dates:
        c = cells[d]
        if c["sales"] >= cfg["min_sales"]:
            rt = safe_rate(c["commission"], c["sales"])
            if rt is not None:
                pts.append((d, rt, c["sales"]))

    if len(pts) < 3:
        return None

    # 基准 = 除最后一天外的历史中位数（假设跳变前是正常的）
    base = statistics.median([r for _, r, _ in pts[:-1]])
    if base <= 0:
        return None

    # 从最新一天往前，找「连续偏离」的那一段
    seg = []
    for d, r, s in reversed(pts):
        rel = (r - base) / base
        if rel >= cfg["fee_jump_ratio"] and (r - base) >= cfg["fee_jump_abs"]:
            seg.append((d, r, s))
        else:
            break

    if not seg:
        return None

    seg.reverse()
    onset = seg[0][0]
    days = len(seg)
    cur_r = seg[-1][1]
    leak_usd = sum((r - base) * s for _, r, s in seg)
    day_usd = leak_usd / days

    span = "从 {} 起连续 {} 天".format(onset, days) if days > 1 else "{} 当天".format(onset)
    return {
        "rule": "费率跳变", "level": "red", "sku": sku,
        "fact": "{}佣金费率 {:.1%}，跳变前基准 {:.1%}，高出 {:.1f} 个百分点（涨 {:.0f}%）；"
                "累计被多扣 {} 天，平均每天 {}".format(
                    span, cur_r, base, cur_r * 100 - base * 100,
                    (cur_r - base) / base * 100, days, money(day_usd)),
        "base": base, "cur": cur_r, "onset": onset,
        "base_source": "该 SKU 跳变前 {} 期的中位数".format(len(pts) - days),
        "leak_usd": leak_usd,
        "action": "拿 {} 之后的结算明细开 CASE，要求核对 {} 的类目归属；"
                  "附上同节点其他 ASIN 的费率截图作证据。"
                  "若判为类目误放，可申请重算并追回这段时间多扣的部分。".format(onset, sku),
    }


def rule_outlier(sku, dates, cells, all_cells, cfg):
    """② 横向离群：同一天，这个 SKU 的费率远高于当天其他 SKU。"""
    cur_d = dates[-1]
    c = cells[cur_d]
    if c["sales"] < cfg["min_sales"]:
        return None
    rt = safe_rate(c["commission"], c["sales"])
    if rt is None:
        return None

    others = []
    for other_sku, dd in all_cells.items():
        if other_sku == sku or cur_d not in dd:
            continue
        oc = dd[cur_d]
        if oc["sales"] < cfg["min_sales"]:
            continue
        ort = safe_rate(oc["commission"], oc["sales"])
        if ort is not None:
            others.append(ort)

    # 少于 2 个可比 SKU 不判定。基准不足就不出结论——这是刻意的。
    if len(others) < 2:
        return None

    med = statistics.median(others)
    if med <= 0:
        return None

    if rt >= med * cfg["outlier_mult"]:
        leak_usd = (rt - med) * c["sales"]
        return {
            "rule": "费率离群", "level": "red", "sku": sku,
            "fact": "{} 佣金费率 {:.1%}，而当天其他 {} 个 SKU 的中位数是 {:.1%}，高出 {:.0%}".format(
                cur_d, rt, len(others), med, rt / med - 1),
            "base": med, "cur": rt, "onset": cur_d,
            "base_source": "同期其他 {} 个 SKU 的费率中位数".format(len(others)),
            "leak_usd": leak_usd,
            "action": "高度疑似类目放置错误：同一批货被划到了更高费率档。"
                      "开 CASE 要求核对 {} 的商品类目节点，"
                      "并把当天其他 SKU 的费率列在同一张截图里一并提交。".format(sku),
        }
    return None


def rule_ceiling(sku, dates, cells, cfg):
    """③ 绝对红线：费率超过硬性上限，多数情况下意味着类目被错划。"""
    cur_d = dates[-1]
    c = cells[cur_d]
    if c["sales"] < cfg["min_sales"]:
        return None
    rt = safe_rate(c["commission"], c["sales"])
    if rt is None:
        return None
    if rt >= cfg["hard_ceiling"]:
        return {
            "rule": "费率触顶", "level": "red", "sku": sku,
            "fact": "{} 佣金费率 {:.1%}，已越过 {:.0%} 的判定上限".format(
                cur_d, rt, cfg["hard_ceiling"]),
            "base": None, "cur": rt, "onset": cur_d,
            "base_source": "绝对阈值，不依赖任何其他 SKU",
            "leak_usd": 0.0,  # 没有可信基准，不估算漏损
            "action": "这个费率在大多数类目里都属于最高档，九成是类目被错划或品类被误判。"
                      "先自查 Listing 里有没有触发高费率类目的关键词（如被判为某类配件），"
                      "改完开 CASE 申请重分类。漏损金额需拿到正确类目费率后另行计算——未获取。",
        }
    return None


def scan(cells, cfg):
    """优先级：跳变 → 离群 → 触顶。一个 SKU 只报最严重的那条。"""
    all_hits = []
    for sku, dd in cells.items():
        dates = sorted(dd.keys())
        if not dates:
            continue
        hit = rule_jump(sku, dates, dd, cfg)
        if hit:
            all_hits.append(hit)
            continue
        hit = rule_outlier(sku, dates, dd, cells, cfg)
        if hit:
            all_hits.append(hit)
            continue
        hit = rule_ceiling(sku, dates, dd, cfg)
        if hit:
            all_hits.append(hit)

    total_usd = sum(h["leak_usd"] for h in all_hits)
    return all_hits, total_usd


def audit_file(csv_path, cfg=None):
    """顶层入口：一张表进去，一份结论出来。

    返回 dict，可直接 JSON 序列化给 MCP / CLI / HTTP 三种壳共用。
    失败时抛 ValueError —— 绝不返回「无异常」。
    """
    cfg = dict(DEFAULT_CFG if cfg is None else cfg)
    if not os.path.isfile(csv_path):
        raise ValueError("文件不存在：{}".format(csv_path))

    rows = load_rate_rows(csv_path)
    cells = build_cells(rows)
    hits, total_usd = scan(cells, cfg)

    total_sales = sum(c["sales"] for dd in cells.values() for c in dd.values())
    total_comm = sum(c["commission"] for dd in cells.values() for c in dd.values())
    dates = sorted({d for dd in cells.values() for d in dd.keys()})

    return {
        "ok": True,
        "file": os.path.abspath(csv_path),
        "rows_read": len(rows),
        "skus": len(cells),
        "date_range": [dates[0], dates[-1]] if dates else [],
        "total_sales": round(total_sales, 2),
        "total_commission": round(total_comm, 2),
        "blended_rate": round(safe_rate(total_comm, total_sales) or 0.0, 6),
        "findings": [
            {
                "sku": h["sku"],
                "rule": h["rule"],
                "level": h["level"],
                "base_rate": h["base"],
                "current_rate": round(h["cur"], 6),
                "onset_date": h["onset"],
                "base_source": h["base_source"],
                "leak_usd": round(h["leak_usd"], 2),
                "fact": h["fact"],
                "next_action": h["action"],
            }
            for h in hits
        ],
        "total_leak_usd": round(total_usd, 2),
        "config_used": cfg,
        "checked_sku_count": len(cells),
        "not_checked_reason": [],
    }
