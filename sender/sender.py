#!/usr/bin/env python3
"""本地发送器 CLI：按可控顺序发送两类协议报文。

用法：
  python sender.py reset                     # 清空服务端数据
  python sender.py seed                      # 写入种子契约/关系/锚点
  python sender.py run <场景名|all>          # 按顺序执行场景
  python sender.py publish                   # 构建两个用途的发布版本
  python sender.py consume <name> <purpose>  # 模拟消费端按游标读完整版本
  python sender.py coverage-demo             # 覆盖分析一键演示（补读数+导入+分析）
  python sender.py coverage-run              # 对演示窗口重跑覆盖分析
  python sender.py coverage-withdraw         # 演示批量撤回验证（CAL-B 组 10:30 起失效）
  python sender.py coverage-supplement       # 演示补证（导入门侧 VAL-004 并重跑）

场景：binary-main | binary-restart | json-basic | json-new-firmware | contract-revision
"""
import argparse
import json
import sys

from scenarios import Client, SCENARIOS, run_all

DEMO_WINDOW = ("2026-11-15T09:00:00+00:00", "2026-11-15T15:00:00+00:00")
VAL_004 = {
    "validation_key": "VAL-004", "vehicle_id": "VH-01", "structure": "冷藏厢-6m-A",
    "probe_id": "PRB-1001", "install_point": {"x": 0.5, "y": 1.0, "layer": "L1"},
    "install_condition": "内侧前壁固定支架，补充验证门侧",
    "range_rect": {"x": 5.0, "y": 0.0, "w": 1.0, "h": 2.0}, "range_layer": "L1",
    "partition_config": [{"partition_id": "D1", "x": 3.0}],
    "condition_group": "CAL-C",
    "valid_from": "2026-11-15T12:00:00+00:00", "valid_to": "2026-11-15T13:00:00+00:00",
}


def main():
    ap = argparse.ArgumentParser(description="冷链温控本地发送器（不依赖真实硬件）")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="服务端地址")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reset")
    sub.add_parser("seed")
    p_run = sub.add_parser("run")
    p_run.add_argument("scenario", choices=[*SCENARIOS.keys(), "all"])
    sub.add_parser("publish")
    p_cons = sub.add_parser("consume")
    p_cons.add_argument("name")
    p_cons.add_argument("purpose", choices=["line_monitor", "batch_audit"])
    p_cons.add_argument("--size", type=int, default=5)
    sub.add_parser("coverage-demo")
    sub.add_parser("coverage-run")
    sub.add_parser("coverage-withdraw")
    sub.add_parser("coverage-supplement")
    args = ap.parse_args()

    c = Client(args.base)
    if args.cmd == "reset":
        print(json.dumps(c.reset(), ensure_ascii=False))
    elif args.cmd == "seed":
        print(json.dumps(c.seed(), ensure_ascii=False))
    elif args.cmd == "run":
        out = run_all(c) if args.scenario == "all" else SCENARIOS[args.scenario](c)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.cmd == "publish":
        for purpose in ("line_monitor", "batch_audit"):
            print(json.dumps(c.build_publication(purpose), ensure_ascii=False))
    elif args.cmd == "consume":
        cons = c.create_consumer(args.name, args.purpose)
        cid = cons["id"]
        total = 0
        while True:
            page = c.pull(cid, args.size)
            items = page.get("items", [])
            if not items:
                print(f"[{args.name}] 无更多数据：{page.get('note', '')}")
                break
            print(f"[{args.name}] 版本 {page['version']} 分页 {page['from']}..{page['to']}"
                  f"{'（重发）' if page.get('redelivered') else ''}：{len(items)} 条")
            for it in items:
                print(f"  seq={it['seq']:>3} {it['device_id']:<9} {it['std_c']:>7}°C"
                      f"  t={it['sample_time']} ±{it['time_error_s']:.1f}s"
                      f"  {it.get('vehicle_id')}/{it.get('cargo_batch_id')}")
            c.ack(cid, page["version"], page["from"], page["to"])
            total += len(items)
            if not page.get("has_more"):
                break
        print(f"[{args.name}] 共接收 {total} 条，游标推进完成")
    elif args.cmd == "coverage-demo":
        out = c.coverage_demo()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"覆盖分析运行 #{out['run_id']}（{out['run_status']}），"
              f"前端「分区覆盖」页查看空间分区与覆盖比例")
    elif args.cmd == "coverage-run":
        out = c.coverage_run("VH-01", *DEMO_WINDOW)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.cmd == "coverage-withdraw":
        out = c.coverage_withdraw_group("CAL-B", "2026-11-15T10:30:00+00:00",
                                        "校准批次 CAL-B 复核不合格，批量撤回")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        print("已批量撤回 VAL-002/VAL-003；用 coverage-run 重跑并在「更正与影响」页比较")
    elif args.cmd == "coverage-supplement":
        print(json.dumps(c.coverage_import({"validations": [VAL_004]}), ensure_ascii=False))
        out = c.coverage_run("VH-01", *DEMO_WINDOW)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        print("补证 VAL-004（门侧 [5,6]，12:00–13:00）已导入并重跑，"
              "影响查询可见原本未确认的记录获得资格")


if __name__ == "__main__":
    sys.exit(main())
