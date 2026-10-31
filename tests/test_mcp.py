# -*- coding: utf-8 -*-
"""MCP 协议端到端测试：真起进程、走 stdio 握手、逐个调工具。

跑法：python3 tests/test_mcp.py

这个测试存在的理由：目录站收录的是「能跑的 server」，
而「能跑」这件事只有真的握一次手才算数——单测过不代表客户端连得上。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(ROOT, "examples", "sample_settlement.csv")
PASS, FAIL = [], []


def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  {} {}{}".format("PASS" if cond else "FAIL", name,
                             "" if cond else "  <<< " + str(extra)))


def main():
    env = dict(os.environ, PYTHONPATH=ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "feeguard_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=ROOT, env=env, text=True, bufsize=1)

    def send(o):
        proc.stdin.write(json.dumps(o, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        if not line.strip():
            return None
        return json.loads(line)

    print("── 握手 ──")
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "probe", "version": "1"}}})
    res = recv().get("result", {})
    chk("serverInfo.name = feeguard", res.get("serverInfo", {}).get("name") == "feeguard")
    chk("protocolVersion 一致", res.get("protocolVersion") == "2025-06-18")
    chk("声明 tools 能力", "tools" in res.get("capabilities", {}))

    # 通知没有 id，不该产生回包
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    print("── tools/list ──")
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = recv().get("result", {}).get("tools", [])
    chk("4 个工具", len(tools) == 4, [t["name"] for t in tools])
    chk("工具名齐全",
        sorted(t["name"] for t in tools) ==
        ["feeguard_explain", "feeguard_inspect", "feeguard_scan", "feeguard_verify"])
    chk("均有 inputSchema", all("inputSchema" in t for t in tools))
    chk("均有 description", all(t.get("description") for t in tools))

    print("── feeguard_explain ──")
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
          "params": {"name": "feeguard_explain", "arguments": {}}})
    r = recv()
    d = json.loads(r["result"]["content"][0]["text"])
    chk("3 条规则", len(d["rules"]) == 3)
    chk("未置 isError", not r["result"].get("isError"))
    chk("写明了不做的事", len(d["deliberately_does_not"]) >= 3)

    print("── feeguard_inspect ──")
    send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
          "params": {"name": "feeguard_inspect", "arguments": {"path": CSV}}})
    d = json.loads(recv()["result"]["content"][0]["text"])
    chk("认出亚马逊原生表头", d["ready"] is True, d.get("missing"))
    chk("关键列全在", {"sku", "sales", "commission", "date"} <= set(d["recognised"]),
        d["recognised"])

    print("── feeguard_scan ──")
    send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
          "params": {"name": "feeguard_scan", "arguments": {"path": CSV}}})
    d = json.loads(recv()["result"]["content"][0]["text"])
    chk("扫出 3 条跳变", len(d["findings"]) == 3, len(d["findings"]))
    chk("漏损合计非空", d["total_leak_usd"] > 0, d["total_leak_usd"])
    chk("每条都有 base_source", all(f["base_source"] for f in d["findings"]))
    chk("每条都有 next_action", all(f["next_action"] for f in d["findings"]))
    chk("对照 SKU 未被误报",
        all(f["sku"] != "B0D8M5R3VW" for f in d["findings"]),
        [f["sku"] for f in d["findings"]])
    chk("返回了实际使用的阈值", "config_used" in d and d["config_used"])

    print("── feeguard_verify ──")
    send({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
          "params": {"name": "feeguard_verify", "arguments": {"path": CSV}}})
    d = json.loads(recv()["result"]["content"][0]["text"])
    chk("逐日表行数正确", len(d["daily_rates"]) == 84, len(d["daily_rates"]))
    bad = [r for r in d["daily_rates"]
           if r["rate"] is not None and abs(r["rate"] - r["commission"] / r["sales"]) > 1e-6]
    chk("每一行 rate == commission/sales", not bad, bad[:2])

    print("── 错误处理 ──")
    send({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
          "params": {"name": "feeguard_scan", "arguments": {"path": "/tmp/definitely-nope.csv"}}})
    r = recv()
    chk("文件不存在 → isError", r["result"].get("isError") is True)
    chk("错误原因可读", "文件不存在" in r["result"]["content"][0]["text"])

    send({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
          "params": {"name": "feeguard_scan", "arguments": {"path": "relative.csv"}}})
    r = recv()
    chk("相对路径被拒", "绝对路径" in r["result"]["content"][0]["text"])

    send({"jsonrpc": "2.0", "id": 9, "method": "no/such/method"})
    chk("未知方法 -32601", recv().get("error", {}).get("code") == -32601)

    send({"jsonrpc": "2.0", "id": 10, "method": "ping"})
    chk("ping 回空 result", recv().get("result") == {})

    # 出错之后 server 必须还活着
    chk("多次出错后进程仍存活", proc.poll() is None, proc.poll())

    proc.stdin.close()
    proc.terminate()
    print()
    print("{} 通过 / {} 失败".format(len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
