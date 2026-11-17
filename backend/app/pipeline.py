"""接入与标准化管线。

集中解析：发送器只无损转发，这里统一按版本化契约解析。
  ingest_json / ingest_binary_chunk / close_connection  —— 接入
  register_parsed                                       —— 幂等注册（去重/冲突）
  standardize_sample                                    —— 标准化（换算+时钟映射+归属）
"""
import hashlib
import json
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import contracts as C
from .clockmap import map_counter_to_time
from .framing import Framer
from .models import (
    ClockAnchor, ConnectionState, CurrentReading, DeviceRelation, Frame,
    MetaCounter, ParsedMessage, ProtocolContract, RawEnvelope, ReadingVersion,
    Sample, SampleConflict, TransportBatch,
)


# ---------------------------------------------------------------- 工具

def get_counter(db: Session, key: str) -> int:
    row = db.get(MetaCounter, key)
    return row.value if row else 0


def bump_counter(db: Session, key: str) -> int:
    row = db.get(MetaCounter, key)
    if not row:
        row = MetaCounter(key=key, value=0)
        db.add(row)
        db.flush()
    row.value += 1
    db.flush()
    return row.value


def active_contracts(db: Session, kind: str | None = None):
    q = db.query(ProtocolContract).filter(ProtocolContract.status == "active")
    if kind:
        q = q.filter(ProtocolContract.kind == kind)
    return q.all()


def find_contract(db: Session, kind: str, fw=None, version=None):
    for c in active_contracts(db, kind):
        if C.contract_matches(c, kind, fw, version):
            return c
    return None


