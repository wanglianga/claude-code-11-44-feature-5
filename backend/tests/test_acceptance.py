"""端到端验收：真实发送两类协议，组合全部要求场景，核对标准温度、
样本总数、时间范围、归属与消费端接收结果。

场景覆盖：半帧、多帧、坏校验、负温、序号重启、断网补传、换车边界、
契约修订、重发去重、载荷冲突、缺锚点、超量程。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sender"))

from scenarios import (  # noqa: E402
    JSON_TEMP_R2, scenario_binary_main, scenario_binary_restart,
    scenario_contract_revision, scenario_json_basic, scenario_json_new_firmware,
)

pytestmark = pytest.mark.usefixtures("server")


# ---------------------------------------------------------------- 工具

def samples_by_key(c):
    out = {}
    for s in c.get("/api/samples")["samples"]:
        out[(s["device_id"], s["boot_batch"], s["sample_seq"])] = s
    return out


def batch_by_note(c, needle):
    for b in c.get("/api/batches")["batches"]:
        if needle in b["note"]:
            return b
    raise AssertionError(f"找不到批次：{needle}")


def almost(v, want, tol=1e-6):
    assert v is not None and abs(v - want) < 1e-4, f"{v} != {want}"


# ---------------------------------------------------------------- 主流程

def test_full_acceptance(client):
    c = client
    c.reset()
    c.seed()

    # ---- 1. 二进制：半帧拼合 + 多帧 + 坏校验 + 负温 + 边界 + 超量程 + 截断
    scenario_binary_main(c)
    b1 = batch_by_note(c, "换车后补传")
    assert b1["frames_ok"] == 5, b1
    assert b1["frames_bad_crc"] == 1, b1
    assert b1["frames_garbage"] == 2, b1          # 噪声 2B + 坏帧残余 15B
    assert b1["frames_truncated"] == 1, b1
    assert b1["parsed_new"] == 5, b1
    assert b1["pending_attribution"] == 1, b1

    s = samples_by_key(c)
    # 标准温度：定点 80 × 0.0625 = 5.0，校准 −0.2 → 4.8
    almost(s[("PRB-2002", "1", 150)]["std_c"], 4.8)
    assert s[("PRB-2002", "1", 150)]["sample_time"].startswith("2026-11-15T10:25:00")
    almost(s[("PRB-2002", "1", 150)]["time_error_s"], 4.5)
    # 断网补传：接收时车辆已是 VH-02，按采样时间仍归 VH-01/LOT-A
    almost(s[("PRB-2002", "1", 339)]["std_c"], 4.8)
    assert s[("PRB-2002", "1", 339)]["vehicle_id"] == "VH-01"
    assert s[("PRB-2002", "1", 339)]["cargo_batch_id"] == "LOT-A"
    # 负温：−56 × 0.0625 = −3.5，校准 −0.2 → −3.7
    almost(s[("PRB-2002", "1", 340)]["std_c"], -3.7)
    # 换车边界：误差窗口跨过 12:00 → 保持待归属
    assert s[("PRB-2002", "1", 340)]["status"] == "pending_attribution"
    assert s[("PRB-2002", "1", 340)]["vehicle_id"] is None
    # 坏校验后面的完整帧没有被吞掉
    almost(s[("PRB-2002", "1", 344)]["std_c"], 5.8)
    assert s[("PRB-2002", "1", 344)]["vehicle_id"] == "VH-02"
    assert s[("PRB-2002", "1", 344)]["cargo_batch_id"] == "LOT-B"
    # 超量程：1700 × 0.0625 − 0.2 = 106.05 → 标记但不静默丢弃
    almost(s[("PRB-2002", "1", 200)]["std_c"], 106.05)
    assert s[("PRB-2002", "1", 200)]["quality_flag"] == "out_of_range"

    # ---- 2. 序号重启：boot 2 的 seq 0..4 是新样本，不算重复
    scenario_binary_restart(c)
    s = samples_by_key(c)
    for seq in range(5):
        row = s[("PRB-2002", "2", seq)]
        almost(row["std_c"], 4.8)
        assert row["vehicle_id"] == "VH-02"
    assert s[("PRB-2002", "2", 0)]["sample_time"].startswith("2026-11-15T13:00:00")
    # 缺锚点：同一启动批次没有可信锚点 → 待归属（缺锚）
    assert s[("PRB-3003", "7", 10)]["status"] == "pending_anchor"
    assert s[("PRB-3003", "7", 10)]["sample_time"] is None

    # ---- 3. JSON：明确时区、摄氏小数、重发去重、载荷冲突
    out = scenario_json_basic(c)
    assert out["statuses"] == ["parsed_new", "parsed_new", "parsed_new",
                               "parsed_dup", "conflict"]
    s = samples_by_key(c)
    almost(s[("PRB-1001", "B7", 1)]["std_c"], 4.5)
    # +08:00 时区被正确换算为 UTC 10:10
    assert s[("PRB-1001", "B7", 2)]["sample_time"].startswith("2026-11-15T10:10:00")
    almost(s[("PRB-1001", "B7", 3)]["std_c"], 5.25)
    # 冲突不覆盖原值
    detail = c.get(f"/api/samples/{s[('PRB-1001', 'B7', 1)]['id']}")
    almost(detail["sample"]["raw_value"], 4.5)
    assert len(detail["conflicts"]) == 1
    assert detail["conflicts"][0]["incoming_fields"]["raw_fields"]["c"] == 9.9

    # ---- 4. 新固件无契约：保留待解析，不猜单位
    scenario_json_new_firmware(c)
    b5 = batch_by_note(c, "固件 3.0.0")
    assert b5["pending_contract"] == 2
    assert ("PRB-1001", "B7", 4) not in samples_by_key(c)

    # ---- 5. 契约修订：只重放依赖该契约的信封，校验通过后切换
    rev = scenario_contract_revision(c)
    assert rev["run"]["status"] == "committed", rev
    assert rev["run"]["stats"]["parsed_new"] == 2
    s = samples_by_key(c)
    # 华氏度换算：41°F = 5°C，30.2°F = −1°C；资产号 A-1001 → PRB-1001
    almost(s[("PRB-1001", "B7", 4)]["std_c"], 5.0)
    almost(s[("PRB-1001", "B7", 5)]["std_c"], -1.0)
    assert s[("PRB-1001", "B7", 4)]["vehicle_id"] == "VH-01"
    # 换算依据保留：原值 41.0°F 与契约修订号都可查
    d4 = c.get(f"/api/samples/{s[('PRB-1001', 'B7', 4)]['id']}")
    assert d4["sample"]["raw_value"] == 41.0 and d4["sample"]["raw_unit"] == "F"
    assert d4["current"]["contract_revision"] == 2
    steps = [st["step"] for st in d4["current"]["conversion"]["steps"]]
    assert steps[:2] == ["raw", "unit"] and "validate" in steps
    # 重放后没有待解析
    assert c.get("/api/overview")["pending_contract"] == 0

    # ---- 6. 样本总数与时间范围
    all_samples = c.get("/api/samples")["samples"]
    assert len(all_samples) == 17          # 5 + 5 + 2(缺锚) + 3 + 2(新固件)
    timed = [x for x in all_samples if x["sample_time"]]
    times = sorted(x["sample_time"] for x in timed)
    assert times[0].startswith("2026-11-15T10:05:00")
    assert times[-1].startswith("2026-11-15T13:02:00")

    # ---- 7. 质量发布：只交付达到用途要求的数据
    pub_line = c.build_publication("line_monitor")
    pub_audit = c.build_publication("batch_audit")
    assert pub_line["item_count"] == 14    # 排除超量程 1 + 缺锚 2
    assert pub_audit["item_count"] == 13   # 再排除换车边界待归属 1
    line_items = c.get(f"/api/publications/{pub_line['id']}/items")["items"]
    audit_items = c.get(f"/api/publications/{pub_audit['id']}/items")["items"]
    line_ids = {i["sample_id"] for i in line_items}
    audit_ids = {i["sample_id"] for i in audit_items}
    oor_id = s[("PRB-2002", "1", 200)]["id"]
    edge_id = s[("PRB-2002", "1", 340)]["id"]
    anchor_id = s[("PRB-3003", "7", 10)]["id"]
    assert oor_id not in line_ids and oor_id not in audit_ids      # 超量程不交付
    assert edge_id in line_ids and edge_id not in audit_ids        # 边界：监控可见、审计不交
    assert anchor_id not in line_ids                               # 缺锚不交付
    assert all(i["cargo_batch_id"] for i in audit_items)           # 审计必须完整归属

    # ---- 8. 消费端：按游标读完整版本，未确认重发，确认才前进
    cons = c.create_consumer("audit-app", "batch_audit")
    cid = cons["id"]
    p1 = c.pull(cid, 5)
    assert p1["version"] == 1 and p1["from"] == 0 and p1["to"] == 4
    p1_again = c.pull(cid, 5)                                       # 未确认重拉
    assert p1_again["redelivered"] is True
    assert [i["sample_id"] for i in p1_again["items"]] == \
           [i["sample_id"] for i in p1["items"]]                    # 重复拉取无重复样本
    c.ack(cid, p1["version"], p1["from"], p1["to"])
    p2 = c.pull(cid, 5)
    assert p2["from"] == 5 and p2["to"] == 9                        # 确认后才前进
    c.ack(cid, p2["version"], p2["from"], p2["to"])
    p3 = c.pull(cid, 5)
    assert p3["from"] == 10 and p3["to"] == 12 and p3["has_more"] is False
    c.ack(cid, p3["version"], p3["from"], p3["to"])
    p4 = c.pull(cid, 5)
    assert p4["items"] == []                                        # 读完
    received = [i["sample_id"] for p in (p1, p2, p3) for i in p["items"]]
    assert len(received) == len(set(received)) == 13
    assert set(received) == audit_ids                               # 与发布内容一致
    recs = c.get(f"/api/consumption-records?consumer_id={cid}")["records"]
    assert len(recs) == 3 and sum(r["item_count"] for r in recs) == 13

    # ---- 9. 批次曲线数据可用
    curve = c.get("/api/curves?cargo_batch_id=LOT-A")
    assert curve["series"], "LOT-A 应有曲线"
    points = [p for pts in curve["series"].values() for p in pts]
    assert all(p["cargo_batch_id"] == "LOT-A" for p in points)
    assert any(p["std_c"] == 4.8 for p in points)


# ---------------------------------------------------------------- 重放提交守卫

def test_replay_commit_guards(client):
    """重放期间安装关系被修订 / 收到新信封 → 提交拒绝。"""
    c = client
    # 基于主流程已提交的状态，再造一个契约修订并手工构造“过期运行”
    created = c.create_contract("json-temp", "json", JSON_TEMP_R2["match"],
                                JSON_TEMP_R2, note="守卫测试用修订")
    c.activate_contract(created["id"])

    os.environ.setdefault("DATABASE_URL",
                          "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/coldchain_test")
    from app.db import SessionLocal
    from app.models import (
        ParsedMessage, ProtocolContract, RawEnvelope, Sample, StandardRun,
    )
    from app.pipeline import get_counter, standardize_sample
    from app.replay import ReplayAbort, _validate
    from sqlalchemy import func

    db = SessionLocal()
    try:
        contract = db.get(ProtocolContract, created["id"])
        max_env = db.query(func.max(RawEnvelope.id)).scalar() or 0
        sample = db.query(Sample).filter_by(device_id="PRB-1001", boot_batch="B7",
                                            sample_seq=1).one()
        pm = db.get(ParsedMessage, sample.first_parsed_id)
        norm = {"device_id": sample.device_id, "boot_batch": sample.boot_batch,
                "sample_seq": sample.sample_seq, "raw_value": sample.raw_value,
                "raw_unit": sample.raw_unit, "fw": (pm.fields or {}).get("fw"),
                "rule_key": "default", "raw_fields": sample.raw_fields}

        def make_run(rel_rev, env_id):
            run = StandardRun(kind="replay", contract_key="json-temp", to_revision=3,
                              started_relation_revision=rel_rev,
                              started_envelope_id=env_id)
            db.add(run)
            db.flush()
            # 暂存一条真实可复算的结果，保证守卫按顺序走到被测那一关
            standardize_sample(db, sample, norm, contract,
                               source="replay", run=run, commit=False)
            db.flush()
            return run

        # 情形 1：运行期间安装关系被修订（started_relation_revision 落后）
        run1 = make_run(get_counter(db, "relation_revision") - 1, max_env)
        with pytest.raises(ReplayAbort, match="安装关系"):
            _validate(db, run1, contract)
        db.rollback()

        # 情形 2：运行期间收到新信封（可能是新固件）
        run2 = make_run(get_counter(db, "relation_revision"), max_env - 1)
        with pytest.raises(ReplayAbort, match="信封"):
            _validate(db, run2, contract)
        db.rollback()
    finally:
        db.close()
