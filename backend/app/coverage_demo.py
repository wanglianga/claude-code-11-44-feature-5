"""覆盖分析演示数据：虚构的车厢布局、装载清单、隔板、探头安装、分区验证资料，
以及为覆盖场景补充的演示读数（经真实接入管线进入，幂等）。

演示时间线（2026-11-15，UTC，车辆 VH-01，车厢 6.0m × 2.0m，本地坐标：
原点=车厢最内侧左角地板，x 指向门口，y 指向右侧，单位米）：

  探头    PRB-1001@(0.5,1.0) 09:00–18:00；PRB-2002@(2.5,1.0) 10:00–12:00（之后迁往 VH-02）
  隔板    D1@x=3.0（09:00–13:00）→ D1@x=4.0（13:00 起）
  托盘    P1[0,1]LOT-A；P3[2,3]混批 LOT-A+LOT-B；P2[3,4]LOT-A；
          P5[4,5]→(11:00–11:20 不确定)→[1,2] LOT-B；P4[5,6]门侧 LOT-B；
          P9[1.5,2.5]（清单 MF-02，12:00 起，与 P5/P3 占用冲突）
  验证    VAL-001 PRB-1001 代表 [0,3]（D1@3.0，CAL-A）
          VAL-002 PRB-2002 代表 [2,3]（D1@3.0，CAL-B，与 VAL-001 交叠）
          VAL-003 PRB-1001 代表 [3,5]（D1@3.0，CAL-B）
          VAL-005 PRB-1001 代表 [0,4]（D1@4.0，CAL-A，13:00 起）
          VAL-004 PRB-1001 代表 [5,6]（D1@3.0，CAL-C，12:00–13:00）——补证，单独导入
"""
import struct
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .coverage import import_bundle, run_coverage
from .models import Sample, TransportBatch
from .pipeline import ingest_binary_chunk, ingest_json

T = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

DEMO_VEHICLE = "VH-01"
DEMO_WINDOW = (T("2026-11-15T09:00:00"), T("2026-11-15T15:00:00"))

