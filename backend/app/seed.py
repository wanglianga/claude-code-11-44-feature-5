"""演示/验收共用的种子数据：协议契约、设备安装关系、可信锚点。

时间线（2026-11-15，UTC）：
  PRB-1001（JSON 实时探头）      09:00–18:00  VH-01 / BX-01 / LOT-A
  PRB-2002（二进制离线记录仪）   10:00–12:00  VH-01 / BX-01 / LOT-A
                                 12:00–14:00  VH-02 / BX-02 / LOT-B（换车）
  PRB-3003（二进制记录仪）       08:00–       VH-03 / BX-03 / LOT-C
锚点：PRB-2002 启动批次 "1" 在计数 100@10:00、200@10:50（各 ±2s）；
      启动批次 "2"（重启后）计数 0@13:00（±2s）。标称采样间隔 30s。
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import ClockAnchor, DeviceRelation, MetaCounter, ProtocolContract

T = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

JSON_TEMP_R1 = {
    "kind": "json",
    "match": {"fw": ["2.3.0", "2.3.1"]},
    "rules": {
        "default": {
            "device_field": "dev",
            "device_map": {},
            "boot_field": "boot",
            "seq_field": "seq",
            "temp": {"field": "c", "unit": "C", "cal_mul": 1.0, "cal_add": 0.0},
            "clock": {"mode": "explicit_ts", "field": "ts", "error_s": 5.0},
        }
    },
}

# 修订 2：固件 3.0.0 改了温度单位（°F）与设备序号含义（资产号 A-xxxx），
# 契约明确给出换算规则与设备映射——不凭字段形状猜单位。
JSON_TEMP_R2 = {
    "kind": "json",
    "match": {"fw": ["2.3.0", "2.3.1", "3.0.0"]},
    "rules": {
        "2.3": JSON_TEMP_R1["rules"]["default"],
        "3.0": {
            "device_field": "asset",
            "device_map": {"A-1001": "PRB-1001"},
            "boot_field": "boot",
            "seq_field": "seq",
            "temp": {"field": "f", "unit": "F", "cal_mul": 1.0, "cal_add": 0.0},
            "clock": {"mode": "explicit_ts", "field": "ts", "error_s": 8.0},
        },
        "default": JSON_TEMP_R1["rules"]["default"],
    },
}

BIN_TEMP_R1 = {
    "kind": "binary",
    "match": {"versions": [1]},
    "binary": {
        "magic": 199, "header_len": 4, "payload_len": 10,
        "fields": {
            "device_no": {"off": 4, "fmt": "u16le"},
            "boot_batch": {"off": 6, "fmt": "u16le"},
            "seq": {"off": 8, "fmt": "u16le"},
            "temp_raw": {"off": 10, "fmt": "i16le"},
            "uptime_s": {"off": 12, "fmt": "u16le"},
        },
    },
    "rules": {
        "default": {
            "device_map": {"2002": "PRB-2002", "3003": "PRB-3003"},
            "temp": {"raw_field": "temp_raw", "unit": "C",
                     "scale": 0.0625, "offset": 0.0,
                     "cal_mul": 1.0, "cal_add": -0.2},
            "clock": {"mode": "counter", "counter_field": "seq",
                      "interval_s": 30.0, "drift_s_per_step": 0.05},
        }
    },
}


def seed(db: Session) -> dict:
    if db.query(ProtocolContract).count():
        return {"seeded": False, "note": "已有契约数据，跳过种子"}
    db.add_all([
        ProtocolContract(contract_key="json-temp", revision=1, kind="json",
                         match=JSON_TEMP_R1["match"], definition=JSON_TEMP_R1,
                         status="active", note="JSON 实时探头固件 2.3.x，摄氏小数"),
        ProtocolContract(contract_key="bin-temp", revision=1, kind="binary",
                         match=BIN_TEMP_R1["match"], definition=BIN_TEMP_R1,
                         status="active", note="二进制记录仪协议 v1，定点摄氏"),
    ])
    db.add_all([
        DeviceRelation(probe_id="PRB-1001", vehicle_id="VH-01", box_id="BX-01",
                       cargo_batch_id="LOT-A", valid_from=T("2026-11-15T09:00:00"),
                       valid_to=T("2026-11-15T18:00:00")),
        DeviceRelation(probe_id="PRB-2002", vehicle_id="VH-01", box_id="BX-01",
                       cargo_batch_id="LOT-A", valid_from=T("2026-11-15T10:00:00"),
                       valid_to=T("2026-11-15T12:00:00")),
        DeviceRelation(probe_id="PRB-2002", vehicle_id="VH-02", box_id="BX-02",
                       cargo_batch_id="LOT-B", valid_from=T("2026-11-15T12:00:00"),
                       valid_to=T("2026-11-15T14:00:00")),
        DeviceRelation(probe_id="PRB-3003", vehicle_id="VH-03", box_id="BX-03",
                       cargo_batch_id="LOT-C", valid_from=T("2026-11-15T08:00:00"),
                       valid_to=None),
    ])
    db.add_all([
        ClockAnchor(device_id="PRB-2002", boot_batch="1", counter=100,
                    server_time=T("2026-11-15T10:00:00"), error_s=2.0,
                    source="gateway_sync"),
        ClockAnchor(device_id="PRB-2002", boot_batch="1", counter=200,
                    server_time=T("2026-11-15T10:50:00"), error_s=2.0,
                    source="gateway_sync"),
        ClockAnchor(device_id="PRB-2002", boot_batch="2", counter=0,
                    server_time=T("2026-11-15T13:00:00"), error_s=2.0,
                    source="gateway_sync"),
    ])
    if not db.get(MetaCounter, "relation_revision"):
        db.add(MetaCounter(key="relation_revision", value=1))
    db.commit()
    return {"seeded": True}
