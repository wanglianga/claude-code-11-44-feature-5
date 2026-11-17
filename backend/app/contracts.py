"""协议契约：匹配、字段解析、单位换算。全部为纯函数，重放校验时独立复算。

契约 definition 结构（示例见 docs/protocol.md 与 seed）：
  kind: json | binary
  match: {"fw": ["2.3.1", ...]}          # JSON 按固件号精确匹配
         {"versions": [1, ...]}          # 二进制按帧内版本字节匹配
  binary: {fields: {name: {off, fmt}}, payload_len}   # 仅二进制
  rules: {"<major.minor>": 规则, "default": 规则}     # JSON 按固件主次版本选规则
  规则 = {
    device_field / device_map, boot_field, seq_field,
    temp: {field|raw_field, unit, scale, offset, cal_mul, cal_add},
    clock: {mode: explicit_ts, field, error_s}
           {mode: counter, counter_field, interval_s, drift_s_per_step}
  }
"""
import struct
from datetime import datetime, timezone

from .config import settings

FMT = {
    "u16le": ("<H", False), "i16le": ("<h", True),
    "u32le": ("<I", False), "i32le": ("<i", True),
    "u8": ("<B", False), "i8": ("<b", True),
}


class ContractError(Exception):
    pass


def select_rule(definition: dict, fw: str | None) -> dict:
    rules = definition.get("rules") or {}
    if fw:
        key = ".".join(str(fw).split(".")[:2])
        if key in rules:
            return rules[key]
    if "default" in rules:
        return rules["default"]
    raise ContractError(f"契约中没有适用于固件 {fw!r} 的规则")


def contract_matches(contract, kind: str, fw: str | None, version: int | None) -> bool:
    if contract.kind != kind:
        return False
    m = contract.match or {}
    if kind == "json":
        return fw is not None and fw in (m.get("fw") or [])
    return version is not None and version in (m.get("versions") or [])


# ---------------------------------------------------------------- 字段解析

def parse_binary_fields(definition: dict, payload: bytes) -> dict:
    spec = definition["binary"]
    fields = {}
    for name, f in spec["fields"].items():
        fmt, _ = FMT[f["fmt"]]
        size = struct.calcsize(fmt)
        off = f["off"]
        if off + size > len(payload):
            raise ContractError(f"字段 {name} 超出帧长")
        fields[name] = struct.unpack_from(fmt, payload, off)[0]
    return fields


def normalize_json(definition: dict, msg: dict) -> dict:
    """按契约规则把 JSON 报文规范成统一字段；单位与设备号含义完全由契约决定。"""
    fw = msg.get("fw")
    rule = select_rule(definition, fw)
    dev_raw = msg.get(rule["device_field"])
    if dev_raw is None:
        raise ContractError(f"缺少设备字段 {rule['device_field']}")
    device_map = rule.get("device_map") or {}
    device = device_map.get(str(dev_raw), str(dev_raw))
    temp_spec = rule["temp"]
    if temp_spec["field"] not in msg:
        raise ContractError(f"缺少温度字段 {temp_spec['field']}")
    return {
        "device_id": device,
        "boot_batch": str(msg[rule["boot_field"]]),
        "sample_seq": int(msg[rule["seq_field"]]),
        "raw_value": float(msg[temp_spec["field"]]),
        "raw_unit": temp_spec["unit"],
        "fw": fw,
        "rule_key": ".".join(str(fw).split(".")[:2]) if fw else "default",
        "raw_fields": msg,
    }


def normalize_binary(definition: dict, payload: bytes) -> dict:
    fields = parse_binary_fields(definition, payload)
    rule = select_rule(definition, None)
    device_map = rule.get("device_map") or {}
    dev_no = str(fields["device_no"])
    if dev_no not in device_map:
        raise ContractError(f"设备序号 {dev_no} 不在契约设备映射表中")
    temp_spec = rule["temp"]
    return {
        "device_id": device_map[dev_no],
        "boot_batch": str(fields["boot_batch"]),
        "sample_seq": int(fields["seq"]),
        "raw_value": float(fields[temp_spec["raw_field"]]),
        "raw_unit": "fixed16",
        "fw": None,
        "rule_key": "default",
        "raw_fields": {k: v for k, v in fields.items()},
    }


# ---------------------------------------------------------------- 单位换算

def convert_temperature(definition: dict, norm: dict) -> tuple[float, list[dict], str]:
    """原始值 → 标准摄氏。返回 (std_c, 换算步骤, quality_flag)。

    每一步都记录输入输出，界面可下钻；异常值只标记、不静默跳过校验。
    """
    rule = select_rule(definition, norm.get("fw"))
    spec = rule["temp"]
    steps = []
    v = norm["raw_value"]
    steps.append({"step": "raw", "value": v, "unit": norm["raw_unit"],
                  "note": f"原始字段 {spec.get('field') or spec.get('raw_field')}"})
    if "scale" in spec or "offset" in spec:
        scale = spec.get("scale", 1.0)
        offset = spec.get("offset", 0.0)
        v = v * scale + offset
        steps.append({"step": "scale", "value": v, "unit": spec["unit"],
                      "note": f"v = raw × {scale} + {offset}（定点解码）"})
    unit = spec["unit"].upper()
    if unit == "F":
        v = (v - 32.0) * 5.0 / 9.0
        steps.append({"step": "unit", "value": v, "unit": "C",
                      "note": "°F → °C：(v − 32) × 5/9"})
    elif unit == "K":
        v = v - 273.15
        steps.append({"step": "unit", "value": v, "unit": "C", "note": "K → °C：v − 273.15"})
    elif unit != "C":
        raise ContractError(f"契约声明了不支持的单位 {spec['unit']!r}")
    cal_mul = spec.get("cal_mul", 1.0)
    cal_add = spec.get("cal_add", 0.0)
    if cal_mul != 1.0 or cal_add != 0.0:
        v = v * cal_mul + cal_add
        steps.append({"step": "calibration", "value": v, "unit": "C",
                      "note": f"探头校准：v × {cal_mul} + {cal_add}"})
    std_c = round(v, 4)
    flag = "ok" if settings.VALID_TEMP_MIN_C <= std_c <= settings.VALID_TEMP_MAX_C else "out_of_range"
    steps.append({"step": "validate", "value": std_c, "unit": "C",
                  "note": f"物理区间 [{settings.VALID_TEMP_MIN_C}, {settings.VALID_TEMP_MAX_C}] → {flag}"})
    return std_c, steps, flag


# ---------------------------------------------------------------- 时钟规则

def explicit_ts(norm: dict, definition: dict) -> tuple[datetime, float, dict]:
    rule = select_rule(definition, norm.get("fw"))
    clock = rule["clock"]
    if clock["mode"] != "explicit_ts":
        raise ContractError("契约时钟规则不是 explicit_ts")
    raw = norm["raw_fields"].get(clock["field"])
    if raw is None:
        raise ContractError(f"缺少时间字段 {clock['field']}")
    ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ContractError("报文时间缺少明确时区，按契约拒绝猜测")
    info = {"mode": "explicit_ts", "field": clock["field"], "raw": raw,
            "error_s": clock["error_s"]}
    return ts.astimezone(timezone.utc), float(clock["error_s"]), info
