"""数据模型。

分层：
  原始接入层  transport_batch / connection_state / raw_envelope / frame
  协议契约层  protocol_contract
  设备关系层  device_relation / clock_anchor / meta_counter
  标准化层    parsed_message / sample / sample_conflict / reading_version / current_reading
  质量发布层  publication / publication_item / consumer / consumption_record
  运行控制    standard_run（标准化/重放运行，提交前校验）
  分区覆盖层  vehicle_layout / pallet_placement / partition_event / probe_installation /
              zone_validation / batch_spec / coverage_run / coverage_cell / coverage_export /
              coverage_import_conflict
"""
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Integer,
    LargeBinary, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .db import Base


# ---------------------------------------------------------------- 原始接入层

class TransportBatch(Base):
    """传输批次：发送器一次有序发送会话。原始信封都挂在传输批次下。"""
    __tablename__ = "transport_batch"

    id = Column(Integer, primary_key=True)
    source = Column(String(128), nullable=False)          # 发送端标识，如 sender-local-01
    protocol = Column(String(16), nullable=False)         # json | binary | mixed
    note = Column(String(256), default="")
    open = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    closed_at = Column(DateTime(timezone=True))


class ConnectionState(Base):
    """一条逻辑连接的组帧状态。残片只在本连接内续接，不同连接绝不串接。

    buffer 持久化保存，服务重启不丢；offset_base 是 buffer[0] 在连接字节流中的绝对偏移。
    """
    __tablename__ = "connection_state"
    __table_args__ = (UniqueConstraint("batch_id", "conn_key", name="uq_conn_batch_key"),)

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("transport_batch.id"), nullable=False)
    conn_key = Column(String(64), nullable=False)         # 发送方给的连接标识
    protocol = Column(String(16), nullable=False, default="binary")
    buffer = Column(LargeBinary, nullable=False, default=b"")
    offset_base = Column(BigInteger, nullable=False, default=0)   # buffer[0] 的绝对流偏移
    received_bytes = Column(BigInteger, nullable=False, default=0)
    open = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    closed_at = Column(DateTime(timezone=True))


class RawEnvelope(Base):
    """原始接入库：无损保存字节、来源、接收时间、传输批次。"""
    __tablename__ = "raw_envelope"

    id = Column(BigInteger, primary_key=True)
    batch_id = Column(Integer, ForeignKey("transport_batch.id"), nullable=False)
    connection_id = Column(Integer, ForeignKey("connection_state.id"))
    kind = Column(String(16), nullable=False)             # json_msg | bin_chunk
    seq_in_conn = Column(Integer, nullable=False, default=0)
    conn_off_start = Column(BigInteger)                   # 本块在连接流中的起始绝对偏移
    conn_off_end = Column(BigInteger)                     # 结束偏移（开区间）
    payload = Column(LargeBinary, nullable=False)         # 原始字节（JSON 报文为其 UTF-8 字节）
    received_at = Column(DateTime(timezone=True), server_default=func.now())


class Frame(Base):
    """组帧结果。status: ok / bad_crc / bad_length / garbage / truncated。

    坏校验与截断分开记录；一条坏帧只跳过自己，不吞后续完整帧。
    """
    __tablename__ = "frame"

    id = Column(BigInteger, primary_key=True)
    batch_id = Column(Integer, ForeignKey("transport_batch.id"), nullable=False)
    connection_id = Column(Integer, ForeignKey("connection_state.id"), nullable=False)
    start_off = Column(BigInteger, nullable=False)        # 连接流内绝对起始偏移
    end_off = Column(BigInteger, nullable=False)          # 结束偏移（开区间）
    payload = Column(LargeBinary, nullable=False)
    status = Column(String(16), nullable=False)
    detail = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------- 协议契约层