DEMO_BUNDLE = {
    "layouts": [{
        "vehicle_id": "VH-01",
        "coord": {"origin": "车厢最内侧左角地板", "x_axis": "指向门口",
                  "y_axis": "指向右侧", "unit": "m"},
        "length_m": 6.0, "width_m": 2.0, "layers": ["L1"],
        "zones": [{"zone_id": "Z1", "name": "主货舱",
                   "rect": {"x": 0, "y": 0, "w": 6.0, "h": 2.0}, "layer": "L1"}],
        "note": "6m 冷藏厢演示布局",
    }],
    "batch_specs": [
        {"cargo_batch_id": "LOT-A", "temp_min_c": 2.0, "temp_max_c": 8.0,
         "window_minutes": 60, "min_readings": 1},
        {"cargo_batch_id": "LOT-B", "temp_min_c": 2.0, "temp_max_c": 8.0,
         "window_minutes": 60, "min_readings": 1},
        {"cargo_batch_id": "LOT-C", "temp_min_c": -25.0, "temp_max_c": -15.0,
         "window_minutes": 60, "min_readings": 1},
    ],
    "partitions": [
        {"event_key": "PE-D1-01", "vehicle_id": "VH-01", "partition_id": "D1",
         "x_position": 3.0, "effective_from": "2026-11-15T08:00:00+00:00",
         "effective_to": "2026-11-15T13:00:00+00:00"},
        {"event_key": "PE-D1-02", "vehicle_id": "VH-01", "partition_id": "D1",
         "x_position": 4.0, "effective_from": "2026-11-15T13:00:00+00:00",
         "effective_to": None},
    ],
    "probes": [
        {"install_key": "PI-1001-A", "probe_id": "PRB-1001", "vehicle_id": "VH-01",
         "x": 0.5, "y": 1.0, "layer": "L1",
         "installed_from": "2026-11-15T09:00:00+00:00",
         "installed_to": "2026-11-15T18:00:00+00:00"},
        {"install_key": "PI-2002-A", "probe_id": "PRB-2002", "vehicle_id": "VH-01",
         "x": 2.5, "y": 1.0, "layer": "L1",
         "installed_from": "2026-11-15T10:00:00+00:00",
         "installed_to": "2026-11-15T12:00:00+00:00"},
    ],
    "validations": [
        {"validation_key": "VAL-001", "vehicle_id": "VH-01", "structure": "冷藏厢-6m-A",
         "probe_id": "PRB-1001", "install_point": {"x": 0.5, "y": 1.0, "layer": "L1"},
         "install_condition": "内侧前壁固定支架，距地板 1.5m",
         "range_rect": {"x": 0.0, "y": 0.0, "w": 3.0, "h": 2.0}, "range_layer": "L1",
         "partition_config": [{"partition_id": "D1", "x": 3.0}],
         "condition_group": "CAL-A",
         "valid_from": "2026-11-15T09:00:00+00:00", "valid_to": "2026-11-15T18:00:00+00:00"},
        {"validation_key": "VAL-002", "vehicle_id": "VH-01", "structure": "冷藏厢-6m-A",
         "probe_id": "PRB-2002", "install_point": {"x": 2.5, "y": 1.0, "layer": "L1"},
         "install_condition": "隔板 D1 前侧磁吸安装",
         "range_rect": {"x": 2.0, "y": 0.0, "w": 1.0, "h": 2.0}, "range_layer": "L1",
         "partition_config": [{"partition_id": "D1", "x": 3.0}],
         "condition_group": "CAL-B",
         "valid_from": "2026-11-15T10:00:00+00:00", "valid_to": "2026-11-15T12:00:00+00:00"},
        {"validation_key": "VAL-003", "vehicle_id": "VH-01", "structure": "冷藏厢-6m-A",
         "probe_id": "PRB-1001", "install_point": {"x": 0.5, "y": 1.0, "layer": "L1"},
         "install_condition": "内侧前壁固定支架，代表隔板后段中部",
         "range_rect": {"x": 3.0, "y": 0.0, "w": 2.0, "h": 2.0}, "range_layer": "L1",
         "partition_config": [{"partition_id": "D1", "x": 3.0}],
         "condition_group": "CAL-B",
         "valid_from": "2026-11-15T09:00:00+00:00", "valid_to": "2026-11-15T18:00:00+00:00"},
        {"validation_key": "VAL-005", "vehicle_id": "VH-01", "structure": "冷藏厢-6m-A",
         "probe_id": "PRB-1001", "install_point": {"x": 0.5, "y": 1.0, "layer": "L1"},
         "install_condition": "内侧前壁固定支架（隔板移至 4.0 后验证）",
         "range_rect": {"x": 0.0, "y": 0.0, "w": 4.0, "h": 2.0}, "range_layer": "L1",
         "partition_config": [{"partition_id": "D1", "x": 4.0}],
         "condition_group": "CAL-A",
         "valid_from": "2026-11-15T13:00:00+00:00", "valid_to": "2026-11-15T18:00:00+00:00"},
    ],
    "placements": [
        {"placement_key": "PL-P1", "manifest_id": "MF-01", "vehicle_id": "VH-01",
         "pallet_id": "P1", "batches": [{"batch_id": "LOT-A", "pieces": 100}],
         "rect": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L1",
         "from_lo": "2026-11-15T09:00:00+00:00", "to_lo": "2026-11-15T15:00:00+00:00"},
        {"placement_key": "PL-P3", "manifest_id": "MF-01", "vehicle_id": "VH-01",
         "pallet_id": "P3",
         "batches": [{"batch_id": "LOT-A", "pieces": 50}, {"batch_id": "LOT-B", "pieces": 40}],
         "rect": {"x": 2.0, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L1",
         "from_lo": "2026-11-15T09:00:00+00:00", "to_lo": "2026-11-15T15:00:00+00:00"},
        {"placement_key": "PL-P2", "manifest_id": "MF-01", "vehicle_id": "VH-01",
         "pallet_id": "P2", "batches": [{"batch_id": "LOT-A", "pieces": 80}],
         "rect": {"x": 3.0, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L1",
         "from_lo": "2026-11-15T09:00:00+00:00", "to_lo": "2026-11-15T15:00:00+00:00"},
        {"placement_key": "PL-P5A", "manifest_id": "MF-01", "vehicle_id": "VH-01",
         "pallet_id": "P5", "batches": [{"batch_id": "LOT-B", "pieces": 60}],
         "rect": {"x": 4.0, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L1",
         "from_lo": "2026-11-15T09:00:00+00:00",
         "to_lo": "2026-11-15T11:00:00+00:00", "to_hi": "2026-11-15T11:20:00+00:00"},
        {"placement_key": "PL-P5B", "manifest_id": "MF-01", "vehicle_id": "VH-01",
         "pallet_id": "P5", "batches": [{"batch_id": "LOT-B", "pieces": 60}],
         "rect": {"x": 1.0, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L1",
         "from_lo": "2026-11-15T11:00:00+00:00", "from_hi": "2026-11-15T11:20:00+00:00",
         "to_lo": "2026-11-15T15:00:00+00:00"},
        {"placement_key": "PL-P4", "manifest_id": "MF-01", "vehicle_id": "VH-01",
         "pallet_id": "P4", "batches": [{"batch_id": "LOT-B", "pieces": 70}],
         "rect": {"x": 5.0, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L1",
         "from_lo": "2026-11-15T09:00:00+00:00", "to_lo": "2026-11-15T15:00:00+00:00"},
        {"placement_key": "PL-P9", "manifest_id": "MF-02", "vehicle_id": "VH-01",
         "pallet_id": "P9", "batches": [{"batch_id": "LOT-B", "pieces": 30}],
         "rect": {"x": 1.5, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L1",
         "from_lo": "2026-11-15T12:00:00+00:00", "to_lo": "2026-11-15T15:00:00+00:00"},
    ],
}

# 补证验证资料（演示"原本未确认、补证后才获得资格"）：门侧 [5,6]
VAL_004 = {
    "validation_key": "VAL-004", "vehicle_id": "VH-01", "structure": "冷藏厢-6m-A",
    "probe_id": "PRB-1001", "install_point": {"x": 0.5, "y": 1.0, "layer": "L1"},
    "install_condition": "内侧前壁固定支架，补充验证门侧",
    "range_rect": {"x": 5.0, "y": 0.0, "w": 1.0, "h": 2.0}, "range_layer": "L1",
    "partition_config": [{"partition_id": "D1", "x": 3.0}],
    "condition_group": "CAL-C",
    "valid_from": "2026-11-15T12:00:00+00:00", "valid_to": "2026-11-15T13:00:00+00:00",
}

# 演示补充读数：JSON 实时探头 PRB-1001（fw 2.3.1，boot B7）
DEMO_JSON_READINGS = [
    {"type": "temp", "fw": "2.3.1", "dev": "PRB-1001", "boot": "B7", "seq": 11,
     "evt": "EVT-C101", "ts": "2026-11-15T11:20:00+00:00", "c": 4.6},
    {"type": "temp", "fw": "2.3.1", "dev": "PRB-1001", "boot": "B7", "seq": 12,
     "evt": "EVT-C102", "ts": "2026-11-15T12:05:00+00:00", "c": 4.7},
    {"type": "temp", "fw": "2.3.1", "dev": "PRB-1001", "boot": "B7", "seq": 13,
     "evt": "EVT-C103", "ts": "2026-11-15T12:25:00+00:00", "c": 4.8},
    {"type": "temp", "fw": "2.3.1", "dev": "PRB-1001", "boot": "B7", "seq": 14,
     "evt": "EVT-C104", "ts": "2026-11-15T13:05:00+00:00", "c": 4.9},
    {"type": "temp", "fw": "2.3.1", "dev": "PRB-1001", "boot": "B7", "seq": 15,
     "evt": "EVT-C105", "ts": "2026-11-15T13:25:00+00:00", "c": 5.0},
    {"type": "temp", "fw": "2.3.1", "dev": "PRB-1001", "boot": "B7", "seq": 16,
     "evt": "EVT-C106", "ts": "2026-11-15T14:05:00+00:00", "c": 5.1},
    {"type": "temp", "fw": "2.3.1", "dev": "PRB-1001", "boot": "B7", "seq": 17,
     "evt": "EVT-C107", "ts": "2026-11-15T14:25:00+00:00", "c": 5.0},
]

# 二进制记录仪 PRB-2002（boot 1）：seq 260 @ 11:20 = 9.6125°C（超 LOT 上限 8，
# 与同域 PRB-1001 的 4.6°C 构成观测冲突）。帧布局见 docs/protocol.md。
_DEMO_BIN_FRAMES = [(2002, 1, 260, 157, 10800)]


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _build_frame(device_no: int, boot: int, seq: int, temp_raw: int, uptime_s: int) -> bytes:
    payload = struct.pack("<HHHhH", device_no, boot, seq, temp_raw, uptime_s)
    body = bytes([0xC7, 1, 0, len(payload)]) + payload
    return body + struct.pack("<H", _crc16_ccitt(body))


def ensure_demo_readings(db: Session) -> dict:
    """经真实接入管线补入演示读数；已存在则跳过（幂等，不产生重复样本）。"""
    if db.query(Sample).filter_by(device_id="PRB-1001", boot_batch="B7",
                                  sample_seq=11).first():
        return {"ingested": False, "note": "演示读数已存在，跳过"}
    batch = TransportBatch(source="coverage-demo", protocol="mixed",
                           note="覆盖分析演示补充读数（PRB-1001 11:20–14:25；PRB-2002 冲突读数）")
    db.add(batch)
    db.flush()
    for msg in DEMO_JSON_READINGS:
        ingest_json(db, batch, "json-cov", msg)
    chunk = b"".join(_build_frame(*f) for f in _DEMO_BIN_FRAMES)
    ingest_binary_chunk(db, batch, "bin-cov", chunk)
    batch.open = False
    db.commit()
    return {"ingested": True, "batch_id": batch.id,
            "json_msgs": len(DEMO_JSON_READINGS), "bin_frames": len(_DEMO_BIN_FRAMES)}


def run_demo(db: Session) -> dict:
    """一键演示：补读数 → 导入覆盖输入 → 对演示窗口执行覆盖分析。"""
    readings = ensure_demo_readings(db)
    imported = import_bundle(db, DEMO_BUNDLE)
    ws, we = DEMO_WINDOW
    run = run_coverage(db, DEMO_VEHICLE, None, ws, we)
    base = db.query(Sample).filter_by(device_id="PRB-1001", boot_batch="B7",
                                      sample_seq=1).first()
    note = "" if base else "（尚未执行 sender run all，基础读数缺失，部分窗口会显示缺读数）"
    return {"readings": readings, "import": imported, "run_id": run.id,
            "run_status": run.status, "note": note}
