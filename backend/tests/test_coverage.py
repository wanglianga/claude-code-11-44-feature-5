"""分区覆盖分析端到端验收。

用手算可核对的矩形样本验证：空间并集（交叠不重复计量）、冲突交集、
门侧未覆盖、托盘混批、隔板变动、探头迁移、布局时刻不确定、同域读数冲突、
缺时间窗口读数、批量撤回验证、补证获得资格、占用冲突不计入分母、
重复导入不复制货物空间、重算稳定不重复计量、运行守卫（计算期间输入变化）。

测试按顺序在同一服务进程上执行（与 test_acceptance 同一风格）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sender"))

from scenarios import run_all  # noqa: E402

pytestmark = pytest.mark.usefixtures("server")

WIN = ("2026-11-15T09:00:00+00:00", "2026-11-15T15:00:00+00:00")
RUNS = {}   # 顺序执行：各测试产出的运行 id


# ---------------------------------------------------------------- 工具

def windows(stats, batch):
    """{窗口开始 HH:MM: 窗口统计}"""
    return {w["start"][11:16]: w for w in stats["batches"][batch]["windows"]}


def cats(w):
    return {k: v for k, v in w["categories"].items() if v}


def run_demo_window(c):
    out = c._req("POST", "/api/coverage/runs",
                 {"vehicle_id": "VH-01", "window_start": WIN[0], "window_end": WIN[1]})
    return out


def cells_of(c, run_id, batch, hhmm, category=None):
    q = f"/api/coverage/runs/{run_id}/cells?cargo_batch_id={batch}"
    cells = c.get(q)["cells"]
    out = [x for x in cells if x["window_start"][11:16] == hhmm]
    if category:
        out = [x for x in out if x["category"] == category]
    return out


# ---------------------------------------------------------------- 1. 演示流程与手算核对

def test_01_demo_flow_hand_calculated(client):
    c = client
    c.reset()
    c.seed()
    run_all(c)                       # 基础读数（两类协议全部场景）
    demo = c.coverage_demo()
    assert demo["run_status"] == "current", demo
    run_id = demo["run_id"]
    RUNS["demo"] = run_id
    detail = c.get(f"/api/coverage/runs/{run_id}")
    stats = detail["run"]["stats"]

    a = windows(stats, "LOT-A")
    # 09:00 窗口：有资格但窗口内无读数（不能用 10:05 的读数回填本窗口）
    assert cats(a["09:00"]) == {"no_reading": 6.0}
    # 10:00：P1/P2 由 PRB-1001 支持（含 -1.0°C 超限读数→exceed），
    # P3[2,3] 上 PRB-1001(exceed) 与 PRB-2002(normal 4.8) 冲突
    assert cats(a["10:00"]) == {"conflict": 2.0, "supported": 4.0}
    assert a["10:00"]["supported_verdicts"] == {"normal": 0.0, "exceed": 4.0}
    # 11:00：P3 上 4.6°C 与 9.6125°C 冲突；P1/P2 正常
    assert cats(a["11:00"]) == {"conflict": 2.0, "supported": 4.0}
    assert a["11:00"]["supported_verdicts"] == {"normal": 4.0, "exceed": 0.0}
    # 12:00：P9 占用冲突切走 P3 的 [2,2.5]（1.0 m²h 不计入分母）
    assert cats(a["12:00"]) == {"supported": 5.0}
    assert a["12:00"]["occupancy_conflict"] == 1.0
    assert a["12:00"]["cargo_area_time"] == 5.0
    # 13:00/14:00：隔板移至 4.0，VAL-005（D1@4.0）接替
    assert cats(a["13:00"]) == {"supported": 5.0}
    assert cats(a["14:00"]) == {"supported": 5.0}

    b = windows(stats, "LOT-B")
    # 门侧 P4[5,6] 无任何验证 → 未确认
    assert cats(b["09:00"]) == {"no_reading": 4.0, "no_qualification": 2.0}
    assert cats(b["10:00"]) == {"conflict": 2.0, "supported": 2.0,
                                "no_qualification": 2.0}
    # 11:00：P5 移动时刻只有范围 [11:00,11:20] → 布局待核 2×(1/3)h；
    # 确定部分 [1,2]×[11:20,12:00] 有支持 2×(2/3)h
    w11 = b["11:00"]
    assert cats(w11) == {"conflict": 2.0, "supported": 1.333333,
                         "layout_pending": 1.333333, "no_qualification": 2.0}
    # 12:00：占用冲突 [1.5,2]+[2,2.5] 共 2.0 m²h 不计入分母（两份货量不同时入分母）
    assert cats(b["12:00"]) == {"supported": 2.0, "no_qualification": 2.0}
    assert b["12:00"]["occupancy_conflict"] == 2.0
    assert b["12:00"]["cargo_area_time"] == 4.0
    assert cats(b["13:00"]) == {"supported": 2.0, "no_qualification": 2.0}

    # 不变式：类别互斥、总和不超过完整货物区域（比例 ≤ 1）
    for batch in ("LOT-A", "LOT-B"):
        for w in windows(stats, batch).values():
            total = sum(w["categories"].values())
            assert abs(total - w["cargo_area_time"]) < 3e-6
            for r in w["ratios"].values():
                assert r <= 1.0 + 1e-6
            assert abs(sum(w["ratios"].values()) - 1.0) < 3e-6


# ---------------------------------------------------------------- 2. 分片细节：冲突读数/待核/混批/占用

def test_02_cells_detail(client):
    c = client
    run_id = RUNS["demo"]
    # 同域读数冲突：显示冲突区域与两个物理读数，不平均
    # （窗口 11:00 被 P5 不确定移动切成两个布局区间，P3 冲突片在两个区间各出现一次）
    conflict = cells_of(c, run_id, "LOT-A", "11:00", "conflict")
    assert len(conflict) == 2
    for cell in conflict:
        assert cell["pallet_id"] == "P3" and cell["rect"]["x"] == 2.0
        probes = {p["probe_id"]: p for p in cell["probes"]}
        assert probes["PRB-1001"]["verdict"] == "normal"
        assert probes["PRB-1001"]["temps"] == [4.6]
        assert probes["PRB-2002"]["verdict"] == "exceed"
        assert abs(probes["PRB-2002"]["temps"][0] - 9.6125) < 1e-6
        # 混批托盘：只给托盘共同覆盖，不按件数虚构批内位置
        assert cell["mixed_pallet"] is True
        assert "mixed_pallet_common_coverage" in cell["reasons"]

    # 布局待核：P5 移动时刻只有范围，[1,2] 与 [4,5] 两块在各可行布局不一致
    pending = cells_of(c, run_id, "LOT-B", "11:00", "layout_pending")
    assert len(pending) == 2
    xs = sorted(p["rect"]["x"] for p in pending)
    assert xs == [1.0, 4.0]
    for p in pending:
        assert p["interval_start"][11:16] == "11:00"
        assert p["interval_end"][11:16] == "11:20"
        cats_seen = {v["category"] for v in p["layout_variants"]}
        assert "not_cargo" in cats_seen and "supported" in cats_seen

    # 占用冲突单独标记、不计入分母
    occ_a = [x for x in cells_of(c, run_id, "LOT-A", "12:00") if x["occupancy_conflict"]]
    assert len(occ_a) == 1 and occ_a[0]["rect"]["x"] == 2.0 and occ_a[0]["rect"]["w"] == 0.5
    assert "MF-02/P9" in occ_a[0]["conflict_with"]
    occ_b = [x for x in cells_of(c, run_id, "LOT-B", "12:00") if x["occupancy_conflict"]]
    assert sorted((x["rect"]["x"], x["rect"]["w"]) for x in occ_b) == [(1.5, 0.5), (2.0, 0.5)]

    # 缺验证时展示探头实际测量点（点观测不等同整块区域）
    unc = c.get(f"/api/coverage/runs/{run_id}/unconfirmed")
    assert unc["unconfirmed_cells"], "应有未确认分片"
    pts = {(p["probe_id"], tuple(p["point"])) for p in unc["unvalidated_probe_points"]}
    assert ("PRB-1001", (0.5, 1.0)) in pts or ("PRB-2002", (2.5, 1.0)) in pts


# ---------------------------------------------------------------- 3. 重复导入不复制货物空间

def test_03_duplicate_import_no_duplication(client):
    c = client
    before = c.get(f"/api/coverage/runs/{RUNS['demo']}")["run"]["stats"]
    demo2 = c.coverage_demo()          # 再次演示：全部幂等
    imp = demo2["import"]["results"]
    for group in imp.values():
        assert all(i["status"] == "duplicate" for i in group), imp
    after = c.get(f"/api/coverage/runs/{demo2['run_id']}")["run"]["stats"]
    assert before == after, "重复导入/重算不得改变覆盖结果（不复制空间、不重复计量）"

    # 同键不同载荷 → 冲突并保留原值，覆盖结果不变
    bad = dict(next(p for p in _demo_placement() if p["placement_key"] == "PL-P1"))
    bad["batches"] = [{"batch_id": "LOT-A", "pieces": 999}]
    out = c.coverage_import({"placements": [bad]})
    assert out["results"]["placements"][0]["status"] == "conflict"
    conflicts = c.get("/api/coverage/import-conflicts")["conflicts"]
    assert any(x["natural_key"] == "PL-P1" for x in conflicts)
    again = run_demo_window(c)
    assert c.get(f"/api/coverage/runs/{again['run_id']}")["run"]["stats"] == before


def _demo_placement():
    return [{
        "placement_key": "PL-P1", "manifest_id": "MF-01", "vehicle_id": "VH-01",
        "pallet_id": "P1", "batches": [{"batch_id": "LOT-A", "pieces": 100}],
        "rect": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L1",
        "from_lo": "2026-11-15T09:00:00+00:00", "to_lo": "2026-11-15T15:00:00+00:00",
    }]


# ---------------------------------------------------------------- 4. 批量撤回验证与影响

def test_04_withdrawal_impact_and_compare(client):
    c = client
    out = c.coverage_withdraw_group("CAL-B", "2026-11-15T10:30:00+00:00",
                                    "校准批次 CAL-B 复核不合格")
    assert sorted(out["withdrawn"]) == ["VAL-002", "VAL-003"]
    impacts = {i["validation_key"]: i["impact"] for i in out["impacts"]}
    # 影响按实际引用追到货物与分析时段
    v2 = impacts["VAL-002"]
    assert RUNS["demo"] in [r["run_id"] for r in v2["affected_runs"]]
    assert {(a["cargo_batch_id"], a["window_start"][11:16]) for a in v2["affected"]} == \
           {("LOT-A", "10:00"), ("LOT-A", "11:00"), ("LOT-B", "10:00"), ("LOT-B", "11:00")}
    v3 = impacts["VAL-003"]
    assert ("LOT-A", "12:00") in {(a["cargo_batch_id"], a["window_start"][11:16])
                                  for a in v3["affected"]}

    run2 = run_demo_window(c)
    RUNS["withdrawn"] = run2["run_id"]
    assert run2["status"] == "current"
    stats = c.get(f"/api/coverage/runs/{run2['run_id']}")["run"]["stats"]
    a = windows(stats, "LOT-A")
    # 撤回 10:30 生效：P3 冲突只剩前半小时，P2 后半小时失去资格
    assert cats(a["10:00"]) == {"conflict": 1.0, "supported": 4.0,
                                "no_qualification": 1.0}
    assert cats(a["11:00"]) == {"supported": 4.0, "no_qualification": 2.0}
    assert cats(a["12:00"]) == {"supported": 3.0, "no_qualification": 2.0}
    b = windows(stats, "LOT-B")
    assert cats(b["10:00"]) == {"conflict": 1.0, "supported": 2.0,
                                "no_qualification": 3.0}
    assert cats(b["11:00"]) == {"supported": 3.333333, "layout_pending": 1.333333,
                                "no_qualification": 2.0}

    # 更正前后比较：类别变化的分片可列
    cmp = c.get(f"/api/coverage/compare?run_a={RUNS['demo']}&run_b={RUNS['withdrawn']}")
    assert cmp["changed_cells_total"] > 0
    chg = {(x["cell"]["cargo_batch_id"], x["cell"]["pallet_id"],
            x["cell"]["window_start"][11:16], x["from"], x["to"])
           for x in cmp["changed_cells"]}
    assert ("LOT-A", "P2", "11:00", "supported", "no_qualification") in chg
    assert ("LOT-A", "P3", "11:00", "conflict", "supported") in chg


# ---------------------------------------------------------------- 5. 补证获得资格

def test_05_supplementary_validation(client):
    c = client
    val004 = {
        "validation_key": "VAL-004", "vehicle_id": "VH-01", "structure": "冷藏厢-6m-A",
        "probe_id": "PRB-1001", "install_point": {"x": 0.5, "y": 1.0, "layer": "L1"},
        "install_condition": "内侧前壁固定支架，补充验证门侧",
        "range_rect": {"x": 5.0, "y": 0.0, "w": 1.0, "h": 2.0}, "range_layer": "L1",
        "partition_config": [{"partition_id": "D1", "x": 3.0}],
        "condition_group": "CAL-C",
        "valid_from": "2026-11-15T12:00:00+00:00", "valid_to": "2026-11-15T13:00:00+00:00",
    }
    out = c.coverage_import({"validations": [val004]})
    assert out["results"]["validations"][0]["status"] == "imported"
    # 补证影响：原本未确认、补证后才获得资格的记录随导入返回
    qi = out["results"]["validations"][0]["qualification_impact"]
    assert qi and any("LOT-B" in q["batches"] for q in qi)
    run3 = run_demo_window(c)
    RUNS["supplement"] = run3["run_id"]
    stats = c.get(f"/api/coverage/runs/{run3['run_id']}")["run"]["stats"]
    b = windows(stats, "LOT-B")
    # 门侧 P4 补证后 12:00–13:00 获得资格并有读数 → 有观测支持
    assert cats(b["12:00"]) == {"supported": 4.0}
    # 13:00 起补证失效（valid_to 13:00），P4 再次未确认
    assert cats(b["13:00"]) == {"supported": 2.0, "no_qualification": 2.0}
    # 影响查询包含原本未确认、补证后才获得资格的记录
    imp = c.get("/api/coverage/impact?kind=validation&key=VAL-004&vehicle_id=VH-01")
    assert imp["affected_runs"], "补证应能追到受影响的运行"
    cmp = c.get(f"/api/coverage/compare?run_a={RUNS['withdrawn']}&run_b={RUNS['supplement']}")
    assert cmp["changed_cells_total"] == 1
    x = cmp["changed_cells"][0]
    assert x["cell"]["pallet_id"] == "P4"
    assert (x["from"], x["to"]) == ("no_qualification", "supported")


# ---------------------------------------------------------------- 6. 清单更正 / 布局修订 / 导出依据

def test_06_corrections_and_exports(client):
    c = client
    # 清单更正：P4 宽度 1.0 → 0.8；影响追到引用过 PL-P4 的运行与导出
    exp = c._req("POST", f"/api/coverage/runs/{RUNS['supplement']}/export",
                 {"label": "补证后基线"})
    corr = c._req("POST", "/api/coverage/placements/PL-P4/correct", {
        "manifest_id": "MF-01", "vehicle_id": "VH-01", "pallet_id": "P4",
        "batches": [{"batch_id": "LOT-B", "pieces": 70}],
        "rect": {"x": 5.0, "y": 0.0, "w": 0.8, "h": 2.0}, "layer": "L1",
        "from_lo": "2026-11-15T09:00:00+00:00", "to_lo": "2026-11-15T15:00:00+00:00"})
    assert corr["version"] == 2
    assert {r["run_id"] for r in corr["impact"]["affected_runs"]} >= \
           {RUNS["demo"], RUNS["withdrawn"], RUNS["supplement"]}
    assert exp["id"] in corr["impact"]["affected_exports"]
    # 导出保留依据，能查到导出后的更正范围
    detail = c.get(f"/api/coverage/exports/{exp['id']}")
    assert any(x["kind"] == "placement_correct" and x["entity_key"] == "PL-P4"
               for x in detail["subsequent_corrections"])
    # 导出快照仍是更正前依据（LOT-B 12:00 supported 4.0）
    exp_stats = detail["payload"]["run"]["stats"]
    assert cats(windows(exp_stats, "LOT-B")["12:00"]) == {"supported": 4.0}

    # 布局修订：旧布局运行的影响可查
    lay = c.get("/api/coverage/inputs?vehicle_id=VH-01")
    new_layout = dict(lay["layouts"][-1])
    new_layout = {k: new_layout[k] for k in
                  ("coord", "length_m", "width_m", "layers", "zones")}
    new_layout["vehicle_id"] = "VH-01"
    new_layout["zones"] = list(new_layout["zones"]) + [
        {"zone_id": "Z2", "name": "门侧缓冲区",
         "rect": {"x": 5.0, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L1"}]
    out = c.coverage_import({"layouts": [new_layout]})
    assert out["results"]["layouts"][0]["status"] == "imported"
    assert out["results"]["layouts"][0]["revision"] == 2
    imp = c.get("/api/coverage/impact?kind=layout&vehicle_id=VH-01")
    assert imp["current_layout_revision"] == 2
    assert {r["run_id"] for r in imp["affected_runs"]} >= \
           {RUNS["demo"], RUNS["withdrawn"], RUNS["supplement"]}
    # 旧运行保留所用布局修订（过期结果不能直接替换当前分析）
    runs = {r["id"]: r for r in c.get("/api/coverage/runs?vehicle_id=VH-01")["runs"]}
    assert runs[RUNS["demo"]]["used"]["layout_revision"] == 1
    assert runs[RUNS["demo"]]["inputs_stale"] is True


# ---------------------------------------------------------------- 7. 运行守卫：计算期间输入变化

def test_07_run_guard_stale_inputs(client):
    c = client
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/coldchain_test")
    from datetime import datetime, timezone

    from app import coverage as COV
    from app.db import SessionLocal

    T = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        revs = COV.input_revisions(db)
        stale = dict(revs, validation_revision=revs["validation_revision"] - 1)
        run = COV.run_coverage(db, "VH-01", None,
                               T("2026-11-15T09:00:00"), T("2026-11-15T15:00:00"),
                               start_revisions=stale)
        assert run.status == "stale_inputs"
        assert "不能直接替换当前分析" in run.note
        stale_id = run.id
    finally:
        db.close()
    runs = c.get("/api/coverage/runs?vehicle_id=VH-01")["runs"]
    cur = [r for r in runs if r["status"] == "current"]
    assert cur and all(r["id"] != stale_id for r in cur), "过期结果不能成为当前分析"


# ---------------------------------------------------------------- 8. 重算稳定

def test_08_recalculation_stable(client):
    c = client
    r1 = run_demo_window(c)
    s1 = c.get(f"/api/coverage/runs/{r1['run_id']}")["run"]["stats"]
    r2 = run_demo_window(c)
    s2 = c.get(f"/api/coverage/runs/{r2['run_id']}")["run"]["stats"]
    assert s1 == s2, "大批量重算不得重复计量或把未确认区域变成正常"
    b = windows(s2, "LOT-B")
    # P4 已更正为宽 0.8（1.6 m²），未确认区域保持未确认
    assert cats(b["09:00"]) == {"no_reading": 4.0, "no_qualification": 1.6}


# ---------------------------------------------------------------- 9. 手算矩形样本：空间并集与冲突交集

def test_09_union_and_conflict_hand_calc(client):
    c = client
    c.reset()
    c.seed()
    # 探头安装关系（先关系后读数，保证归属）
    c._req("POST", "/api/relations", {
        "probe_id": "PRB-T1", "vehicle_id": "VH-T1", "box_id": "BX-T1",
        "cargo_batch_id": "LOT-T", "valid_from": "2026-11-16T10:00:00+00:00",
        "valid_to": "2026-11-16T12:00:00+00:00"})
    c._req("POST", "/api/relations", {
        "probe_id": "PRB-T2", "vehicle_id": "VH-T1", "box_id": "BX-T1",
        "cargo_batch_id": "LOT-T", "valid_from": "2026-11-16T10:00:00+00:00",
        "valid_to": "2026-11-16T12:00:00+00:00"})
    b = c.create_batch("sender-local-01", "json", "并集/冲突手算样本读数")
    msgs = [
        ("PRB-T1", 1, "2026-11-16T10:10:00+00:00", 5.0),
        ("PRB-T1", 2, "2026-11-16T10:20:00+00:00", 5.0),
        ("PRB-T1", 3, "2026-11-16T11:10:00+00:00", 5.0),
        ("PRB-T2", 1, "2026-11-16T10:10:00+00:00", 5.0),
        ("PRB-T2", 2, "2026-11-16T10:20:00+00:00", 9.0),   # 超 LOT-T 上限 8
        ("PRB-T2", 3, "2026-11-16T11:10:00+00:00", 5.0),
    ]
    for dev, seq, ts, temp in msgs:
        c.send_json(b, "json-t", {"type": "temp", "fw": "2.3.1", "dev": dev,
                                  "boot": "BT1", "seq": seq, "evt": f"E-{dev}-{seq}",
                                  "ts": ts, "c": temp})
    c.close_batch(b)

    bundle = {
        "layouts": [{"vehicle_id": "VH-T1",
                     "coord": {"origin": "最内侧左角", "x_axis": "向门",
                               "y_axis": "向右", "unit": "m"},
                     "length_m": 4.0, "width_m": 2.0, "layers": ["L1"],
                     "zones": [{"zone_id": "Z1", "name": "舱",
                                "rect": {"x": 0, "y": 0, "w": 4.0, "h": 2.0},
                                "layer": "L1"}]}],
        "batch_specs": [{"cargo_batch_id": "LOT-T", "temp_min_c": 2.0,
                         "temp_max_c": 8.0, "window_minutes": 60, "min_readings": 1}],
        "probes": [
            {"install_key": "PI-T1", "probe_id": "PRB-T1", "vehicle_id": "VH-T1",
             "x": 0.5, "y": 1.0, "layer": "L1",
             "installed_from": "2026-11-16T10:00:00+00:00",
             "installed_to": "2026-11-16T12:00:00+00:00"},
            {"install_key": "PI-T2", "probe_id": "PRB-T2", "vehicle_id": "VH-T1",
             "x": 1.5, "y": 1.0, "layer": "L1",
             "installed_from": "2026-11-16T10:00:00+00:00",
             "installed_to": "2026-11-16T12:00:00+00:00"},
        ],
        "validations": [
            {"validation_key": "V-T1", "vehicle_id": "VH-T1", "structure": "试验厢",
             "probe_id": "PRB-T1", "install_point": {"x": 0.5, "y": 1.0, "layer": "L1"},
             "range_rect": {"x": 0.0, "y": 0.0, "w": 1.5, "h": 2.0}, "range_layer": "L1",
             "partition_config": [], "condition_group": "CAL-T",
             "valid_from": "2026-11-16T10:00:00+00:00",
             "valid_to": "2026-11-16T12:00:00+00:00"},
            {"validation_key": "V-T2", "vehicle_id": "VH-T1", "structure": "试验厢",
             "probe_id": "PRB-T2", "install_point": {"x": 1.5, "y": 1.0, "layer": "L1"},
             "range_rect": {"x": 1.0, "y": 0.0, "w": 1.5, "h": 2.0}, "range_layer": "L1",
             "partition_config": [], "condition_group": "CAL-T",
             "valid_from": "2026-11-16T10:00:00+00:00",
             "valid_to": "2026-11-16T12:00:00+00:00"},
        ],
        "placements": [
            {"placement_key": "PL-PA", "manifest_id": "MF-T1", "vehicle_id": "VH-T1",
             "pallet_id": "PA", "batches": [{"batch_id": "LOT-T", "pieces": 10}],
             "rect": {"x": 0.0, "y": 0.0, "w": 2.0, "h": 2.0}, "layer": "L1",
             "from_lo": "2026-11-16T10:00:00+00:00",
             "to_lo": "2026-11-16T12:00:00+00:00"},
        ],
    }
    out = c.coverage_import(bundle)
    assert all(i["status"] == "imported" for g in out["results"].values() for i in g)
    run = c.coverage_run("VH-T1", "2026-11-16T10:00:00+00:00",
                         "2026-11-16T12:00:00+00:00")
    stats = c.get(f"/api/coverage/runs/{run['run_id']}")["run"]["stats"]
    w = windows(stats, "LOT-T")
    # 10:00 窗口：V-T1[0,1.5] 与 V-T2[1,2.5] 交叠 [1,1.5]（1.0 m²）；
    # 交集内一超限一正常 → 冲突 1.0；其余 3.0 有支持；总和恰为托盘 4.0
    assert cats(w["10:00"]) == {"conflict": 1.0, "supported": 3.0}
    assert w["10:00"]["ratios"] == {"supported": 0.75, "conflict": 0.25,
                                    "no_reading": 0.0, "no_qualification": 0.0,
                                    "layout_pending": 0.0}
    # 11:00 窗口：两探头一致正常 → 并集 [0,2.5]∩托盘 = 4.0，交叠不重复计量
    assert cats(w["11:00"]) == {"supported": 4.0}
    assert w["11:00"]["cargo_area_time"] == 4.0
    # 一致只是多项支持：multi_support 标记，面积不增加
    sup = cells_of(c, run["run_id"], "LOT-T", "11:00", "supported")
    mid = [x for x in sup if x["rect"]["x"] == 1.0 and x["rect"]["w"] == 0.5]
    assert mid and "multi_support" in mid[0]["reasons"]


# ---------------------------------------------------------------- 10. 货层与车辆隔离

def test_10_layer_and_vehicle_isolation(client):
    c = client
    # VH-T2：L1/L2 两层；PRB-T1 下午装到 VH-T2（同车型，验证不混用）
    c._req("POST", "/api/relations", {
        "probe_id": "PRB-T1", "vehicle_id": "VH-T2", "box_id": "BX-T2",
        "cargo_batch_id": "LOT-T2", "valid_from": "2026-11-16T13:00:00+00:00",
        "valid_to": "2026-11-16T15:00:00+00:00"})
    b = c.create_batch("sender-local-01", "json", "隔离性样本读数")
    c.send_json(b, "json-t2", {"type": "temp", "fw": "2.3.1", "dev": "PRB-T1",
                               "boot": "BT2", "seq": 1, "evt": "E-T2-1",
                               "ts": "2026-11-16T13:10:00+00:00", "c": 5.0})
    c.close_batch(b)
    bundle = {
        "layouts": [{"vehicle_id": "VH-T2",
                     "coord": {"origin": "最内侧左角", "x_axis": "向门",
                               "y_axis": "向右", "unit": "m"},
                     "length_m": 4.0, "width_m": 2.0, "layers": ["L1", "L2"],
                     "zones": [{"zone_id": "Z1", "name": "下层",
                                "rect": {"x": 0, "y": 0, "w": 4.0, "h": 2.0},
                                "layer": "L1"},
                               {"zone_id": "Z2", "name": "上层",
                                "rect": {"x": 0, "y": 0, "w": 4.0, "h": 2.0},
                                "layer": "L2"}]}],
        "batch_specs": [{"cargo_batch_id": "LOT-T2", "temp_min_c": 2.0,
                         "temp_max_c": 8.0, "window_minutes": 60, "min_readings": 1}],
        "probes": [
            {"install_key": "PI-T2B", "probe_id": "PRB-T1", "vehicle_id": "VH-T2",
             "x": 0.5, "y": 1.0, "layer": "L1",
             "installed_from": "2026-11-16T13:00:00+00:00",
             "installed_to": "2026-11-16T15:00:00+00:00"},
        ],
        "placements": [
            {"placement_key": "PL-PB1", "manifest_id": "MF-T2", "vehicle_id": "VH-T2",
             "pallet_id": "PB1", "batches": [{"batch_id": "LOT-T2", "pieces": 10}],
             "rect": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L1",
             "from_lo": "2026-11-16T13:00:00+00:00",
             "to_lo": "2026-11-16T15:00:00+00:00"},
            {"placement_key": "PL-PB2", "manifest_id": "MF-T2", "vehicle_id": "VH-T2",
             "pallet_id": "PB2", "batches": [{"batch_id": "LOT-T2", "pieces": 10}],
             "rect": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 2.0}, "layer": "L2",
             "from_lo": "2026-11-16T13:00:00+00:00",
             "to_lo": "2026-11-16T15:00:00+00:00"},
        ],
    }
    c.coverage_import(bundle)
    run = c.coverage_run("VH-T2", "2026-11-16T13:00:00+00:00",
                         "2026-11-16T15:00:00+00:00")
    stats = c.get(f"/api/coverage/runs/{run['run_id']}")["run"]["stats"]
    w = windows(stats, "LOT-T2")
    # V-T1 是 VH-T1 的验证：同车型另一辆车不能混用 → 两层都未确认
    assert cats(w["13:00"]) == {"no_qualification": 4.0}
    # L1 有探头在测但无验证 → 展示实际测量点；L2 无探头
    cells = cells_of(c, run["run_id"], "LOT-T2", "13:00", "no_qualification")
    by_layer = {x["layer"]: x for x in cells}
    assert "unvalidated_probe_present" in by_layer["L1"]["reasons"]
    assert "unvalidated_probe_present" not in by_layer["L2"]["reasons"]

    # 给 VH-T2 的 L1 补验证：L1 获得支持，L2 仍未确认（货层不混用）
    c.coverage_import({"validations": [{
        "validation_key": "V-T2B", "vehicle_id": "VH-T2", "structure": "试验厢",
        "probe_id": "PRB-T1", "install_point": {"x": 0.5, "y": 1.0, "layer": "L1"},
        "range_rect": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 2.0}, "range_layer": "L1",
        "partition_config": [], "condition_group": "CAL-T2",
        "valid_from": "2026-11-16T13:00:00+00:00",
        "valid_to": "2026-11-16T15:00:00+00:00"}]})
    run2 = c.coverage_run("VH-T2", "2026-11-16T13:00:00+00:00",
                          "2026-11-16T15:00:00+00:00")
    stats2 = c.get(f"/api/coverage/runs/{run2['run_id']}")["run"]["stats"]
    w2 = windows(stats2, "LOT-T2")
    assert cats(w2["13:00"]) == {"supported": 2.0, "no_qualification": 2.0}