class ProtocolContract(Base):
    """协议契约：固件适用范围、字段含义与换算规则。按 (contract_key, revision) 版本化。

    新固件没有明确契约时，报文保持 pending_contract，绝不凭字段形状猜单位。
    """
    __tablename__ = "protocol_contract"
    __table_args__ = (UniqueConstraint("contract_key", "revision", name="uq_contract_key_rev"),)

    id = Column(Integer, primary_key=True)
    contract_key = Column(String(64), nullable=False)     # 如 json-temp / bin-temp
    revision = Column(Integer, nullable=False)
    kind = Column(String(16), nullable=False)             # json | binary
    match = Column(JSONB, nullable=False)                 # 适用固件：{"fw":[...]} 或 {"versions":[...]}
    definition = Column(JSONB, nullable=False)            # 字段含义、单位、换算、时钟规则
    status = Column(String(16), nullable=False, default="draft")  # draft | active | retired
    note = Column(String(256), default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------- 设备关系层

class DeviceRelation(Base):
    """设备关系：探头→车辆/周转箱/货物批次 的安装区间（左闭右开）。"""
    __tablename__ = "device_relation"

    id = Column(Integer, primary_key=True)
    probe_id = Column(String(64), nullable=False)
    vehicle_id = Column(String(64), nullable=False)
    box_id = Column(String(64), nullable=False)
    cargo_batch_id = Column(String(64), nullable=False)
    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_to = Column(DateTime(timezone=True))            # NULL = 至今有效
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ClockAnchor(Base):
    """可信锚点：同一启动批次内，设备本地计数 ↔ 可信服务器时间 的映射点。"""
    __tablename__ = "clock_anchor"

    id = Column(Integer, primary_key=True)
    device_id = Column(String(64), nullable=False)
    boot_batch = Column(String(64), nullable=False)
    counter = Column(BigInteger, nullable=False)          # 设备本地计数（样本序号）
    server_time = Column(DateTime(timezone=True), nullable=False)
    error_s = Column(Float, nullable=False, default=2.0)  # 锚点自身误差
    source = Column(String(64), default="gateway_sync")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MetaCounter(Base):
    """全局版本计数：relation_revision 等，重放提交时做乐观并发检查。"""
    __tablename__ = "meta_counter"

    key = Column(String(64), primary_key=True)
    value = Column(BigInteger, nullable=False, default=0)


# ---------------------------------------------------------------- 标准化层

class ParsedMessage(Base):
    """一次解析结果。status: parsed_new / parsed_dup / conflict / pending_contract / error"""
    __tablename__ = "parsed_message"

    id = Column(BigInteger, primary_key=True)
    batch_id = Column(Integer, ForeignKey("transport_batch.id"), nullable=False)
    frame_id = Column(BigInteger, ForeignKey("frame.id"))
    envelope_id = Column(BigInteger, ForeignKey("raw_envelope.id"))
    contract_id = Column(Integer, ForeignKey("protocol_contract.id"))
    status = Column(String(24), nullable=False)
    device_id = Column(String(64))                        # 契约换算后的规范设备号
    boot_batch = Column(String(64))
    sample_seq = Column(BigInteger)
    fields = Column(JSONB)                                # 解析出的规范字段
    payload_hash = Column(String(64))
    error = Column(Text, default="")
    run_id = Column(Integer, ForeignKey("standard_run.id"))  # 重放产生的解析标记所属运行
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Sample(Base):
    """样本身份：幂等键 (device_id, boot_batch, sample_seq)。原值与换算依据分开保存。"""
    __tablename__ = "sample"
    __table_args__ = (UniqueConstraint("device_id", "boot_batch", "sample_seq", name="uq_sample_key"),)

    id = Column(BigInteger, primary_key=True)
    device_id = Column(String(64), nullable=False)
    boot_batch = Column(String(64), nullable=False)
    sample_seq = Column(BigInteger, nullable=False)
    raw_value = Column(Float, nullable=False)             # 原始数值（未换算）
    raw_unit = Column(String(16), nullable=False)         # 原始单位/编码，如 C / F / fixed16
    raw_fields = Column(JSONB, nullable=False)            # 全部原始字段（含固件号、事件号等）
    payload_hash = Column(String(64), nullable=False)
    first_batch_id = Column(Integer, ForeignKey("transport_batch.id"), nullable=False)
    first_parsed_id = Column(BigInteger, ForeignKey("parsed_message.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SampleConflict(Base):
    """相同幂等标识、不同载荷 → 冲突。保留原值，不允许最后到达者覆盖。"""
    __tablename__ = "sample_conflict"

    id = Column(BigInteger, primary_key=True)
    sample_id = Column(BigInteger, ForeignKey("sample.id"), nullable=False)
    incoming_parsed_id = Column(BigInteger, ForeignKey("parsed_message.id"), nullable=False)
    existing_hash = Column(String(64), nullable=False)
    incoming_hash = Column(String(64), nullable=False)
    incoming_fields = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class StandardRun(Base):
    """标准化/重放运行。重放先产出完整新结果，校验通过后一次性切换当前索引。"""
    __tablename__ = "standard_run"

    id = Column(Integer, primary_key=True)
    kind = Column(String(16), nullable=False)             # standardize | replay
    contract_key = Column(String(64))
    from_revision = Column(Integer)
    to_revision = Column(Integer)
    started_relation_revision = Column(BigInteger, nullable=False, default=0)
    started_envelope_id = Column(BigInteger, nullable=False, default=0)  # 启动时最大信封 id
    status = Column(String(16), nullable=False, default="running")  # running|committed|aborted
    stats = Column(JSONB, default=dict)
    abort_reason = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True))


class ReadingVersion(Base):
    """一次标准化结果（读数 + 时间误差 + 归属 + 换算依据）。可多次生成，当前指针唯一。"""
    __tablename__ = "reading_version"

    id = Column(BigInteger, primary_key=True)
    sample_id = Column(BigInteger, ForeignKey("sample.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("standard_run.id"))   # NULL = 接入时即时标准化
    std_c = Column(Float)                                 # 标准摄氏温度
    quality_flag = Column(String(16), nullable=False, default="ok")  # ok | out_of_range
    sample_time = Column(DateTime(timezone=True))
    time_error_s = Column(Float)
    vehicle_id = Column(String(64))
    box_id = Column(String(64))
    cargo_batch_id = Column(String(64))
    status = Column(String(24), nullable=False)           # ok | pending_anchor | pending_attribution
    status_detail = Column(Text, default="")
    contract_key = Column(String(64))
    contract_revision = Column(Integer)
    relation_revision = Column(BigInteger)
    conversion = Column(JSONB)                            # 换算过程（逐步），供界面下钻
    source = Column(String(16), nullable=False, default="ingest")   # ingest | replay
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CurrentReading(Base):
    """当前索引：每个样本最多一条当前读数。重放校验通过后原子切换。"""
    __tablename__ = "current_reading"

    sample_id = Column(BigInteger, ForeignKey("sample.id"), primary_key=True)
    reading_version_id = Column(BigInteger, ForeignKey("reading_version.id"), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------- 质量发布层

class Publication(Base):
    """发布版本：某用途一份完整、校验通过的快照。消费端按版本+游标读取。"""
    __tablename__ = "publication"
    __table_args__ = (UniqueConstraint("purpose", "version", name="uq_pub_purpose_version"),)

    id = Column(Integer, primary_key=True)
    purpose = Column(String(32), nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="current")  # current | superseded
    item_count = Column(Integer, nullable=False, default=0)
    relation_revision = Column(BigInteger, nullable=False, default=0)
    stats = Column(JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PublicationItem(Base):
    __tablename__ = "publication_item"
    __table_args__ = (UniqueConstraint("publication_id", "seq", name="uq_pubitem_seq"),)

    id = Column(BigInteger, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publication.id"), nullable=False)
    seq = Column(Integer, nullable=False)
    sample_id = Column(BigInteger, ForeignKey("sample.id"), nullable=False)
    device_id = Column(String(64), nullable=False)
    std_c = Column(Float, nullable=False)
    sample_time = Column(DateTime(timezone=True), nullable=False)
    time_error_s = Column(Float, nullable=False)
    vehicle_id = Column(String(64))
    box_id = Column(String(64))
    cargo_batch_id = Column(String(64))


class Consumer(Base):
    """模拟消费端：游标 = (cursor_version, cursor_seq)；未确认分页重发，不跳过。"""
    __tablename__ = "consumer"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False)
    purpose = Column(String(32), nullable=False)
    cursor_version = Column(Integer)                      # 正在读的发布版本
    cursor_seq = Column(Integer, nullable=False, default=0)
    pending_page = Column(JSONB)                          # {"version","from","to"} 未确认分页
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ConsumptionRecord(Base):
    """消费记录：每次确认的分页落一条，重复确认幂等。"""
    __tablename__ = "consumption_record"
    __table_args__ = (UniqueConstraint("consumer_id", "publication_id", "from_seq", name="uq_consumption_page"),)

    id = Column(BigInteger, primary_key=True)
    consumer_id = Column(Integer, ForeignKey("consumer.id"), nullable=False)
    publication_id = Column(Integer, ForeignKey("publication.id"), nullable=False)
    from_seq = Column(Integer, nullable=False)
    to_seq = Column(Integer, nullable=False)
    item_count = Column(Integer, nullable=False)
    acked_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------- 分区覆盖层
#
# 装载分区温度观测覆盖分析：输入为车厢布局（本地坐标/分区/货层）、托盘装载清单、
# 隔板位置、探头安装位置、分区验证资料（虚构）与批次温控规格；覆盖计算读取这些
# 记录与已有标准读数。所有矩形均为车厢本地坐标 {x, y, w, h}（米，轴对齐）。

class VehicleLayout(Base):
    """车厢布局版本：本地坐标系（原点/方向/单位）、厢体尺寸、货层与分区。

    布局更正产生新修订（revision+1），旧修订保留可溯；覆盖分析按 revision 引用。
    """
    __tablename__ = "vehicle_layout"
    __table_args__ = (UniqueConstraint("vehicle_id", "revision", name="uq_layout_vehicle_rev"),)

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=False)
    coord = Column(JSONB, nullable=False)       # {origin, x_axis, y_axis, unit}
    length_m = Column(Float, nullable=False)    # x 轴长度（米）
    width_m = Column(Float, nullable=False)     # y 轴宽度（米）
    layers = Column(JSONB, nullable=False)      # 货层列表，如 ["L1"]；不同货层空间隔离
    zones = Column(JSONB, nullable=False)       # [{zone_id, name, rect, layer}] 车厢分区
    status = Column(String(16), nullable=False, default="current")  # current | superseded
    note = Column(String(256), default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PalletPlacement(Base):
    """托盘装载清单记录：某托盘在 [from,to) 占用某矩形区域（某货层）。

    中途移动 = 旧记录结束 + 新记录开始；时刻不确定时用 [lo,hi] 范围表示
    （lo==hi 为精确时刻）。更正产生新版本（corrected_by 链接），旧版本保留。
    一个托盘可含多个批次（batches 数组）；没有批内可验证分区时只按托盘共同覆盖。
    """
    __tablename__ = "pallet_placement"
    __table_args__ = (UniqueConstraint("placement_key", "version", name="uq_placement_key_ver"),)

    id = Column(Integer, primary_key=True)
    placement_key = Column(String(64), nullable=False)    # 客户端自然键（幂等）
    manifest_id = Column(String(64), nullable=False)      # 装载清单号
    vehicle_id = Column(String(64), nullable=False)
    pallet_id = Column(String(64), nullable=False)
    batches = Column(JSONB, nullable=False)               # [{"batch_id","pieces"}]
    rect = Column(JSONB, nullable=False)                  # {x,y,w,h}
    layer = Column(String(32), nullable=False)
    from_lo = Column(DateTime(timezone=True), nullable=False)   # 生效时刻下界
    from_hi = Column(DateTime(timezone=True), nullable=False)   # 生效时刻上界（==from_lo 精确）
    to_lo = Column(DateTime(timezone=True))                     # 结束时刻下界（NULL=持续）
    to_hi = Column(DateTime(timezone=True))                     # 结束时刻上界
    payload_hash = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    corrected_by = Column(Integer, nullable=True)         # 更正版本 id（NULL=当前有效）
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PartitionEvent(Base):
    """隔板位置事件：隔板在有效区间内处于 x_position（沿 x 轴贯通全宽的竖隔板）。

    隔板把车厢分成若干封闭分区（enclosure），不同封闭分区的验证范围不能混用。
    """
    __tablename__ = "partition_event"

    id = Column(Integer, primary_key=True)
    event_key = Column(String(64), nullable=False, unique=True)   # 幂等键
    vehicle_id = Column(String(64), nullable=False)
    partition_id = Column(String(64), nullable=False)
    x_position = Column(Float, nullable=False)
    effective_from = Column(DateTime(timezone=True), nullable=False)
    effective_to = Column(DateTime(timezone=True))                # NULL=至今
    payload_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ProbeInstallation(Base):
    """探头安装位置：探头在车厢本地坐标下的安装点（含货层）与有效区间。"""
    __tablename__ = "probe_installation"

    id = Column(Integer, primary_key=True)
    install_key = Column(String(64), nullable=False, unique=True)  # 幂等键
    probe_id = Column(String(64), nullable=False)
    vehicle_id = Column(String(64), nullable=False)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    layer = Column(String(32), nullable=False)
    installed_from = Column(DateTime(timezone=True), nullable=False)
    installed_to = Column(DateTime(timezone=True))                 # NULL=至今
    payload_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ZoneValidation(Base):
    """分区验证资料（虚构）：某探头在指定车厢结构/隔板配置/安装条件下，
    经验证可代表的矩形空间。按 (validation_key, revision) 版本化。

    代表范围只来自本表记录，不由最近距离或温度稳定自行扩张；
    撤回（withdrawn_from 起失效）可能波及同一验证条件下的多次运输。
    """
    __tablename__ = "zone_validation"
    __table_args__ = (UniqueConstraint("validation_key", "revision", name="uq_validation_key_rev"),)

    id = Column(Integer, primary_key=True)
    validation_key = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=False)
    vehicle_id = Column(String(64), nullable=False)      # 仅适用本车（同车型他车不能混用）
    structure = Column(String(128), nullable=False)      # 适用车厢结构描述
    probe_id = Column(String(64), nullable=False)
    install_point = Column(JSONB, nullable=False)        # 验证时安装条件：{x,y,layer}
    install_condition = Column(String(256), default="")  # 安装条件文字说明
    range_rect = Column(JSONB, nullable=False)           # 已确认代表范围 {x,y,w,h}
    range_layer = Column(String(32), nullable=False)
    partition_config = Column(JSONB, nullable=False, default=list)  # 要求隔板配置 [{partition_id,x}]
    condition_group = Column(String(64), nullable=False, default="")  # 同一验证条件（批量撤回用）
    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_to = Column(DateTime(timezone=True))           # NULL=至今
    status = Column(String(16), nullable=False, default="active")   # active | superseded | withdrawn
    withdrawn_from = Column(DateTime(timezone=True))     # 撤回生效时刻（>=该时刻无资格）
    withdraw_reason = Column(String(256), default="")
    payload_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class BatchSpec(Base):
    """批次温控规格：温度上下限、统计时间窗、窗口内最少有效读数。"""
    __tablename__ = "batch_spec"

    cargo_batch_id = Column(String(64), primary_key=True)
    temp_min_c = Column(Float, nullable=False)
    temp_max_c = Column(Float, nullable=False)
    window_minutes = Column(Integer, nullable=False, default=60)
    min_readings = Column(Integer, nullable=False, default=1)  # 窗口内有效读数不足则记缺读数
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CoverageRun(Base):
    """一次覆盖分析运行：范围（车辆/批次/时段）、方法、输入指纹与结果统计。

    输入指纹（revisions + 实际引用的记录键）用于：
      - 计算期间基础数据变化 → stale_inputs，不能直接替换当前分析；
      - 影响查询按实际引用追到货物与分析时段；
      - 导出保留依据，更正范围可查。
    """
    __tablename__ = "coverage_run"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(String(64), nullable=False)
    cargo_batch_id = Column(String(64))                  # NULL=车上全部批次
    window_start = Column(DateTime(timezone=True), nullable=False)
    window_end = Column(DateTime(timezone=True), nullable=False)
    method = Column(String(16), nullable=False, default="exact")  # exact=精确空间切分
    revisions = Column(JSONB, nullable=False)            # 启动时各输入版本计数
    used = Column(JSONB, nullable=False, default=dict)   # 实际引用：validation_keys/placement_keys/...
    status = Column(String(16), nullable=False, default="current")  # current | stale_inputs | superseded
    stats = Column(JSONB, nullable=False, default=dict)  # 分批次的窗口×类别面积时间统计
    note = Column(String(256), default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True))


class CoverageCell(Base):
    """运行产出的最小均质分片：某统计窗口×布局区间内，类别一致的矩形空间。

    category: supported | conflict | no_reading | no_qualification | layout_pending
    类别互斥（面积不重复计量）；reasons 保留不能归入单一类别的重叠原因；
    occupancy_conflict 的空间占用冲突单独标记，不纳入覆盖率分母。
    """
    __tablename__ = "coverage_cell"

    id = Column(BigInteger, primary_key=True)
    run_id = Column(Integer, ForeignKey("coverage_run.id"), nullable=False)
    cargo_batch_id = Column(String(64), nullable=False)
    pallet_id = Column(String(64), nullable=False)
    layer = Column(String(32), nullable=False)
    enclosure = Column(String(64), nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=False)   # 统计窗口
    window_end = Column(DateTime(timezone=True), nullable=False)
    interval_start = Column(DateTime(timezone=True), nullable=False) # 布局区间
    interval_end = Column(DateTime(timezone=True), nullable=False)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    w = Column(Float, nullable=False)
    h = Column(Float, nullable=False)
    category = Column(String(24), nullable=False)
    reading_verdict = Column(String(16))                 # normal | exceed（supported 时）
    reasons = Column(JSONB, nullable=False, default=list)
    probes = Column(JSONB, nullable=False, default=list) # [{probe_id,verdict,readings,temps,validated}]
    layout_variants = Column(JSONB)                      # layout_pending 时各可行布局的类别
    occupancy_conflict = Column(Boolean, nullable=False, default=False)
    conflict_with = Column(JSONB, nullable=False, default=list)  # 冲突的清单/托盘
    mixed_pallet = Column(Boolean, nullable=False, default=False)  # 混批托盘：托盘共同覆盖


class CoverageExport(Base):
    """导出快照：某次运行结果的不可变副本 + 输入依据。更正后可查影响范围。"""
    __tablename__ = "coverage_export"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("coverage_run.id"), nullable=False)
    label = Column(String(128), default="")
    payload = Column(JSONB, nullable=False)              # 完整结果快照（含输入指纹）
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CoverageImportConflict(Base):
    """导入幂等冲突：同自然键不同载荷 → 保留原值，记录冲突（不覆盖、不复制空间）。"""
    __tablename__ = "coverage_import_conflict"

    id = Column(BigInteger, primary_key=True)
    entity = Column(String(32), nullable=False)          # placement | partition | probe | validation | batch_spec
    natural_key = Column(String(64), nullable=False)
    existing_hash = Column(String(64), nullable=False)
    incoming_hash = Column(String(64), nullable=False)
    incoming_payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CoverageCorrection(Base):
    """更正事件日志：布局修订、清单更正、验证修订/撤回等。

    影响查询按运行的实际引用（run.used）追到货物与分析时段；
    导出快照按 created_at 查"导出后的更正范围"。
    """
    __tablename__ = "coverage_correction"

    id = Column(Integer, primary_key=True)
    kind = Column(String(32), nullable=False)            # layout_revise | placement_correct |
                                                         # validation_revise | validation_withdraw
    vehicle_id = Column(String(64), nullable=False)
    entity_key = Column(String(64), nullable=False)      # 布局车辆 / placement_key / validation_key
    effective_at = Column(DateTime(timezone=True))       # 生效时刻（撤回/资格起点等）
    detail = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
