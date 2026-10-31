#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FeeGuard MCP server —— JSON-RPC 2.0 over stdio，零第三方依赖。

为什么不用官方 SDK：
  这个 server 的买家要过安全审查。依赖树越短，审查越快。
  只用标准库，意味着审计人员可以在一小时内读完每一行——这是卖点，不是偷懒。

设计约束：
  1. stdout 只输出 JSON-RPC 消息，一行一条。任何日志、警告、报错全部走 stderr。
     违反这条会让客户端解析失败，且极难排查。
  2. 只读。本 server 不写客户的任何文件，除了可选的审计日志（默认关闭）。
  3. 不出结论时报错，不返回「无异常」。
"""
import csv
import json
import os
import sys
import time
import traceback

from . import rules

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "feeguard"
SERVER_VERSION = "0.1.0"

# 审计日志：默认关闭。开启后只追加、不覆盖，且只记录元数据不记录数据内容。
_AUDIT_PATH = None


def _log(msg):
    """所有非协议输出必须走 stderr。"""
    sys.stderr.write("[feeguard] {}\n".format(msg))
    sys.stderr.flush()


def _audit(tool, path, extra):
    if not _AUDIT_PATH:
        return
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool,
            "path": os.path.abspath(path) if path else None,
            "pid": os.getpid(),
        }
        rec.update(extra)
        with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        _log("审计日志写入失败：{}".format(e))


# ─────────────────────────── 工具定义 ───────────────────────────
TOOLS = [
    {
        "name": "feeguard_scan",
        "description": (
            "Audit an e-commerce settlement CSV for abnormal platform fees. "
            "Applies three deterministic rules (rate jump, cross-SKU outlier, hard ceiling) "
            "and returns per-SKU findings with the USD amount overcharged. "
            "Raises an error if required columns are missing — it never silently "
            "reports 'no anomaly'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the settlement CSV file.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional threshold overrides. See feeguard_explain for keys.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "feeguard_inspect",
        "description": (
            "Check whether a CSV's columns can be recognised, without running the audit. "
            "Use this first when a client's export has unusual headers. "
            "Returns recognised keys, missing keys, and what must be renamed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the CSV file."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "feeguard_explain",
        "description": (
            "Return the exact definition of the three detection rules and every default "
            "threshold, so the numbers in a report can be traced back to an equation. "
            "Also states what this tool deliberately does NOT do."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "feeguard_verify",
        "description": (
            "Recompute the audit from raw rows and emit a per-SKU per-day rate table, "
            "so a human can check the arithmetic independently of the findings. "
            "This is the evidence package, not the conclusion."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the CSV file."},
            },
            "required": ["path"],
        },
    },
]


def _text(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]}


def _err(msg):
    return {"content": [{"type": "text", "text": "ERROR: " + msg}], "isError": True}


def _resolve(arguments):
    """取路径。相对路径一律拒绝——客户端 cwd 不可控，猜出来的一定是错的。"""
    path = arguments.get("path")
    if not path:
        raise ValueError("缺少参数 path")
    if not os.path.isabs(path):
        raise ValueError("path 必须是绝对路径，收到的是：{}".format(path))
    return path


def call_tool(name, arguments):
    t0 = time.time()
    try:
        if name == "feeguard_scan":
            path = _resolve(arguments)
            cfg = dict(rules.DEFAULT_CFG)
            cfg.update(arguments.get("config") or {})
            out = rules.audit_file(path, cfg)
            _audit("feeguard_scan", path, {
                "rows": out["rows_read"], "skus": out["skus"],
                "findings": len(out["findings"]),
                "total_leak_usd": out["total_leak_usd"],
                "ms": int((time.time() - t0) * 1000),
            })
            return _text(out)

        if name == "feeguard_inspect":
            path = _resolve(arguments)
            if not os.path.isfile(path):
                raise ValueError("文件不存在：{}".format(path))
            got = rules.detect_columns(path)
            missing = [k for k in rules.REQUIRED if k not in got]
            with open(path, encoding="utf-8-sig", newline="") as f:
                head = next(csv.reader(f), [])
            out = {
                "file": os.path.abspath(path),
                "headers_found": head,
                "recognised": sorted(got),
                "required": rules.REQUIRED,
                "missing": missing,
                "ready": not missing,
                "hint": ("可以跑 feeguard_scan" if not missing else
                         "缺列。改列名或在 config 里告诉我它们叫什么——"
                         "不改名就跑会报错，不会输出假结论。"),
            }
            _audit("feeguard_inspect", path, {"ready": out["ready"]})
            return _text(out)

        if name == "feeguard_explain":
            out = {
                "rules": [
                    {
                        "id": "rule_jump",
                        "name": "费率跳变",
                        "condition": "相对基准上涨 ≥ {:.0%} 且 绝对上涨 ≥ {:.0f} 个百分点，"
                                     "并且连续多日处于高位".format(
                                         rules.DEFAULT_CFG["fee_jump_ratio"],
                                         rules.DEFAULT_CFG["fee_jump_abs"] * 100),
                        "baseline": "该 SKU 跳变前各期的费率中位数",
                        "leak": "(实际费率 − 基准费率) × 当日销售额，逐日累加",
                        "min_samples": 3,
                    },
                    {
                        "id": "rule_outlier",
                        "name": "费率离群",
                        "condition": "当日费率 ≥ 同日其他 SKU 费率中位数的 {:.1f} 倍".format(
                            rules.DEFAULT_CFG["outlier_mult"]),
                        "baseline": "同日其他 SKU 的费率中位数",
                        "leak": "(实际费率 − 中位数) × 当日销售额",
                        "min_samples": "可比 SKU ≥ 2，不足则不判定",
                    },
                    {
                        "id": "rule_ceiling",
                        "name": "费率触顶",
                        "condition": "费率 ≥ {:.0%}".format(rules.DEFAULT_CFG["hard_ceiling"]),
                        "baseline": "绝对阈值，不依赖任何对照",
                        "leak": "0 —— 没有可信基准，不估算。这是刻意的。",
                        "min_samples": 1,
                    },
                ],
                "priority": ["rule_jump", "rule_outlier", "rule_ceiling"],
                "defaults": rules.DEFAULT_CFG,
                "deliberately_does_not": [
                    "不估算任何缺失字段；缺列直接报错",
                    "不使用行业均值或外部基准；基准只来自客户自己的数据",
                    "不对触顶案例估算漏损金额——需要正确类目费率才能算，那是未获取",
                    "不让模型参与任何一次数值判定；判定全部是除法与乘法",
                    "不联网、不上传、不写客户文件",
                ],
            }
            return _text(out)

        if name == "feeguard_verify":
            path = _resolve(arguments)
            rows = rules.load_rate_rows(path)
            cells = rules.build_cells(rows)
            cfg = dict(rules.DEFAULT_CFG)
            table = []
            for sku in sorted(cells):
                for d in sorted(cells[sku]):
                    c = cells[sku][d]
                    rt = rules.safe_rate(c["commission"], c["sales"])
                    table.append({
                        "sku": sku, "date": d,
                        "sales": round(c["sales"], 2),
                        "commission": round(c["commission"], 2),
                        "rate": round(rt, 6) if rt is not None else None,
                        "below_min_sales": c["sales"] < cfg["min_sales"],
                    })
            out = {
                "file": os.path.abspath(path),
                "rows_read": len(rows),
                "min_sales_threshold": cfg["min_sales"],
                "daily_rates": table,
                "note": "这张表是算给人工复核用的：任何一行的 rate 都等于 commission / sales，"
                        "可以拿计算器逐个对。findings 里的每个数字都必须能在这里找到出处。",
            }
            _audit("feeguard_verify", path, {"rows": len(table)})
            return _text(out)

        raise ValueError("未知工具：{}".format(name))
    except ValueError as e:
        # 业务错误（缺列、文件不存在）：说清楚，不粉饰
        _audit(name or "?", (arguments or {}).get("path"), {"error": str(e)})
        return _err(str(e))
    except Exception as e:
        _log(traceback.format_exc())
        return _err("{}: {}".format(type(e).__name__, e))


# ───────────────────── JSON-RPC 传输层 ─────────────────────
def _read_message(stdin):
    """支持两种帧格式：Content-Length（LSP 式）与纯换行分隔 JSON。"""
    line = stdin.readline()
    if not line:
        return None
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip()
    if not line:
        return ""
    if line.lower().startswith("content-length:"):
        try:
            n = int(line.split(":", 1)[1].strip())
        except (IndexError, ValueError):
            return ""
        stdin.readline()                      # 头部与正文之间的空行
        body = stdin.read(n)
        return body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    return line


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve():
    stdin = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
    _log("FeeGuard MCP server 已启动 pid={} 协议版本={}".format(os.getpid(), PROTOCOL_VERSION))
    while True:
        try:
            raw = _read_message(stdin)
        except KeyboardInterrupt:
            break
        if raw is None:
            break
        if not str(raw).strip():
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
            continue

        method = req.get("method")
        rid = req.get("id")
        params = req.get("params") or {}

        # 通知类（无 id）一律不回包，回了会破坏握手
        if rid is None:
            if method == "notifications/initialized":
                _log("客户端就绪")
            continue

        if method == "initialize":
            _send({
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "FeeGuard audits e-commerce settlement CSVs for abnormal platform fees. "
                        "Call feeguard_inspect first if the client's column names are unusual; "
                        "then feeguard_scan for the conclusion; feeguard_verify for the "
                        "per-day arithmetic a human can re-check. Missing required columns "
                        "raise an error rather than returning 'no anomaly'."
                    ),
                },
            })
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name")
            res = call_tool(name, params.get("arguments") or {})
            _send({"jsonrpc": "2.0", "id": rid, "result": res})
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "resources/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"resources": []}})
        elif method == "prompts/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}})
        else:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": "Method not found: " + str(method)}})


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    global _AUDIT_PATH

    # --audit-log <path>：企业版要的东西，开源版默认不开
    if "--audit-log" in argv:
        i = argv.index("--audit-log")
        if i + 1 < len(argv):
            _AUDIT_PATH = argv[i + 1]
            del argv[i:i + 2]

    # --csv <path>：不走 MCP 时也可以当普通 CLI 用
    if "--csv" in argv:
        i = argv.index("--csv")
        path = argv[i + 1] if i + 1 < len(argv) else ""
        try:
            print(json.dumps(rules.audit_file(path), ensure_ascii=False, indent=2))
            return 0
        except ValueError as e:
            sys.stderr.write("ERROR: {}\n".format(e))
            return 2

    if "--version" in argv:
        print("{} {}".format(SERVER_NAME, SERVER_VERSION))
        return 0

    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
