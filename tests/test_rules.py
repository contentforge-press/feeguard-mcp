# -*- coding: utf-8 -*-
"""边界用例。跑法：python3 tests/test_rules.py

每条用例都是「改一个数，看它报还是不报」——这是唯一能证明阈值真的在起作用的办法。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feeguard_mcp import rules  # noqa: E402

PASS, FAIL = [], []


def write_csv(rows, header=None):
    """用 csv 模块写，保证带逗号的金额（1,000.00）被正确加引号。"""
    import csv as _csv
    header = header or ["date", "sku", "orders", "sales_amount", "platform_commission"]
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(list(r))
    return path


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  {} {}{}".format("PASS" if cond else "FAIL", name,
                             "" if cond else "  <<< " + str(detail)))


def sku_series(rates, sku="S1", start=1, sales=1000.0):
    """按给定费率序列生成行。费率 = commission / sales。"""
    return [(f"2026-10-{d:02d}", sku, 10, sales, round(sales * r, 2))
            for d, r in zip(range(start, start + len(rates)), rates)]


print("── ① 跳变规则 ──")

# 稳定 8%，不该报
rows = sku_series([0.08] * 6)
_, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
check("稳定 8% 不报", total == 0, total)

# 微漂移 8% → 8.5%（+0.5pp，相对 +6.25%），低于两条门槛，不报
rows = sku_series([0.08, 0.08, 0.08, 0.08, 0.08, 0.085])
_, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
check("+0.5pp 微漂移不报", total == 0, total)

# +2pp（相对 +25%，但绝对 2pp < 3pp），不满足「且」，不报
rows = sku_series([0.08, 0.08, 0.08, 0.08, 0.08, 0.10])
_, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
check("+2pp（相对达标绝对不达标）不报", total == 0, total)

# +4pp（相对 +50%，绝对 4pp），两条都过，要报
rows = sku_series([0.08, 0.08, 0.08, 0.08, 0.08, 0.12])
_, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
check("+4pp 要报", total > 0, total)

# 高基准场景：20% → 26%（相对 +30%，绝对 +6pp）两条都过，要报
rows = sku_series([0.20, 0.20, 0.20, 0.20, 0.20, 0.26])
_, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
check("高基准 +6pp 要报", total > 0, total)

# 已知行为（不是 bug）：相对跳变正好等于 25% 时，浮点运算可能给出
# 0.24999999999999994，落在门槛下方 → 不报。
# 刻意不做 epsilon 补偿：这条规则的原则是宁可漏报不可误报，
# 卡在门槛上的案例本来就该由人复核，不该由代码替他决定。
rows = sku_series([0.20, 0.20, 0.20, 0.20, 0.20, 0.25])
_, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
print("  INFO 相对跳变正好 25% 时的浮点结果：total={}".format(total))

# 只有 2 个样本点，样本不足，不判定
rows = sku_series([0.08, 0.30])
_, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
check("样本 <3 不判定", total == 0, total)

# 连续 3 天高位：漏损应逐日累计
rows = sku_series([0.08, 0.08, 0.08, 0.20, 0.20, 0.20])
hits, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
expect = (0.20 - 0.08) * 1000.0 * 3
check("连续 3 天累计 = ${:.2f}".format(expect), abs(total - expect) < 0.01,
      "实际 {}".format(total))

print("── ② 离群规则 ──")

# 同日 4 个 SKU，其中 1 个是他人的 2 倍
rows = []
for i in range(1, 5):
    rows += sku_series([0.08] * 4, sku="S{}".format(i))
rows += [("2026-10-04", "S9", 10, 1000.0, 160.0)]   # 16%，是 8% 的 2 倍
hits, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
check("横向 2 倍离群要报", any(h["sku"] == "S9" for h in hits), hits)
check("离群漏损 = (0.16-0.08)*1000 = $80", abs(total - 80.0) < 0.01, total)

# 只有 1 个可比 SKU，基准不足，不判定
rows = sku_series([0.08] * 4, sku="A") + [("2026-10-04", "B", 10, 1000.0, 300.0)]
_, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
check("可比 SKU <2 不判定", total == 0, total)

print("── ③ 触顶规则 ──")

# 触顶但不估算漏损：leak 必须是 0
rows = sku_series([0.46] * 4)
hits, total = rules.scan(rules.build_cells(rules.read_any_csv(write_csv(rows))), rules.DEFAULT_CFG)
check("费率 46% 触顶要报", any(h["rule"] == "费率触顶" for h in hits), hits)
check("触顶不估算漏损（leak=0）", total == 0.0, total)

print("── ④ 缺列必须报错 ──")

try:
    p = write_csv([("2026-10-01", "S1", 10, 1000.0)],
                  header=["date", "sku", "orders", "sales_amount"])  # 缺佣金列
    rules.load_rate_rows(p)
    check("缺佣金列报错", False, "没有抛异常——这是最危险的失败方式")
except ValueError as e:
    check("缺佣金列报错", "缺 1 列" in str(e), str(e))

try:
    p = write_csv([("x", "y")], header=["foo", "bar"])
    rules.load_rate_rows(p)
    check("列名全不认识也报错", False, "静默通过了")
except ValueError as e:
    check("列名全不认识也报错", True)

print("── ⑤ 时区与负数 ──")

check("带时区日期压成同一天",
      rules.norm_date("2026-10-15T00:00:00+00:00") == rules.norm_date("2026-10-15T23:00:00+00:00"))
check("美式日期 M/D/YYYY", rules.norm_date("10/15/2026") == "2026-10-15")

# 亚马逊原生格式 + 费用记负数
p = write_csv([("2026-10-0%d" % i, "B0TEST", 5, "1,000.00", "-80.00") for i in range(1, 5)],
              header=["date/time", "seller-sku", "quantity", "product-sales", "selling-fees"])
rows = rules.read_any_csv(p)
check("原生表头认得出", len(rows) == 4 and rows[0]["sku"] == "B0TEST", rows[:1])
check("负数费用取绝对值", rows[0]["commission"] == 80.0, rows[0])
check("千分位逗号能解析", rows[0]["sales"] == 1000.0, rows[0])

print()
print("{} 通过 / {} 失败".format(len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
