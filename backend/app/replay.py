"""契约修订后的重放。

只重放依赖该契约的原始信封：
  - 曾用该契约（任一历史修订）解析成功的信封/帧；
  - 因没有契约而 pending、且现在被新修订覆盖的信封/帧。
完整新结果先挂在运行上，校验（独立复算 + 版本检查）通过后一次性切换当前索引；
重放期间收到新信封（可能是新固件）或安装关系被修订 → 提交拒绝，必须重来。
"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import contracts as C
from .models import (
    Frame, ParsedMessage, ProtocolContract, RawEnvelope, ReadingVersion,
    StandardRun,
)
from .pipeline import (
    _fill_parsed, get_counter, json_payload_hash, process_parsed,
    switch_current,
)
import hashlib
import json


class ReplayAbort(Exception):
    pass


def select_targets(db: Session, contract: ProtocolContract):
    """返回 (json_envelope_ids, binary_frame_ids, 待解析记录 id 列表)。"""
    key = contract.contract_key
    # 曾用本契约任一修订解析的
    dep_pms = (db.query(ParsedMessage)
               .join(ProtocolContract, ParsedMessage.contract_id == ProtocolContract.id)
               .filter(ProtocolContract.contract_key == key).all())
    env_ids = {pm.envelope_id for pm in dep_pms if pm.envelope_id}
    frame_ids = {pm.frame_id for pm in dep_pms if pm.frame_id}
    # 待解析且被新修订覆盖的
    pending_ids = []
    pend = db.query(ParsedMessage).filter(ParsedMessage.status == "pending_contract").all()
    for pm in pend:
        if pm.envelope_id:
            env = db.get(RawEnvelope, pm.envelope_id)
            msg = json.loads(bytes(env.payload).decode("utf-8"))
            if C.contract_matches(contract, "json", fw=msg.get("fw"), version=None):
                env_ids.add(pm.envelope_id)
                pending_ids.append(pm.id)
        elif pm.frame_id:
            fr = db.get(Frame, pm.frame_id)
            if fr.status == "ok" and C.contract_matches(contract, "binary", fw=None,
                                                        version=fr.payload[1]):
                frame_ids.add(pm.frame_id)
                pending_ids.append(pm.id)
    return sorted(env_ids), sorted(frame_ids), pending_ids


def run_replay(db: Session, contract: ProtocolContract) -> StandardRun:
    if contract.status != "active":
        raise ReplayAbort("只能按已生效的契约重放")
    prev = (db.query(ProtocolContract)
            .filter(ProtocolContract.contract_key == contract.contract_key,
                    ProtocolContract.revision < contract.revision)
            .order_by(ProtocolContract.revision.desc()).first())
    run = StandardRun(
        kind="replay", contract_key=contract.contract_key,
        from_revision=prev.revision if prev else None, to_revision=contract.revision,
        started_relation_revision=get_counter(db, "relation_revision"),
        started_envelope_id=db.query(func.max(RawEnvelope.id)).scalar() or 0,
    )
    db.add(run)
    db.flush()

    env_ids, frame_ids, pending_ids = select_targets(db, contract)
    stats = {"envelopes": len(env_ids), "frames": len(frame_ids),
             "parsed_new": 0, "parsed_dup": 0, "conflict": 0, "error": 0}

    for env_id in env_ids:
        env = db.get(RawEnvelope, env_id)
        msg = json.loads(bytes(env.payload).decode("utf-8"))
        pm = ParsedMessage(batch_id=env.batch_id, envelope_id=env.id,
                           contract_id=contract.id, run_id=run.id,
                           payload_hash=json_payload_hash(msg), status="error")
        db.add(pm)
        try:
            norm = C.normalize_json(contract.definition, msg)
        except (C.ContractError, KeyError, ValueError, TypeError) as e:
            pm.error = f"重放解析失败：{e}"
            stats["error"] += 1
            db.flush()
            continue
        _fill_parsed(pm, norm)
        process_parsed(db, pm, norm, contract, run=run, commit=False)
        stats[pm.status] = stats.get(pm.status, 0) + 1

    for frame_id in frame_ids:
        fr = db.get(Frame, frame_id)
        pm = ParsedMessage(batch_id=fr.batch_id, frame_id=fr.id,
                           contract_id=contract.id, run_id=run.id,
                           payload_hash=hashlib.sha256(fr.payload).hexdigest(),
                           status="error")
        db.add(pm)
        try:
            norm = C.normalize_binary(contract.definition, bytes(fr.payload))
        except (C.ContractError, KeyError, ValueError, TypeError) as e:
            pm.error = f"重放解析失败：{e}"
            stats["error"] += 1
            db.flush()
            continue
        _fill_parsed(pm, norm)
        process_parsed(db, pm, norm, contract, run=run, commit=False)
        stats[pm.status] = stats.get(pm.status, 0) + 1

    db.flush()
    try:
        _validate(db, run, contract)
    except ReplayAbort as e:
        run.status = "aborted"
        run.abort_reason = str(e)
        run.finished_at = func.now()
        run.stats = stats
        db.commit()
        return run

    # 校验通过：一次性切换当前索引；旧的待解析记录标记为已解决
    staged = db.query(ReadingVersion).filter(ReadingVersion.run_id == run.id).all()
    for rv in staged:
        switch_current(db, rv.sample_id, rv.id)
    for pm_id in pending_ids:
        old = db.get(ParsedMessage, pm_id)
        if old and old.status == "pending_contract":
            old.status = "resolved"
            old.error = f"已由契约修订 {contract.revision} 重放解析"
    run.status = "committed"
    run.finished_at = func.now()
    stats["switched"] = len(staged)
    run.stats = stats
    db.commit()
    return run


def _validate(db: Session, run: StandardRun, contract: ProtocolContract):
    """提交前校验：结果复算一致 + 契约/关系/信封版本未漂移。"""
    staged = db.query(ReadingVersion).filter(ReadingVersion.run_id == run.id).all()
    if not staged:
        raise ReplayAbort("重放没有产生任何新结果，拒绝切换空结果集")
    # 1) 独立复算：同样本同契约重算，必须与暂存结果一致（防止运行中锚点等数据漂移）
    from .pipeline import standardize_sample
    from .models import Sample
    mismatches = 0
    for rv in staged:
        sample = db.get(Sample, rv.sample_id)
        pm = db.get(ParsedMessage, sample.first_parsed_id)
        norm = {
            "device_id": sample.device_id, "boot_batch": sample.boot_batch,
            "sample_seq": sample.sample_seq, "raw_value": sample.raw_value,
            "raw_unit": sample.raw_unit, "fw": (pm.fields or {}).get("fw") if pm else None,
            "rule_key": "default", "raw_fields": sample.raw_fields,
        }
        check = standardize_sample(db, sample, norm, contract,
                                   source="replay", run=None, commit=False)
        db.delete(check)  # 复算副本不入库
        db.flush()
        if (check.std_c != rv.std_c or check.status != rv.status
                or check.vehicle_id != rv.vehicle_id
                or check.time_error_s != rv.time_error_s):
            mismatches += 1
    if mismatches:
        raise ReplayAbort(f"{mismatches} 条暂存结果复算不一致，运行期间基础数据已变化")
    # 2) 安装关系版本
    now_rel = get_counter(db, "relation_revision")
    if now_rel != run.started_relation_revision:
        raise ReplayAbort(
            f"重放期间设备安装关系被修订（{run.started_relation_revision} → {now_rel}），"
            f"提交会产生归属过期的信号，已中止")
    # 3) 新信封（可能是新固件）
    now_env = db.query(func.max(RawEnvelope.id)).scalar() or 0
    if now_env != run.started_envelope_id:
        raise ReplayAbort("重放期间收到新的原始信封（可能含新固件），已中止，请重新发起重放")
    # 4) 契约仍是当前生效修订
    current = (db.query(ProtocolContract)
               .filter_by(contract_key=contract.contract_key, status="active")
               .order_by(ProtocolContract.revision.desc()).first())
    if not current or current.revision != contract.revision:
        raise ReplayAbort("重放期间契约又发生了修订，已中止")
