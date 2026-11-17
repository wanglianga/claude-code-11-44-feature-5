"""可控顺序的本地发送场景。每个场景是一串有严格顺序的步骤，
覆盖：半帧、多帧、坏校验、负温、序号重启、断网补传、换车边界、契约修订、
     重复去重、载荷冲突、缺锚点、超量程。

发送器只无损转发（原始字节 / 原始 JSON），不做任何解析与换算。
"""
import json
import os
import urllib.request

from frames import build_frame, temp_c_to_raw

BASE = os.environ.get("SENDER_BASE", "http://127.0.0.1:8000")


class Client:
    def __init__(self, base: str = BASE):
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body=None, raw: bytes | None = None,
             headers: dict | None = None):
        url = self.base + path
        data = raw
        h = headers or {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---- 管理 ----
    def reset(self):
        return self._req("POST", "/api/admin/reset")

    def seed(self):
        return self._req("POST", "/api/admin/seed")

    # ---- 接入 ----
    def create_batch(self, source: str, protocol: str, note: str = "") -> int:
        return self._req("POST", "/api/transport-batches",
                         {"source": source, "protocol": protocol, "note": note})["id"]

    def close_batch(self, batch_id: int):
        return self._req("POST", f"/api/transport-batches/{batch_id}/close")

    def send_json(self, batch_id: int, conn: str, msg: dict):
        return self._req("POST", f"/api/ingest/json?batch_id={batch_id}&conn={conn}", msg)

    def send_binary(self, batch_id: int, conn: str, chunk: bytes):
        return self._req("POST", f"/api/ingest/binary?batch_id={batch_id}&conn={conn}",
                         raw=chunk, headers={"Content-Type": "application/octet-stream"})

    def close_conn(self, batch_id: int, conn: str):
        return self._req("POST", "/api/connections/close",
                         {"batch_id": batch_id, "conn": conn})

    # ---- 契约 / 发布 / 消费 ----
    def create_contract(self, key: str, kind: str, match: dict, definition: dict, note=""):
        return self._req("POST", "/api/contracts",
                         {"contract_key": key, "kind": kind, "match": match,
                          "definition": definition, "note": note})

    def activate_contract(self, cid: int):
        return self._req("POST", f"/api/contracts/{cid}/activate")

    def replay(self, cid: int):
        return self._req("POST", f"/api/contracts/{cid}/replay")

    def build_publication(self, purpose: str):
        return self._req("POST", f"/api/publications/build?purpose={purpose}")

    def create_consumer(self, name: str, purpose: str):
        return self._req("POST", "/api/consumers", {"name": name, "purpose": purpose})

    def pull(self, cid: int, size: int | None = None):
        q = f"?size={size}" if size else ""
        return self._req("POST", f"/api/consumers/{cid}/pull{q}")

    def ack(self, cid: int, version: int, frm: int, to: int):
        return self._req("POST", f"/api/consumers/{cid}/ack",
                         {"version": version, "from_seq": frm, "to_seq": to})

    def get(self, path: str):
        return self._req("GET", path)

    # ---- 分区覆盖 ----
    def coverage_demo(self):
        return self._req("POST", "/api/coverage/demo")

    def coverage_import(self, bundle: dict):
        return self._req("POST", "/api/coverage/import", bundle)

    def coverage_run(self, vehicle_id: str, window_start: str, window_end: str,
                     cargo_batch_id: str | None = None):
        return self._req("POST", "/api/coverage/runs",
                         {"vehicle_id": vehicle_id, "cargo_batch_id": cargo_batch_id,
                          "window_start": window_start, "window_end": window_end})

    def coverage_withdraw_group(self, group: str, withdrawn_from: str, reason: str = ""):
        return self._req("POST", "/api/coverage/validations/withdraw-group",
                         {"condition_group": group, "withdrawn_from": withdrawn_from,
                          "reason": reason})


# ---------------------------------------------------------------- 场景

def scenario_binary_main(c: Client) -> dict:
    """换车后补传的离线记录仪：半帧拼合、多帧同块、坏校验、负温、
    换车边界待归属、超量程、截断。"""
    b = c.create_batch("sender-local-01", "binary",
                       "PRB-2002 换车后补传：半帧+多帧+坏校验+负温+边界+截断")
    conn = "bin-c1"
    f1 = build_frame(2002, 1, 150, temp_c_to_raw(4.8), uptime_s=4500)   # 10:25 VH-01
    f2 = build_frame(2002, 1, 339, temp_c_to_raw(4.8), uptime_s=10170)  # 11:59:30 VH-01(补传)
    f3 = build_frame(2002, 1, 340, temp_c_to_raw(-3.7), uptime_s=10200)  # 12:00:00 边界→待归属
    f4 = build_frame(2002, 1, 344, temp_c_to_raw(5.8), corrupt_crc=True)  # 坏校验
    f5 = build_frame(2002, 1, 344, temp_c_to_raw(5.8), uptime_s=10320)  # 12:02 VH-02
    f6 = build_frame(2002, 1, 200, 1700, uptime_s=3000)                 # 10:50 超量程 106.05°C
    f7 = build_frame(2002, 1, 100, temp_c_to_raw(4.8), uptime_s=0)      # 只发半帧后断连

    c.send_binary(b, conn, f1[:7])                    # 半帧（前 7 字节）
    c.send_binary(b, conn, f1[7:] + f2 + f3)          # 拼合 + 一次多帧
    c.send_binary(b, conn, b"\x00\x11" + f4 + f5)     # 噪声 + 坏帧 + 完整帧（不能被吞）
    c.send_binary(b, conn, f6)
    c.send_binary(b, conn, f7[:9])                    # 半帧后断网
    c.close_conn(b, conn)                             # 关闭 → 残片记截断
    c.close_batch(b)
    return {"batch": b}


def scenario_binary_restart(c: Client) -> dict:
    """设备重启：序号从头开始（boot 2），不算重复；另发缺锚点设备样本。"""
    b = c.create_batch("sender-local-01", "binary", "PRB-2002 重启后实时上报 + PRB-3003 缺锚点")
    frames = b"".join(
        build_frame(2002, 2, seq, temp_c_to_raw(4.8), uptime_s=seq * 30)
        for seq in range(5))
    c.send_binary(b, "bin-c2", frames)                # 重启后 seq 0..4，一批多帧
    orphan = build_frame(3003, 7, 10, temp_c_to_raw(3.0)) + \
             build_frame(3003, 7, 11, temp_c_to_raw(3.1))
    c.send_binary(b, "bin-c3", orphan)                # 同一启动批次无锚点 → 待归属(缺锚)
    c.close_batch(b)
    return {"batch": b}


def scenario_json_basic(c: Client) -> dict:
    """JSON 实时探头：明确时区、摄氏小数、外部事件号；含重发去重与载荷冲突。"""
    b = c.create_batch("sender-local-01", "json", "PRB-1001 实时上报 + 重发/冲突 + 新固件")
    conn = "json-c1"
    m1 = {"type": "temp", "fw": "2.3.1", "dev": "PRB-1001", "boot": "B7", "seq": 1,
          "evt": "EVT-9001", "ts": "2026-11-15T10:05:00+00:00", "c": 4.5}
    m2 = {"type": "temp", "fw": "2.3.1", "dev": "PRB-1001", "boot": "B7", "seq": 2,
          "evt": "EVT-9002", "ts": "2026-11-15T18:10:00+08:00", "c": 4.6}  # 明确 +08:00
    m3 = {"type": "temp", "fw": "2.3.1", "dev": "PRB-1001", "boot": "B7", "seq": 3,
          "evt": "EVT-9003", "ts": "2026-11-15T10:15:00+00:00", "c": 5.25}
    statuses = [c.send_json(b, conn, m)["status"] for m in (m1, m2, m3)]
    statuses.append(c.send_json(b, conn, m1)["status"])            # 原样重发 → 去重
    conflict = dict(m1, c=9.9, evt="EVT-9001R")                    # 同标识不同载荷 → 冲突
    statuses.append(c.send_json(b, conn, conflict)["status"])
    c.close_batch(b)
    return {"batch": b, "statuses": statuses}


def scenario_json_new_firmware(c: Client) -> dict:
    """固件 3.0.0（华氏度 + 新设备序号）：无契约 → 保留待解析。"""
    b = c.create_batch("sender-local-01", "json", "PRB-1001 升级固件 3.0.0 后上报")
    conn = "json-c1"
    m6 = {"type": "temp", "fw": "3.0.0", "asset": "A-1001", "boot": "B7", "seq": 4,
          "evt": "EVT-9104", "ts": "2026-11-15T10:20:00+00:00", "f": 41.0}   # 41°F = 5°C
    m7 = {"type": "temp", "fw": "3.0.0", "asset": "A-1001", "boot": "B7", "seq": 5,
          "evt": "EVT-9105", "ts": "2026-11-15T10:25:00+00:00", "f": 30.2}   # 30.2°F = -1°C
    s1 = c.send_json(b, conn, m6)["status"]
    s2 = c.send_json(b, conn, m7)["status"]
    c.close_batch(b)
    return {"batch": b, "statuses": [s1, s2]}


# 契约修订 2：补充固件 3.0.0 的明确规则（单位 °F、资产号映射）
JSON_TEMP_R2 = {
    "kind": "json",
    "match": {"fw": ["2.3.0", "2.3.1", "3.0.0"]},
    "rules": {
        "2.3": {
            "device_field": "dev", "device_map": {},
            "boot_field": "boot", "seq_field": "seq",
            "temp": {"field": "c", "unit": "C", "cal_mul": 1.0, "cal_add": 0.0},
            "clock": {"mode": "explicit_ts", "field": "ts", "error_s": 5.0},
        },
        "3.0": {
            "device_field": "asset",
            "device_map": {"A-1001": "PRB-1001"},
            "boot_field": "boot", "seq_field": "seq",
            "temp": {"field": "f", "unit": "F", "cal_mul": 1.0, "cal_add": 0.0},
            "clock": {"mode": "explicit_ts", "field": "ts", "error_s": 8.0},
        },
        "default": {
            "device_field": "dev", "device_map": {},
            "boot_field": "boot", "seq_field": "seq",
            "temp": {"field": "c", "unit": "C", "cal_mul": 1.0, "cal_add": 0.0},
            "clock": {"mode": "explicit_ts", "field": "ts", "error_s": 5.0},
        },
    },
}


def scenario_contract_revision(c: Client) -> dict:
    """发布契约修订 2（覆盖固件 3.0.0），重放依赖该契约的原始信封。"""
    created = c.create_contract("json-temp", "json", JSON_TEMP_R2["match"],
                                JSON_TEMP_R2, note="补充固件 3.0.0：华氏度 + 资产号映射")
    c.activate_contract(created["id"])
    run = c.replay(created["id"])
    return {"contract_id": created["id"], "run": run}


SCENARIOS = {
    "binary-main": scenario_binary_main,
    "binary-restart": scenario_binary_restart,
    "json-basic": scenario_json_basic,
    "json-new-firmware": scenario_json_new_firmware,
    "contract-revision": scenario_contract_revision,
}


def run_all(c: Client) -> dict:
    out = {}
    out["binary-main"] = scenario_binary_main(c)
    out["binary-restart"] = scenario_binary_restart(c)
    out["json-basic"] = scenario_json_basic(c)
    out["json-new-firmware"] = scenario_json_new_firmware(c)
    out["contract-revision"] = scenario_contract_revision(c)
    return out