def json_payload_hash(msg: dict) -> str:
    canon = json.dumps(msg, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 接入：JSON

def ingest_json(db: Session, batch: TransportBatch, conn_key: str, msg: dict) -> ParsedMessage:
    conn = _get_or_create_conn(db, batch, conn_key, protocol="json")
    seq = db.query(func.count(RawEnvelope.id)).filter(RawEnvelope.connection_id == conn.id).scalar() or 0
    env = RawEnvelope(
        batch_id=batch.id, connection_id=conn.id, kind="json_msg", seq_in_conn=seq,
        payload=json.dumps(msg, ensure_ascii=False).encode("utf-8"),
    )
    db.add(env)
    db.flush()

    pm = ParsedMessage(batch_id=batch.id, envelope_id=env.id,
                       payload_hash=json_payload_hash(msg), status="error")
    db.add(pm)
    fw = msg.get("fw")
    contract = find_contract(db, "json", fw=fw)
    if not contract:
        pm.status = "pending_contract"
        pm.error = f"固件 {fw!r} 没有生效的协议契约，保留待解析"
        db.commit()
        return pm
    pm.contract_id = contract.id
    try:
        norm = C.normalize_json(contract.definition, msg)
    except (C.ContractError, KeyError, ValueError, TypeError) as e:
        pm.status = "error"
        pm.error = f"契约解析失败：{e}"
        db.commit()
        return pm
    _fill_parsed(pm, norm)
    process_parsed(db, pm, norm, contract)
    db.commit()
    return pm


# ---------------------------------------------------------------- 接入：二进制

def ingest_binary_chunk(db: Session, batch: TransportBatch, conn_key: str,
                        chunk: bytes) -> list[ParsedMessage]:
    conn = _get_or_create_conn(db, batch, conn_key, protocol="binary")
    seq = db.query(func.count(RawEnvelope.id)).filter(RawEnvelope.connection_id == conn.id).scalar() or 0
    start = conn.received_bytes
    env = RawEnvelope(
        batch_id=batch.id, connection_id=conn.id, kind="bin_chunk", seq_in_conn=seq,
        conn_off_start=start, conn_off_end=start + len(chunk), payload=chunk,
    )
    db.add(env)
    conn.received_bytes = start + len(chunk)

    framer = Framer(conn)
    frames = framer.feed(chunk)
    buf, base = framer.state()
    conn.buffer, conn.offset_base = buf, base
    for f in frames:
        db.add(f)
    db.flush()
    pms = []
    for f in frames:
        if f.status == "ok":
            pms.append(_parse_binary_frame(db, batch, f))
    db.commit()
    return pms


def close_connection(db: Session, batch: TransportBatch, conn_key: str) -> list[Frame]:
    conn = db.query(ConnectionState).filter_by(batch_id=batch.id, conn_key=conn_key).first()
    if not conn or not conn.open:
        return []
    conn.open = False
    conn.closed_at = func.now()
    frames = []
    if conn.protocol == "binary":
        framer = Framer(conn)
        frames = framer.close()
        buf, base = framer.state()
        conn.buffer, conn.offset_base = buf, base
        for f in frames:
            db.add(f)
    db.commit()
    return frames


def _parse_binary_frame(db: Session, batch: TransportBatch, frame: Frame) -> ParsedMessage:
    pm = ParsedMessage(batch_id=batch.id, frame_id=frame.id,
                       payload_hash=hashlib.sha256(frame.payload).hexdigest(),
                       status="error")
    db.add(pm)
    version = frame.payload[1]
    contract = find_contract(db, "binary", version=version)
    if not contract:
        pm.status = "pending_contract"
        pm.error = f"二进制协议版本 {version} 没有生效的契约，保留待解析"
        db.flush()
        return pm
    pm.contract_id = contract.id
    try:
        norm = C.normalize_binary(contract.definition, bytes(frame.payload))
    except (C.ContractError, KeyError, ValueError, TypeError) as e:
        pm.status = "error"
        pm.error = f"契约解析失败：{e}"
        db.flush()
        return pm
    _fill_parsed(pm, norm)
    process_parsed(db, pm, norm, contract)
    db.flush()
    return pm


def _fill_parsed(pm: ParsedMessage, norm: dict):
    pm.device_id = norm["device_id"]
    pm.boot_batch = norm["boot_batch"]
    pm.sample_seq = norm["sample_seq"]
    pm.fields = {"raw_value": norm["raw_value"], "raw_unit": norm["raw_unit"],
                 "fw": norm.get("fw"), "rule_key": norm.get("rule_key"),
                 "raw_fields": norm["raw_fields"]}


# ---------------------------------------------------------------- 幂等注册

def register_parsed(db: Session, pm: ParsedMessage, norm: dict, contract,
                    run=None) -> Sample | None:
    """幂等键 (device, boot_batch, sample_seq)：
    同键同载荷 → 不增样本；同键不同载荷 → 冲突，保留原值。
    """
    key = (norm["device_id"], norm["boot_batch"], norm["sample_seq"])
    db.flush()  # 确保 pm.id 已生成，样本/冲突要引用
    sample = (db.query(Sample).filter_by(device_id=key[0], boot_batch=key[1],
                                         sample_seq=key[2]).first())
    if sample:
        if sample.payload_hash == pm.payload_hash:
            pm.status = "parsed_dup"
            if run is None:  # 接入时重复样本无需重算；重放仍需按新契约重算
                return None
            return sample
        pm.status = "conflict"
        # 同一冲突载荷只记一次（重放不会重复制造冲突记录）
        dup_conflict = (db.query(SampleConflict)
                        .filter_by(sample_id=sample.id, incoming_hash=pm.payload_hash)
                        .first())
        if not dup_conflict:
            db.add(SampleConflict(
                sample_id=sample.id, incoming_parsed_id=pm.id,
                existing_hash=sample.payload_hash, incoming_hash=pm.payload_hash,
                incoming_fields=pm.fields,
            ))
        db.flush()
        return None

    sample = Sample(
        device_id=key[0], boot_batch=key[1], sample_seq=key[2],
        raw_value=norm["raw_value"], raw_unit=norm["raw_unit"],
        raw_fields=norm["raw_fields"], payload_hash=pm.payload_hash,
        first_batch_id=pm.batch_id, first_parsed_id=pm.id,
    )
    db.add(sample)
    db.flush()
    pm.status = "parsed_new"
    return sample


def process_parsed(db: Session, pm: ParsedMessage, norm: dict, contract,
                   run=None, commit: bool = True) -> Sample | None:
    """解析成功后的统一处理：幂等注册 + 标准化。

    接入路径 commit=True 立即切换当前索引；重放路径 commit=False，
    结果先挂在运行上，校验通过后一次性切换。
    """
    sample = register_parsed(db, pm, norm, contract, run=run)
    if sample is not None:
        standardize_sample(db, sample, norm, contract,
                           source="replay" if run else "ingest",
                           run=run, commit=commit)
    return sample


# ---------------------------------------------------------------- 标准化

def standardize_sample(db: Session, sample: Sample, norm: dict, contract,
                       source: str = "ingest", run=None, commit: bool = True) -> ReadingVersion:
    definition = contract.definition
    std_c, steps, flag = C.convert_temperature(definition, norm)
    rule = C.select_rule(definition, norm.get("fw"))
    clock = rule["clock"]

    t = err = None
    clock_info: dict = {}
    if clock["mode"] == "explicit_ts":
        t, err, clock_info = C.explicit_ts(norm, definition)
    else:
        anchors = (db.query(ClockAnchor)
                   .filter_by(device_id=sample.device_id, boot_batch=sample.boot_batch)
                   .all())
        t, err, clock_info = map_counter_to_time(
            sample.sample_seq, anchors, clock["interval_s"], clock["drift_s_per_step"])

    vehicle = box = cargo = None
    if t is None:
        status, detail = "pending_anchor", clock_info.get("reason", "缺少可信锚点")
    else:
        rel, why = attribute(db, sample.device_id, t, err)
        if rel:
            status, detail = "ok", ""
            vehicle, box, cargo = rel.vehicle_id, rel.box_id, rel.cargo_batch_id
        else:
            status, detail = "pending_attribution", why

    rv = ReadingVersion(
        sample_id=sample.id, run_id=run.id if run else None,
        std_c=std_c, quality_flag=flag, sample_time=t, time_error_s=err,
        vehicle_id=vehicle, box_id=box, cargo_batch_id=cargo,
        status=status, status_detail=detail,
        contract_key=contract.contract_key, contract_revision=contract.revision,
        relation_revision=get_counter(db, "relation_revision"),
        conversion={"steps": steps, "clock": clock_info},
        source=source,
    )
    db.add(rv)
    db.flush()
    if commit:
        switch_current(db, sample.id, rv.id)
    return rv


def switch_current(db: Session, sample_id: int, reading_version_id: int):
    cur = db.get(CurrentReading, sample_id)
    if cur:
        cur.reading_version_id = reading_version_id
    else:
        db.add(CurrentReading(sample_id=sample_id, reading_version_id=reading_version_id))
    db.flush()


def attribute(db: Session, probe_id: str, t, err_s: float):
    """按采样时间 ± 误差窗口与安装区间连接（不按接收时间）。

    误差窗口跨过安装边界（如换车）→ 无法唯一归属，保持待归属。
    """
    start = t - timedelta(seconds=err_s)
    end = t + timedelta(seconds=err_s)
    rels = (db.query(DeviceRelation)
            .filter(DeviceRelation.probe_id == probe_id,
                    DeviceRelation.valid_from < end,
                    (DeviceRelation.valid_to.is_(None)) | (DeviceRelation.valid_to > start))
            .all())
    covering = [r for r in rels
                if r.valid_from <= start and (r.valid_to is None or r.valid_to >= end)]
    if len(covering) == 1:
        return covering[0], ""
    if not rels:
        return None, f"探头 {probe_id} 在时间窗口 [{start.isoformat()}, {end.isoformat()}] 内没有任何安装区间"
    if not covering:
        return None, (f"误差窗口 [{start.isoformat()}, {end.isoformat()}] 跨过安装边界，"
                      f"涉及 {len(rels)} 段区间，无法唯一归属")
    return None, f"误差窗口同时落入 {len(covering)} 段重叠区间，归属不唯一"


def _get_or_create_conn(db: Session, batch: TransportBatch, conn_key: str,
                        protocol: str) -> ConnectionState:
    conn = db.query(ConnectionState).filter_by(batch_id=batch.id, conn_key=conn_key).first()
    if conn:
        return conn
    conn = ConnectionState(batch_id=batch.id, conn_key=conn_key, protocol=protocol)
    db.add(conn)
    db.flush()
    return conn
