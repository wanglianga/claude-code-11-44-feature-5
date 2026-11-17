"""装载分区温度观测覆盖分析。

方法选择：**精确空间切分（exact）**。托盘占用、验证代表范围、隔板位置都是
轴对齐矩形，把所有矩形边投影到 x/y 轴得到分片网格，每个分片内类别均质，
面积精确、可用矩形样本手算核对，适合需要精细定位的复核。
代价：多次移动与大量重叠会让分片数随边数平方增长，大批量重算更慢、
差异展示更重。固定网格保守法（整格同资格且读数相容才计入确定覆盖、
边缘格保留未确认并显示网格精度区间）更适合车队规模大、批量扫描要控制
成本、可接受 [确定, 确定+待核] 比例区间的场景；本实现不采用格子中心点
命中冒充整格覆盖的做法。

类别（互斥，面积不重复计量，总和不超过完整货物区域）：
  supported         有观测支持（≥1 个有资格探头窗口内有效读数足够且结论一致）
  conflict          观测冲突（≥2 个有资格探头同域读数一超限一正常，保留两个物理读数）
  no_reading        空间有资格但窗口内有效读数不足（不用窗口外读数填满窗口）
  no_qualification  缺少验证资格（未确认；有探头在测但无对应布局验证时展示实际测量点）
  layout_pending    移动时刻只有范围，各可行布局类别不一致 → 布局待核
空间占用冲突（不同清单把同一空间分给不同托盘）单独标记，不纳入覆盖率分母。
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import (
    BatchSpec, CoverageCell, CoverageCorrection, CoverageExport,
    CoverageImportConflict, CoverageRun, CurrentReading, PalletPlacement,
    PartitionEvent, ProbeInstallation, ReadingVersion, Sample, VehicleLayout,
    ZoneValidation,
)
from .pipeline import bump_counter, get_counter

EPS = 1e-9
MAX_UNCERTAIN = 5           # 可行布局按 2^k 枚举，超过则整段布局待核
MAX_VARIANTS = 32
CATEGORIES = ["supported", "conflict", "no_reading", "no_qualification", "layout_pending"]
REVISION_KEYS = ["layout_revision", "loading_revision", "validation_revision",
                 "probe_revision", "partition_revision"]


class CoverageError(Exception):
    pass


# ---------------------------------------------------------------- 矩形工具

def rect_area(r) -> float:
    return max(0.0, r["w"]) * max(0.0, r["h"])


def rect_contains(outer, inner, eps=EPS) -> bool:
    """inner 完全落在 outer 内（分片边含全部矩形边，故只会全含或不相交）。"""
    return (outer["x"] <= inner["x"] + eps and outer["y"] <= inner["y"] + eps
            and outer["x"] + outer["w"] >= inner["x"] + inner["w"] - eps
            and outer["y"] + outer["h"] >= inner["y"] + inner["h"] - eps)


def rect_intersects(a, b, eps=EPS) -> bool:
    return (a["x"] < b["x"] + b["w"] - eps and a["x"] + a["w"] > b["x"] + eps
            and a["y"] < b["y"] + b["h"] - eps and a["y"] + a["h"] > b["y"] + eps)


def _dt(s) -> datetime:
    if isinstance(s, datetime):
        d = s
    else:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise CoverageError(f"时间必须带时区：{s!r}")
    return d.astimezone(timezone.utc)


def _dt_or_none(s):
    if s in (None, ""):
        return None
    return _dt(s)


def _hash(payload) -> str:
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _iso(dt):
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------- 输入版本

def input_revisions(db: Session) -> dict:
    """覆盖计算的全部输入版本指纹：任一变化都应切开/重算。"""
    revs = {k: get_counter(db, k) for k in REVISION_KEYS}
    revs["readings_max_id"] = db.query(func.max(ReadingVersion.id)).scalar() or 0
    return revs


def current_layout(db: Session, vehicle_id: str) -> VehicleLayout | None:
    return (db.query(VehicleLayout)
            .filter_by(vehicle_id=vehicle_id, status="current")
            .order_by(VehicleLayout.revision.desc()).first())


def _bump(db: Session, key: str):
    bump_counter(db, key)


def _log_correction(db: Session, kind: str, vehicle_id: str, entity_key: str,
                    effective_at=None, detail=None):
    db.add(CoverageCorrection(kind=kind, vehicle_id=vehicle_id, entity_key=entity_key,
                              effective_at=effective_at, detail=detail or {}))


# ---------------------------------------------------------------- 导入（幂等）

def import_bundle(db: Session, bundle: dict) -> dict:
    """导入覆盖输入。同键同载荷 → duplicate；同键不同载荷 → conflict 保留原值；
    布局内容变化 → 新修订（布局更正）。任何实际变更都会推高对应版本计数。"""
    results = {}
    changed = set()
    for item in bundle.get("layouts", []):
        results.setdefault("layouts", []).append(_import_layout(db, item, changed))
    db.flush()
    for item in bundle.get("batch_specs", []):
        results.setdefault("batch_specs", []).append(_import_batch_spec(db, item, changed))
    for item in bundle.get("partitions", []):
        results.setdefault("partitions", []).append(_import_partition(db, item, changed))
    for item in bundle.get("probes", []):
        results.setdefault("probes", []).append(_import_probe(db, item, changed))
    for item in bundle.get("validations", []):
        results.setdefault("validations", []).append(_import_validation(db, item, changed))
    db.flush()
    # 补证影响：新导入的验证可能让原本未确认的空间获得资格
    for res in results.get("validations", []):
        if res.get("status") == "imported":
            v = (db.query(ZoneValidation)
                 .filter_by(validation_key=res["validation_key"])
                 .order_by(ZoneValidation.revision.desc()).first())
            res["qualification_impact"] = impact_of_new_validation(db, v)
    for item in bundle.get("placements", []):
        results.setdefault("placements", []).append(_import_placement(db, item, changed))
    db.flush()
    for key in changed:
        _bump(db, key)
    db.commit()
    return {"results": results, "revisions": input_revisions(db)}


def _check_rect(rect, layout, what):
    for k in ("x", "y", "w", "h"):
        if k not in rect:
            raise CoverageError(f"{what} 缺少字段 {k}")
    if rect["w"] <= 0 or rect["h"] <= 0:
        raise CoverageError(f"{what} 宽高必须为正")
    if (rect["x"] < -EPS or rect["y"] < -EPS
            or rect["x"] + rect["w"] > layout.length_m + EPS
            or rect["y"] + rect["h"] > layout.width_m + EPS):
        raise CoverageError(f"{what} 超出车厢范围 "
                            f"[0,{layout.length_m}]×[0,{layout.width_m}]：{rect}")


def _import_layout(db: Session, item: dict, changed: set) -> dict:
    vehicle_id = item["vehicle_id"]
    for k in ("coord", "length_m", "width_m", "layers", "zones"):
        if k not in item:
            raise CoverageError(f"布局缺少字段 {k}")
    content = {"coord": item["coord"], "length_m": float(item["length_m"]),
               "width_m": float(item["width_m"]), "layers": item["layers"],
               "zones": item["zones"]}
    cur = current_layout(db, vehicle_id)
    if cur and _hash({"coord": cur.coord, "length_m": cur.length_m,
                      "width_m": cur.width_m, "layers": cur.layers,
                      "zones": cur.zones}) == _hash(content):
        return {"vehicle_id": vehicle_id, "status": "duplicate", "revision": cur.revision}
    last = (db.query(VehicleLayout).filter_by(vehicle_id=vehicle_id)
            .order_by(VehicleLayout.revision.desc()).first())
    revision = (last.revision + 1) if last else 1
    if cur:
        cur.status = "superseded"
    lay = VehicleLayout(vehicle_id=vehicle_id, revision=revision, status="current",
                        note=item.get("note", ""), **content)
    db.add(lay)
    db.flush()
    changed.add("layout_revision")
    if revision > 1:
        _log_correction(db, "layout_revise", vehicle_id, vehicle_id,
                        detail={"from_revision": revision - 1, "to_revision": revision})
    return {"vehicle_id": vehicle_id, "status": "imported", "revision": revision}


def _import_batch_spec(db: Session, item: dict, changed: set) -> dict:
    bid = item["cargo_batch_id"]
    content = {"temp_min_c": float(item["temp_min_c"]), "temp_max_c": float(item["temp_max_c"]),
               "window_minutes": int(item.get("window_minutes", 60)),
               "min_readings": int(item.get("min_readings", 1))}
    if content["temp_min_c"] >= content["temp_max_c"]:
        raise CoverageError(f"批次 {bid} 温度下限必须小于上限")
    old = db.get(BatchSpec, bid)
    h = _hash(content)
    if old:
        if _hash({"temp_min_c": old.temp_min_c, "temp_max_c": old.temp_max_c,
                  "window_minutes": old.window_minutes, "min_readings": old.min_readings}) == h:
            return {"cargo_batch_id": bid, "status": "duplicate"}
        db.add(CoverageImportConflict(entity="batch_spec", natural_key=bid,
                                      existing_hash=_hash({"temp_min_c": old.temp_min_c,
                                                           "temp_max_c": old.temp_max_c,
                                                           "window_minutes": old.window_minutes,
                                                           "min_readings": old.min_readings}),
                                      incoming_hash=h, incoming_payload=content))
        return {"cargo_batch_id": bid, "status": "conflict", "note": "与既有规格不同，保留原值"}
    db.add(BatchSpec(cargo_batch_id=bid, **content))
    return {"cargo_batch_id": bid, "status": "imported"}


def _import_partition(db: Session, item: dict, changed: set) -> dict:
    key = item["event_key"]
    content = {"vehicle_id": item["vehicle_id"], "partition_id": item["partition_id"],
               "x_position": float(item["x_position"]),
               "effective_from": _iso(_dt(item["effective_from"])),
               "effective_to": _iso(_dt_or_none(item.get("effective_to")))}
    old = db.query(PartitionEvent).filter_by(event_key=key).first()
    h = _hash(content)
    if old:
        if old.payload_hash == h:
            return {"event_key": key, "status": "duplicate"}
        db.add(CoverageImportConflict(entity="partition", natural_key=key,
                                      existing_hash=old.payload_hash, incoming_hash=h,
                                      incoming_payload=content))
        return {"event_key": key, "status": "conflict", "note": "同键不同载荷，保留原值"}
    db.add(PartitionEvent(event_key=key, vehicle_id=content["vehicle_id"],
                          partition_id=content["partition_id"], x_position=content["x_position"],
                          effective_from=_dt(content["effective_from"]),
                          effective_to=_dt_or_none(content["effective_to"]),
                          payload_hash=h))
    changed.add("partition_revision")
    return {"event_key": key, "status": "imported"}


def _import_probe(db: Session, item: dict, changed: set) -> dict:
    key = item["install_key"]
    content = {"probe_id": item["probe_id"], "vehicle_id": item["vehicle_id"],
               "x": float(item["x"]), "y": float(item["y"]), "layer": item["layer"],
               "installed_from": _iso(_dt(item["installed_from"])),
               "installed_to": _iso(_dt_or_none(item.get("installed_to")))}
    old = db.query(ProbeInstallation).filter_by(install_key=key).first()
    h = _hash(content)
    if old:
        if old.payload_hash == h:
            return {"install_key": key, "status": "duplicate"}
        db.add(CoverageImportConflict(entity="probe", natural_key=key,
                                      existing_hash=old.payload_hash, incoming_hash=h,
                                      incoming_payload=content))
        return {"install_key": key, "status": "conflict", "note": "同键不同载荷，保留原值"}
    db.add(ProbeInstallation(install_key=key, probe_id=content["probe_id"],
                             vehicle_id=content["vehicle_id"], x=content["x"], y=content["y"],
                             layer=content["layer"],
                             installed_from=_dt(content["installed_from"]),
                             installed_to=_dt_or_none(content["installed_to"]),
                             payload_hash=h))
    changed.add("probe_revision")
    return {"install_key": key, "status": "imported"}


def _import_validation(db: Session, item: dict, changed: set) -> dict:
    key = item["validation_key"]
    content = _validation_content(item)
    old = (db.query(ZoneValidation).filter_by(validation_key=key)
           .order_by(ZoneValidation.revision.desc()).first())
    h = _hash(content)
    if old:
        if old.payload_hash == h:
            return {"validation_key": key, "status": "duplicate", "revision": old.revision}
        db.add(CoverageImportConflict(entity="validation", natural_key=key,
                                      existing_hash=old.payload_hash, incoming_hash=h,
                                      incoming_payload=content))
        return {"validation_key": key, "status": "conflict",
                "note": "同键不同载荷，保留原值；范围修订请用 /revise"}
    _validate_validation(db, content)
    db.add(ZoneValidation(validation_key=key, revision=1, status="active",
                          payload_hash=h, **_row_fields(content)))
    changed.add("validation_revision")
    return {"validation_key": key, "status": "imported", "revision": 1}


def _validation_content(item: dict) -> dict:
    return {"vehicle_id": item["vehicle_id"], "structure": item["structure"],
            "probe_id": item["probe_id"],
            "install_point": {"x": float(item["install_point"]["x"]),
                              "y": float(item["install_point"]["y"]),
                              "layer": item["install_point"]["layer"]},
            "install_condition": item.get("install_condition", ""),
            "range_rect": {k: float(item["range_rect"][k]) for k in ("x", "y", "w", "h")},
            "range_layer": item["range_layer"],
            "partition_config": [{"partition_id": c["partition_id"], "x": float(c["x"])}
                                 for c in item.get("partition_config", [])],
            "condition_group": item.get("condition_group", ""),
            "valid_from": _iso(_dt(item["valid_from"])),
            "valid_to": _iso(_dt_or_none(item.get("valid_to")))}


def _row_fields(content: dict) -> dict:
    return {"vehicle_id": content["vehicle_id"], "structure": content["structure"],
            "probe_id": content["probe_id"], "install_point": content["install_point"],
            "install_condition": content["install_condition"],
            "range_rect": content["range_rect"], "range_layer": content["range_layer"],
            "partition_config": content["partition_config"],
            "condition_group": content["condition_group"],
            "valid_from": _dt(content["valid_from"]),
            "valid_to": _dt_or_none(content["valid_to"])}


def _validate_validation(db: Session, content: dict):
    """验证资料自检：范围在车厢内、货层存在、代表范围不得跨过它要求的隔板。"""
    layout = current_layout(db, content["vehicle_id"])
    if not layout:
        raise CoverageError(f"车辆 {content['vehicle_id']} 没有当前车厢布局，先导入布局")
    _check_rect(content["range_rect"], layout, "验证代表范围")
    if content["range_layer"] not in layout.layers:
        raise CoverageError(f"货层 {content['range_layer']} 不在布局货层 {layout.layers}")
    if content["install_point"]["layer"] != content["range_layer"]:
        raise CoverageError("探头安装点与代表范围必须在同一货层（不同货层不能混用）")
    r = content["range_rect"]
    for c in content["partition_config"]:
        if r["x"] < c["x"] < r["x"] + r["w"]:
            raise CoverageError(f"代表范围跨过其隔板配置中的隔板 {c['partition_id']}@x={c['x']}，"
                                f"隔板两侧的验证范围不能混用")


def _import_placement(db: Session, item: dict, changed: set) -> dict:
    key = item["placement_key"]
    content = _placement_content(item)
    existing = (db.query(PalletPlacement).filter_by(placement_key=key)
                .order_by(PalletPlacement.version).all())
    h = _hash(content)
    if existing:
        if any(e.payload_hash == h for e in existing):
            return {"placement_key": key, "status": "duplicate"}
        db.add(CoverageImportConflict(entity="placement", natural_key=key,
                                      existing_hash=existing[-1].payload_hash,
                                      incoming_hash=h, incoming_payload=content))
        return {"placement_key": key, "status": "conflict",
                "note": "同键不同载荷，保留原值；清单更正请用 /placements/{key}/correct"}
    _validate_placement(db, content)
    db.add(PalletPlacement(placement_key=key, version=1, payload_hash=h,
                           **_placement_row(content)))
    changed.add("loading_revision")
    return {"placement_key": key, "status": "imported", "version": 1}


def _placement_content(item: dict) -> dict:
    batches = [{"batch_id": b["batch_id"], "pieces": b.get("pieces")}
               for b in item["batches"]]
    return {"manifest_id": item["manifest_id"], "vehicle_id": item["vehicle_id"],
            "pallet_id": item["pallet_id"], "batches": batches,
            "rect": {k: float(item["rect"][k]) for k in ("x", "y", "w", "h")},
            "layer": item["layer"],
            "from_lo": _iso(_dt(item["from_lo"])),
            "from_hi": _iso(_dt(item.get("from_hi") or item["from_lo"])),
            "to_lo": _iso(_dt_or_none(item.get("to_lo"))),
            "to_hi": _iso(_dt_or_none(item.get("to_hi") or item.get("to_lo")))}


def _placement_row(content: dict) -> dict:
    return {"manifest_id": content["manifest_id"], "vehicle_id": content["vehicle_id"],
            "pallet_id": content["pallet_id"], "batches": content["batches"],
            "rect": content["rect"], "layer": content["layer"],
            "from_lo": _dt(content["from_lo"]), "from_hi": _dt(content["from_hi"]),
            "to_lo": _dt_or_none(content["to_lo"]), "to_hi": _dt_or_none(content["to_hi"])}


def _validate_placement(db: Session, content: dict):
    layout = current_layout(db, content["vehicle_id"])
    if not layout:
        raise CoverageError(f"车辆 {content['vehicle_id']} 没有当前车厢布局，先导入布局")
    _check_rect(content["rect"], layout, "托盘占用区域")
    if content["layer"] not in layout.layers:
        raise CoverageError(f"货层 {content['layer']} 不在布局货层 {layout.layers}")
    if not content["batches"]:
        raise CoverageError("托盘必须至少包含一个批次")
    flo, fhi = _dt(content["from_lo"]), _dt(content["from_hi"])
    tlo, thi = _dt_or_none(content["to_lo"]), _dt_or_none(content["to_hi"])
    if fhi < flo:
        raise CoverageError("生效时刻范围下界不能大于上界")
    if (tlo is None) != (thi is None):
        raise CoverageError("结束时刻范围的上下界必须同时给出或同时为空")
    if tlo is not None:
        if thi < tlo:
            raise CoverageError("结束时刻范围下界不能大于上界")
        if tlo < fhi:
            raise CoverageError("结束时刻不能早于生效时刻")


# ---------------------------------------------------------------- 更正

def correct_placement(db: Session, key: str, item: dict) -> tuple[PalletPlacement, dict]:
    """清单更正：新版本替换旧版本，旧版本保留可溯；返回更正影响。"""
    old = (db.query(PalletPlacement).filter_by(placement_key=key, corrected_by=None)
           .order_by(PalletPlacement.version.desc()).first())
    if not old:
        raise CoverageError(f"装载记录 {key} 不存在或已被更正")
    content = _placement_content({**item, "placement_key": key})
    _validate_placement(db, content)
    new = PalletPlacement(placement_key=key, version=old.version + 1,
                          payload_hash=_hash(content), **_placement_row(content))
    db.add(new)
    db.flush()
    old.corrected_by = new.id
    _bump(db, "loading_revision")
    _log_correction(db, "placement_correct", new.vehicle_id, key,
                    effective_at=new.from_lo,
                    detail={"from_version": old.version, "to_version": new.version,
                            "pallet_id": new.pallet_id})
    impact = impact_of(db, "placement", key, new.vehicle_id, new.from_lo,
                       pallet_id=new.pallet_id)
    db.commit()
    return new, impact


def revise_validation(db: Session, key: str, item: dict) -> tuple[ZoneValidation, dict]:
    """范围修订：新修订替换当前修订（旧修订保留）；按实际引用追影响。"""
    old = (db.query(ZoneValidation).filter_by(validation_key=key)
           .order_by(ZoneValidation.revision.desc()).first())
    if not old:
        raise CoverageError(f"验证资料 {key} 不存在")
    content = _validation_content({**item, "validation_key": key})
    _validate_validation(db, content)
    new = ZoneValidation(validation_key=key, revision=old.revision + 1,
                         status="active", payload_hash=_hash(content), **_row_fields(content))
    db.add(new)
    old.status = "superseded"
    _bump(db, "validation_revision")
    _log_correction(db, "validation_revise", new.vehicle_id, key,
                    effective_at=new.valid_from,
                    detail={"from_revision": old.revision, "to_revision": new.revision})
    impact = impact_of(db, "validation", key, new.vehicle_id, new.valid_from)
    db.commit()
    return new, impact


def withdraw_validation(db: Session, key: str, withdrawn_from: datetime,
                        reason: str = "") -> tuple[ZoneValidation, dict]:
    """范围撤回：withdrawn_from 起失去资格，可能波及同一验证条件下的多次运输。"""
    v = (db.query(ZoneValidation).filter_by(validation_key=key)
         .order_by(ZoneValidation.revision.desc()).first())
    if not v:
        raise CoverageError(f"验证资料 {key} 不存在")
    if v.status == "withdrawn":
        raise CoverageError(f"验证资料 {key} 已撤回（{_iso(v.withdrawn_from)}）")
    v.status = "withdrawn"
    v.withdrawn_from = withdrawn_from
    v.withdraw_reason = reason
    _bump(db, "validation_revision")
    _log_correction(db, "validation_withdraw", v.vehicle_id, key,
                    effective_at=withdrawn_from,
                    detail={"condition_group": v.condition_group, "reason": reason})
    impact = impact_of(db, "validation", key, v.vehicle_id, withdrawn_from)
    db.commit()
    return v, impact


def withdraw_group(db: Session, group: str, withdrawn_from: datetime,
                   reason: str = "") -> dict:
    """批量撤回：同一验证条件（condition_group）的全部现行验证一起失效。"""
    vals = []
    for v in (db.query(ZoneValidation).filter_by(condition_group=group).all()):
        latest = (db.query(ZoneValidation)
                  .filter_by(validation_key=v.validation_key)
                  .order_by(ZoneValidation.revision.desc()).first())
        if latest.id == v.id and latest.status == "active" and latest not in vals:
            vals.append(latest)
    if not vals:
        raise CoverageError(f"验证条件组 {group} 没有现行验证资料")
    impacts = []
    for v in vals:
        v.status = "withdrawn"
        v.withdrawn_from = withdrawn_from
        v.withdraw_reason = reason
        _log_correction(db, "validation_withdraw", v.vehicle_id, v.validation_key,
                        effective_at=withdrawn_from,
                        detail={"condition_group": group, "reason": reason, "batch": True})
        impacts.append({"validation_key": v.validation_key,
                        "impact": impact_of(db, "validation", v.validation_key,
                                            v.vehicle_id, withdrawn_from)})
    _bump(db, "validation_revision")
    db.commit()
    return {"group": group, "withdrawn": [v.validation_key for v in vals],
            "withdrawn_from": _iso(withdrawn_from), "impacts": impacts}


# ---------------------------------------------------------------- 影响查询

def impact_of(db: Session, kind: str, entity_key: str, vehicle_id: str | None = None,
              effective_at: datetime | None = None, pallet_id: str | None = None) -> dict:
    """按运行实际引用（run.used）追到货物批次与分析时段；含相关导出。"""
    q = db.query(CoverageRun)
    if vehicle_id:
        q = q.filter_by(vehicle_id=vehicle_id)
    runs = q.order_by(CoverageRun.id).all()
    hit_runs, affected, export_ids = [], {}, set()
    for r in runs:
        used = r.used or {}
        if kind == "layout":
            hit = used.get("layout_revision") is not None
        elif kind == "placement":
            hit = entity_key in (used.get("placement_keys") or [])
        elif kind == "validation":
            hit = entity_key in (used.get("validation_keys") or [])
        else:
            hit = False
        if not hit:
            continue
        if effective_at and r.window_end <= effective_at:
            continue  # 分析时段在更正生效前结束的不受影响
        cells = db.query(CoverageCell).filter_by(run_id=r.id).all()
        for c in cells:
            if kind == "validation":
                keys = {k for p in (c.probes or []) for k in p.get("validation_keys", [])}
                if entity_key not in keys:
                    continue
            elif kind == "placement" and pallet_id:
                if pallet_id not in (c.pallet_id or "").split(","):
                    continue      # 只追被更正托盘占用的空间
            wkey = (c.cargo_batch_id, _iso(c.window_start), _iso(c.window_end))
            a = affected.setdefault(wkey, {"cargo_batch_id": c.cargo_batch_id,
                                           "window_start": _iso(c.window_start),
                                           "window_end": _iso(c.window_end),
                                           "categories": set(), "runs": set()})
            a["categories"].add(c.category)
            a["runs"].add(r.id)
        hit_runs.append({"run_id": r.id, "status": r.status,
                         "cargo_batch_id": r.cargo_batch_id,
                         "window_start": _iso(r.window_start),
                         "window_end": _iso(r.window_end)})
        for exp in db.query(CoverageExport).filter_by(run_id=r.id).all():
            export_ids.add(exp.id)
    out_affected = []
    for a in sorted(affected.values(), key=lambda x: (x["cargo_batch_id"], x["window_start"])):
        out_affected.append({**a, "categories": sorted(a["categories"]),
                             "runs": sorted(a["runs"])})
    return {"kind": kind, "entity_key": entity_key,
            "affected_runs": hit_runs, "affected": out_affected,
            "affected_exports": sorted(export_ids)}


def impact_of_new_validation(db: Session, v: ZoneValidation) -> list:
    """补证影响：原本未确认、补证后才获得资格的记录。"""
    runs = (db.query(CoverageRun).filter_by(vehicle_id=v.vehicle_id)
            .order_by(CoverageRun.id).all())
    out = []
    for r in runs:
        if r.window_end <= v.valid_from or (v.valid_to and r.window_start >= v.valid_to):
            continue
        cells = (db.query(CoverageCell)
                 .filter(CoverageCell.run_id == r.id,
                         CoverageCell.category.in_(["no_qualification", "no_reading"]),
                         CoverageCell.layer == v.range_layer).all())
        hits = [c for c in cells
                if rect_intersects({"x": c.x, "y": c.y, "w": c.w, "h": c.h}, v.range_rect)
                and c.interval_end > v.valid_from
                and (v.valid_to is None or c.interval_start < v.valid_to)]
        if hits:
            out.append({"run_id": r.id, "run_status": r.status,
                        "batches": sorted({c.cargo_batch_id for c in hits}),
                        "windows": sorted({_iso(c.window_start) for c in hits}),
                        "unconfirmed_cells": len(hits)})
    return out


# ---------------------------------------------------------------- 覆盖计算

class _Ctx:
    def __init__(self, layout, placements, partitions, probes, validations, readings):
        self.layout = layout
        self.placements = placements
        self.partitions = partitions
        self.probes = probes
        self.validations = validations
        self.readings = readings          # {probe_id: [(sample_time, std_c)]}


def _vehicle_readings(db: Session, vehicle_id: str, ws: datetime, we: datetime) -> dict:
    """窗口内的有效标准读数（当前索引、归属本车、质量合格）。窗口外读数不用。"""
    rows = (db.query(ReadingVersion, Sample)
            .join(CurrentReading, CurrentReading.reading_version_id == ReadingVersion.id)
            .join(Sample, Sample.id == ReadingVersion.sample_id)
            .filter(ReadingVersion.vehicle_id == vehicle_id,
                    ReadingVersion.status == "ok",
                    ReadingVersion.quality_flag == "ok",
                    ReadingVersion.sample_time.isnot(None),
                    ReadingVersion.sample_time >= ws,
                    ReadingVersion.sample_time < we)
            .order_by(ReadingVersion.sample_time).all())
    out: dict[str, list] = {}
    for rv, s in rows:
        out.setdefault(s.device_id, []).append((rv.sample_time, rv.std_c))
    return out


def run_coverage(db: Session, vehicle_id: str, cargo_batch_id: str | None,
                 ws: datetime, we: datetime,
                 start_revisions: dict | None = None) -> CoverageRun:
    """执行一次覆盖分析（精确空间切分）。

    start_revisions 为测试钩子：传入过期版本可模拟"计算期间收到更正"，
    结果标记 stale_inputs，不能直接替换当前分析。
    """
    if we <= ws:
        raise CoverageError("分析窗口结束必须大于开始")
    layout = current_layout(db, vehicle_id)
    if not layout:
        raise CoverageError(f"车辆 {vehicle_id} 没有当前车厢布局")
    start_revs = start_revisions or input_revisions(db)

    placements = [p for p in db.query(PalletPlacement)
                  .filter_by(vehicle_id=vehicle_id)
                  .filter(PalletPlacement.corrected_by.is_(None)).all()
                  if p.from_lo < we and (p.to_hi is None or p.to_hi > ws)]
    partitions = [pe for pe in db.query(PartitionEvent).filter_by(vehicle_id=vehicle_id).all()
                  if pe.effective_from < we and (pe.effective_to is None or pe.effective_to > ws)]
    probes = [pi for pi in db.query(ProbeInstallation).filter_by(vehicle_id=vehicle_id).all()
              if pi.installed_from < we and (pi.installed_to is None or pi.installed_to > ws)]
    validations = [v for v in db.query(ZoneValidation)
                   .filter_by(vehicle_id=vehicle_id)
                   .filter(ZoneValidation.status.in_(["active", "withdrawn"])).all()
                   if v.valid_from < we and (v.valid_to is None or v.valid_to > ws)
                   and (v.withdrawn_from is None or v.withdrawn_from > ws)]
    if cargo_batch_id:
        batch_ids = [cargo_batch_id]
    else:
        batch_ids = sorted({b["batch_id"] for p in placements for b in p.batches})
    if not batch_ids:
        raise CoverageError("窗口内没有任何托盘装载记录")
    if not placements:
        raise CoverageError("窗口内没有任何托盘装载记录")
    specs = {s.cargo_batch_id: s for s in db.query(BatchSpec).all()}
    missing = [b for b in batch_ids if b not in specs]
    if missing:
        raise CoverageError(f"批次缺少温控规格（温度上下限/统计时间窗）：{missing}")

    readings = _vehicle_readings(db, vehicle_id, ws, we)
    run = CoverageRun(vehicle_id=vehicle_id, cargo_batch_id=cargo_batch_id,
                      window_start=ws, window_end=we, method="exact",
                      revisions=start_revs, used={}, stats={})
    db.add(run)
    db.flush()

    ctx = _Ctx(layout, placements, partitions, probes, validations, readings)
    used = {"validation_keys": set(), "placement_keys": {p.placement_key for p in placements},
            "partition_keys": {pe.event_key for pe in partitions},
            "probe_install_keys": {pi.install_key for pi in probes},
            "layout_revision": layout.revision}
    stats = {"batches": {}}
    for bid in batch_ids:
        cells, bstats = _compute_batch(ctx, run.id, bid, specs[bid], ws, we, used)
        for c in cells:
            db.add(c)
        stats["batches"][bid] = bstats
    run.stats = stats
    run.used = {k: (sorted(v) if isinstance(v, set) else v) for k, v in used.items()}
    run.finished_at = datetime.now(timezone.utc)

    end_revs = input_revisions(db)
    if end_revs != start_revs:
        run.status = "stale_inputs"
        diff = ",".join(k for k in start_revs if start_revs[k] != end_revs.get(k))
        run.note = (f"计算期间输入版本变化（{diff}）；"
                    f"本结果按启动时布局修订 {layout.revision} 等输入计算，"
                    f"不能直接替换当前分析")
    else:
        run.status = "current"
        q = db.query(CoverageRun).filter(
            CoverageRun.vehicle_id == vehicle_id,
            CoverageRun.window_start == ws, CoverageRun.window_end == we,
            CoverageRun.status == "current", CoverageRun.id != run.id)
        q = q.filter(CoverageRun.cargo_batch_id.is_(None) if cargo_batch_id is None
                     else CoverageRun.cargo_batch_id == cargo_batch_id)
        for old in q.all():
            old.status = "superseded"
    db.commit()
    return run


def _compute_batch(ctx: _Ctx, run_id: int, batch_id: str, spec: BatchSpec,
                   ws: datetime, we: datetime, used: dict):
    cells: list[CoverageCell] = []
    windows_stats = []
    edges = [ws]
    t = ws
    while t < we:
        t = min(t + timedelta(minutes=spec.window_minutes), we)
        edges.append(t)
    for sws, swe in zip(edges, edges[1:]):
        bounds = {sws, swe}
        for p in ctx.placements:
            for tt in (p.from_lo, p.from_hi, p.to_lo, p.to_hi):
                if tt and sws < tt < swe:
                    bounds.add(tt)
        for pe in ctx.partitions:
            for tt in (pe.effective_from, pe.effective_to):
                if tt and sws < tt < swe:
                    bounds.add(tt)
        for pi in ctx.probes:
            for tt in (pi.installed_from, pi.installed_to):
                if tt and sws < tt < swe:
                    bounds.add(tt)
        for v in ctx.validations:
            for tt in (v.valid_from, v.valid_to, v.withdrawn_from):
                if tt and sws < tt < swe:
                    bounds.add(tt)
        ordered = sorted(bounds)
        wstat = _empty_window_stat(sws, swe)
        for is_, ie in zip(ordered, ordered[1:]):
            icells = _compute_interval(ctx, run_id, batch_id, spec, sws, swe, is_, ie, used)
            cells.extend(icells)
            _accumulate(wstat, icells)
        _finalize_window_stat(wstat)
        windows_stats.append(wstat)
    totals = _merge_windows(windows_stats)
    return cells, {"spec": {"temp_min_c": spec.temp_min_c, "temp_max_c": spec.temp_max_c,
                            "window_minutes": spec.window_minutes,
                            "min_readings": spec.min_readings},
                   "windows": windows_stats, "totals": totals}


def _empty_window_stat(sws, swe) -> dict:
    return {"start": _iso(sws), "end": _iso(swe),
            "categories": {c: 0.0 for c in CATEGORIES},
            "supported_verdicts": {"normal": 0.0, "exceed": 0.0},
            "occupancy_conflict": 0.0, "cargo_area_time": 0.0, "ratios": {}}


def _accumulate(wstat: dict, icells: list[CoverageCell]):
    for c in icells:
        at = c.w * c.h * (c.interval_end - c.interval_start).total_seconds() / 3600.0
        if c.occupancy_conflict:
            wstat["occupancy_conflict"] += at   # 占用冲突不纳入覆盖率分母
            continue
        wstat["categories"][c.category] += at
        wstat["cargo_area_time"] += at
        if c.category == "supported":
            wstat["supported_verdicts"][c.reading_verdict or "normal"] += at


def _finalize_window_stat(wstat: dict):
    den = wstat["cargo_area_time"]
    for k in CATEGORIES:
        wstat["categories"][k] = round(wstat["categories"][k], 6)
    for k in wstat["supported_verdicts"]:
        wstat["supported_verdicts"][k] = round(wstat["supported_verdicts"][k], 6)
    wstat["occupancy_conflict"] = round(wstat["occupancy_conflict"], 6)
    wstat["cargo_area_time"] = round(den, 6)
    wstat["ratios"] = ({k: round(v / den, 6) for k, v in wstat["categories"].items()}
                       if den > EPS else {})


def _merge_windows(windows: list[dict]) -> dict:
    tot = {"categories": {c: 0.0 for c in CATEGORIES},
           "supported_verdicts": {"normal": 0.0, "exceed": 0.0},
           "occupancy_conflict": 0.0, "cargo_area_time": 0.0, "ratios": {}}
    for w in windows:
        for c in CATEGORIES:
            tot["categories"][c] += w["categories"][c]
        for k in tot["supported_verdicts"]:
            tot["supported_verdicts"][k] += w["supported_verdicts"][k]
        tot["occupancy_conflict"] += w["occupancy_conflict"]
        tot["cargo_area_time"] += w["cargo_area_time"]
    _finalize_window_stat(tot)
    tot.pop("start", None)
    tot.pop("end", None)
    return tot


def _compute_interval(ctx: _Ctx, run_id: int, batch_id: str, spec: BatchSpec,
                      sws: datetime, swe: datetime, is_: datetime, ie: datetime,
                      used: dict) -> list[CoverageCell]:
    """一个布局区间内的空间切分。区间内各输入恒定（边界已在有效时刻切开）。"""
    layout = ctx.layout
    active_partitions = [pe for pe in ctx.partitions
                         if pe.effective_from <= is_
                         and (pe.effective_to is None or pe.effective_to >= ie)]
    pxs = sorted({pe.x_position for pe in active_partitions})
    config_active = {(pe.partition_id, round(pe.x_position, 3)) for pe in active_partitions}
    active_probes = [pi for pi in ctx.probes
                     if pi.installed_from <= is_
                     and (pi.installed_to is None or pi.installed_to >= ie)]
    app_vals = []
    for v in ctx.validations:
        if not (v.valid_from <= is_ and (v.valid_to is None or v.valid_to >= ie)):
            continue
        if v.withdrawn_from is not None and v.withdrawn_from < ie:
            continue
        vcfg = {(c["partition_id"], round(float(c["x"]), 3))
                for c in (v.partition_config or [])}
        if vcfg != config_active:
            continue                      # 隔板配置不符：该布局下无资格
        app_vals.append(v)

    # 装载：确定在位 / 不确定（时刻只有范围）
    definite: list[PalletPlacement] = []
    uncertain: list[PalletPlacement] = []
    for p in ctx.placements:
        if p.from_hi <= is_ and (p.to_lo is None or p.to_lo >= ie):
            definite.append(p)
        elif p.from_lo >= ie or (p.to_hi is not None and p.to_hi <= is_):
            continue
        else:
            uncertain.append(p)

    # 可行布局枚举：同一托盘的各不确定位置互斥（一个托盘不会同时在两处）
    by_pallet: dict[str, list[PalletPlacement]] = {}
    for u in uncertain:
        by_pallet.setdefault(u.pallet_id, []).append(u)
    option_sets = []
    for pid, us in by_pallet.items():
        opts = [[u] for u in us]
        opts.append([])                   # 也可能已不在车上/尚未放上
        option_sets.append((pid, opts))
    n_variants = 1
    for _, opts in option_sets:
        n_variants *= len(opts)
    enumerable = n_variants <= MAX_VARIANTS and len(uncertain) <= MAX_UNCERTAIN

    variants: list[tuple[str, list[PalletPlacement]]] = []
    if enumerable:
        combos: list[list[list]] = [[]]
        for pid, opts in option_sets:
            combos = [c + [opt] for c in combos for opt in opts]
        for chosen in combos:
            active = definite + [u for opt in chosen for u in opt]
            variants.append((_variant_desc(by_pallet, chosen), active))
    else:
        variants = [("布局组合过多", definite)]

    out: list[CoverageCell] = []
    for layer in layout.layers:
        layer_ps = [p for p in definite + uncertain if p.layer == layer]
        if not layer_ps:
            continue
        layer_vals = [v for v in app_vals if v.range_layer == layer]
        xs = {0.0, layout.length_m} | set(pxs)
        ys = {0.0, layout.width_m}
        for p in layer_ps:
            xs |= {p.rect["x"], p.rect["x"] + p.rect["w"]}
            ys |= {p.rect["y"], p.rect["y"] + p.rect["h"]}
        for v in layer_vals:
            xs |= {v.range_rect["x"], v.range_rect["x"] + v.range_rect["w"]}
            ys |= {v.range_rect["y"], v.range_rect["y"] + v.range_rect["h"]}
        xs = sorted(x for x in xs if -EPS < x < layout.length_m + EPS)
        ys = sorted(y for y in ys if -EPS < y < layout.width_m + EPS)
        for x0, x1 in zip(xs, xs[1:]):
            for y0, y1 in zip(ys, ys[1:]):
                if x1 - x0 < EPS or y1 - y0 < EPS:
                    continue
                cell = {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
                results = []
                for desc, active in variants:
                    cat, detail = _classify_cell(
                        ctx, cell, layer, active, active_probes, layer_vals,
                        batch_id, spec, sws, swe, pxs, used)
                    results.append((desc, cat, detail))
                cats = {cat for _, cat, _ in results}
                if cats == {None}:
                    continue                # 各可行布局下都不是本批次货物空间
                if not enumerable:
                    first = next((d for _, c, d in results if c is not None), None)
                    out.append(_make_cell(run_id, batch_id, cell, layer, sws, swe, is_, ie,
                                          "layout_pending", None,
                                          ["layout_uncertain", "too_many_variants"],
                                          first, None, variants_note="可行布局超过枚举上限"))
                    continue
                non_null = [cat for _, cat, _ in results if cat is not None]
                definite_cat = (len(non_null) == len(results)
                                and len(set(non_null)) == 1)
                if definite_cat:
                    cat = non_null[0]
                    detail = _merge_details([d for _, _, d in results])
                    out.append(_make_cell(run_id, batch_id, cell, layer, sws, swe, is_, ie,
                                          cat, detail["verdict"], detail["reasons"],
                                          detail, None))
                else:
                    merged = _merge_details([d for _, _, d in results if d])
                    lv = [{"variant": desc, "category": cat or "not_cargo"}
                          for desc, cat, _ in results]
                    reasons = ["layout_uncertain"] + [r for r in merged["reasons"]
                                                      if r != "supported"]
                    out.append(_make_cell(run_id, batch_id, cell, layer, sws, swe, is_, ie,
                                          "layout_pending", None, reasons, merged, lv))
    return out


def _variant_desc(by_pallet: dict, chosen: list) -> str:
    """可行布局描述：每个不确定托盘的位置（或不在位）。"""
    loc = {}
    for opt in chosen:
        for u in opt:
            loc[u.pallet_id] = f"[{u.rect['x']:g},{u.rect['y']:g}]"
    parts = [f"{pid}@{loc[pid]}" if pid in loc else f"{pid}不在位"
             for pid in by_pallet]
    return "；".join(parts) if parts else "基准布局"


def _merge_details(details: list[dict]) -> dict:
    """同一分片在不同可行布局下类别一致时合并细节（读数不随布局变化）。"""
    reasons: list[str] = []
    probes: dict[str, dict] = {}
    occ = False
    conflict_with: set = set()
    mixed = False
    verdict = None
    enclosure = ""
    pallet_ids: list[str] = []
    for d in details:
        if not d:
            continue
        enclosure = enclosure or d["enclosure"]
        for r in d["reasons"]:
            if r not in reasons:
                reasons.append(r)
        for p in d["probes"]:
            cur = probes.get(p["probe_id"])
            if not cur:
                probes[p["probe_id"]] = dict(p)
            else:
                cur["validated"] = cur["validated"] or p["validated"]
                cur["validation_keys"] = sorted(set(cur.get("validation_keys", []))
                                                | set(p.get("validation_keys", [])))
        occ = occ or d["occupancy_conflict"]
        conflict_with |= set(d["conflict_with"])
        mixed = mixed or d["mixed"]
        verdict = verdict or d["verdict"]
        for pid in d["pallet_ids"]:
            if pid not in pallet_ids:
                pallet_ids.append(pid)
    return {"reasons": reasons, "probes": list(probes.values()),
            "occupancy_conflict": occ, "conflict_with": sorted(conflict_with),
            "mixed": mixed, "verdict": verdict, "enclosure": enclosure,
            "pallet_ids": pallet_ids}


def _make_cell(run_id, batch_id, cell, layer, sws, swe, is_, ie, category, verdict,
               reasons, detail, layout_variants, variants_note=None) -> CoverageCell:
    detail = detail or {}
    lv = layout_variants
    if variants_note:
        lv = [{"variant": variants_note, "category": "unknown"}]
    return CoverageCell(
        run_id=run_id, cargo_batch_id=batch_id,
        pallet_id=",".join(detail.get("pallet_ids") or ["-"]),
        layer=layer, enclosure=detail.get("enclosure", ""),
        window_start=sws, window_end=swe, interval_start=is_, interval_end=ie,
        x=cell["x"], y=cell["y"], w=cell["w"], h=cell["h"],
        category=category, reading_verdict=verdict,
        reasons=reasons, probes=detail.get("probes", []),
        layout_variants=lv,
        occupancy_conflict=detail.get("occupancy_conflict", False),
        conflict_with=detail.get("conflict_with", []),
        mixed_pallet=detail.get("mixed", False))


def _classify_cell(ctx: _Ctx, cell: dict, layer: str, active: list[PalletPlacement],
                   active_probes: list[ProbeInstallation], app_vals: list[ZoneValidation],
                   batch_id: str, spec: BatchSpec, sws: datetime, swe: datetime,
                   pxs: list[float], used: dict):
    """单个分片在一种可行布局下的类别。返回 (category|None, detail|None)。"""
    my = [p for p in active if p.layer == layer
          and any(b["batch_id"] == batch_id for b in p.batches)
          and rect_contains(p.rect, cell)]
    if not my:
        return None, None
    occupants = [p for p in active if p.layer == layer and rect_contains(p.rect, cell)]
    manifests = {p.manifest_id for p in occupants}
    occ_conflict = len(manifests) > 1
    conflict_with = sorted({f"{p.manifest_id}/{p.pallet_id}" for p in occupants
                            if p.manifest_id != my[0].manifest_id})
    mixed = any(len(p.batches) > 1 for p in my)
    enclosure = _enclosure_of(cell["x"] + cell["w"] / 2.0, pxs,
                              ctx.layout.length_m)

    qualified: dict[str, dict] = {}
    for pi in active_probes:
        if pi.layer != layer:
            continue
        for v in app_vals:
            if v.probe_id != pi.probe_id:
                continue
            ip = v.install_point
            if (abs(ip["x"] - pi.x) > 0.01 or abs(ip["y"] - pi.y) > 0.01
                    or ip["layer"] != pi.layer):
                continue                # 探头已迁移：旧安装条件下的验证不适用
            if rect_contains(v.range_rect, cell):
                q = qualified.setdefault(pi.probe_id, {"keys": set(), "point": [pi.x, pi.y]})
                q["keys"].add(v.validation_key)
                used["validation_keys"].add(v.validation_key)

    probes_detail = []
    for pid, q in qualified.items():
        rd = [r for r in ctx.readings.get(pid, []) if sws <= r[0] < swe]
        temps = [round(t, 4) for _, t in rd]
        enough = len(rd) >= spec.min_readings
        verdict = None
        if enough:
            verdict = ("exceed" if any(t < spec.temp_min_c - EPS
                                       or t > spec.temp_max_c + EPS for t in temps)
                       else "normal")
        probes_detail.append({"probe_id": pid, "validated": True,
                              "validation_keys": sorted(q["keys"]), "point": q["point"],
                              "readings": len(rd), "temps": temps,
                              "verdict": verdict if enough else "insufficient"})
    for pi in active_probes:
        if pi.layer != layer or pi.probe_id in qualified:
            continue
        rd = [r for r in ctx.readings.get(pi.probe_id, []) if sws <= r[0] < swe]
        if not rd:
            continue
        # 探头在测但没有对应布局的验证：展示实际测量点，不把点观测等同于整块区域
        probes_detail.append({"probe_id": pi.probe_id, "validated": False,
                              "point": [pi.x, pi.y], "readings": len(rd),
                              "temps": [round(t, 4) for _, t in rd], "verdict": None})

    observing = [p for p in probes_detail
                 if p["validated"] and p["verdict"] in ("normal", "exceed")]
    verdicts = {p["verdict"] for p in observing}
    reasons: list[str] = []
    if len(verdicts) > 1:
        category, verdict = "conflict", None
        reasons.append("observation_conflict")
    elif observing:
        category, verdict = "supported", observing[0]["verdict"]
        reasons.append("supported")
        if len(observing) > 1:
            reasons.append("multi_support")
    elif qualified:
        category, verdict = "no_reading", None
        reasons.append("qualified_no_reading")
    else:
        category, verdict = "no_qualification", None
        reasons.append("no_validated_range")
        if any(not p["validated"] for p in probes_detail):
            reasons.append("unvalidated_probe_present")
    if occ_conflict:
        reasons.append("occupancy_conflict")
    if mixed:
        reasons.append("mixed_pallet_common_coverage")
    detail = {"probes": probes_detail, "reasons": reasons, "verdict": verdict,
              "occupancy_conflict": occ_conflict, "conflict_with": conflict_with,
              "mixed": mixed, "enclosure": enclosure,
              "pallet_ids": [p.pallet_id for p in my]}
    return category, detail


def _enclosure_of(x: float, pxs: list[float], length: float) -> str:
    """封闭分区标识：隔板把车厢切成若干封闭区间，不同封闭分区不能混用验证。"""
    bounds = [0.0] + list(pxs) + [length]
    for i in range(len(bounds) - 1):
        if bounds[i] - EPS <= x <= bounds[i + 1] + EPS:
            return f"E{i}[{bounds[i]:g},{bounds[i + 1]:g}]"
    return f"E?[{x:g}]"


# ---------------------------------------------------------------- 结果查询

def run_dict(db: Session, run: CoverageRun) -> dict:
    cur = input_revisions(db)
    return {"id": run.id, "vehicle_id": run.vehicle_id,
            "cargo_batch_id": run.cargo_batch_id,
            "window_start": _iso(run.window_start), "window_end": _iso(run.window_end),
            "method": run.method, "status": run.status, "note": run.note,
            "revisions": run.revisions, "used": run.used,
            "inputs_stale": run.revisions != cur,
            "stats": run.stats, "created_at": _iso(run.created_at),
            "finished_at": _iso(run.finished_at)}


def cell_dict(c: CoverageCell) -> dict:
    return {"id": c.id, "cargo_batch_id": c.cargo_batch_id, "pallet_id": c.pallet_id,
            "layer": c.layer, "enclosure": c.enclosure,
            "window_start": _iso(c.window_start), "window_end": _iso(c.window_end),
            "interval_start": _iso(c.interval_start), "interval_end": _iso(c.interval_end),
            "rect": {"x": c.x, "y": c.y, "w": c.w, "h": c.h},
            "category": c.category, "reading_verdict": c.reading_verdict,
            "reasons": c.reasons, "probes": c.probes,
            "layout_variants": c.layout_variants,
            "occupancy_conflict": c.occupancy_conflict,
            "conflict_with": c.conflict_with, "mixed_pallet": c.mixed_pallet}


def unconfirmed_of_run(db: Session, run_id: int) -> dict:
    """未确认原因查询：缺资格/缺读数/布局待核的空间及其原因，含实际测量点。"""
    cells = (db.query(CoverageCell)
             .filter(CoverageCell.run_id == run_id,
                     CoverageCell.category.in_(["no_qualification", "no_reading",
                                                "layout_pending"]))
             .order_by(CoverageCell.cargo_batch_id, CoverageCell.window_start,
                       CoverageCell.x).all())
    points = {}
    for c in cells:
        for p in c.probes or []:
            if not p.get("validated"):
                points[(p["probe_id"], tuple(p["point"]))] = {
                    "probe_id": p["probe_id"], "point": p["point"],
                    "note": "探头在测但缺少对应布局的验证，点观测不能等同于整块区域"}
    return {"run_id": run_id, "unconfirmed_cells": [cell_dict(c) for c in cells],
            "unvalidated_probe_points": list(points.values())}


def export_run(db: Session, run_id: int, label: str = "") -> CoverageExport:
    run = db.get(CoverageRun, run_id)
    if not run:
        raise CoverageError(f"运行 {run_id} 不存在")
    cells = (db.query(CoverageCell).filter_by(run_id=run_id)
             .order_by(CoverageCell.id).all())
    payload = {"run": run_dict(db, run), "cells": [cell_dict(c) for c in cells],
               "basis": {"revisions": run.revisions, "used": run.used}}
    exp = CoverageExport(run_id=run_id, label=label, payload=payload)
    db.add(exp)
    db.commit()
    return exp


def export_dict(db: Session, exp: CoverageExport, with_payload: bool = False) -> dict:
    out = {"id": exp.id, "run_id": exp.run_id, "label": exp.label,
           "created_at": _iso(exp.created_at)}
    if with_payload:
        out["payload"] = exp.payload
        # 导出后的更正范围：按运行实际引用追到更正事件
        run = db.get(CoverageRun, exp.run_id)
        used = (run.used or {}) if run else {}
        keys = set(used.get("validation_keys") or []) | set(used.get("placement_keys") or [])
        corrs = []
        for corr in (db.query(CoverageCorrection)
                     .filter(CoverageCorrection.vehicle_id == (run.vehicle_id if run else ""))
                     .order_by(CoverageCorrection.id).all()):
            if corr.created_at <= exp.created_at:
                continue
            if corr.entity_key in keys or corr.kind == "layout_revise":
                corrs.append({"id": corr.id, "kind": corr.kind,
                              "entity_key": corr.entity_key,
                              "effective_at": _iso(corr.effective_at),
                              "detail": corr.detail,
                              "created_at": _iso(corr.created_at)})
        out["subsequent_corrections"] = corrs
    return out


def compare_runs(db: Session, a_id: int, b_id: int) -> dict:
    """布局更正前后比较：分窗口类别面积时间对比 + 类别变化的分片。"""
    a, b = db.get(CoverageRun, a_id), db.get(CoverageRun, b_id)
    if not a or not b:
        raise CoverageError("运行不存在")
    ca = (db.query(CoverageCell).filter_by(run_id=a_id)
          .order_by(CoverageCell.id).all())
    cb = (db.query(CoverageCell).filter_by(run_id=b_id)
          .order_by(CoverageCell.id).all())

    def key(c):
        return (c.cargo_batch_id, c.pallet_id, c.layer, _iso(c.window_start),
                _iso(c.interval_start), round(c.x, 6), round(c.y, 6),
                round(c.w, 6), round(c.h, 6))

    ka, kb = {key(c): c for c in ca}, {key(c): c for c in cb}
    changed, only_a, only_b = [], [], []
    for k in sorted(ka.keys() & kb.keys()):
        if ka[k].category != kb[k].category:
            changed.append({"cell": cell_dict(ka[k]), "from": ka[k].category,
                            "to": kb[k].category})
    for k in sorted(ka.keys() - kb.keys()):
        only_a.append(cell_dict(ka[k]))
    for k in sorted(kb.keys() - ka.keys()):
        only_b.append(cell_dict(kb[k]))

    windows = {}
    for run, cells in (("a", ca), ("b", cb)):
        for c in cells:
            wk = (c.cargo_batch_id, _iso(c.window_start))
            w = windows.setdefault(wk, {"cargo_batch_id": c.cargo_batch_id,
                                        "window_start": _iso(c.window_start),
                                        "a": {cat: 0.0 for cat in CATEGORIES},
                                        "b": {cat: 0.0 for cat in CATEGORIES}})
            at = c.w * c.h * (c.interval_end - c.interval_start).total_seconds() / 3600.0
            if not c.occupancy_conflict:
                w[run][c.category] += at
    for w in windows.values():
        for run in ("a", "b"):
            w[run] = {k: round(v, 6) for k, v in w[run].items()}
    return {"run_a": {"id": a.id, "status": a.status, "revisions": a.revisions},
            "run_b": {"id": b.id, "status": b.status, "revisions": b.revisions},
            "windows": sorted(windows.values(),
                              key=lambda w: (w["cargo_batch_id"], w["window_start"])),
            "changed_cells": changed[:200], "changed_cells_total": len(changed),
            "only_in_a": only_a[:50], "only_in_b": only_b[:50]}
