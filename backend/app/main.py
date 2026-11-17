"""FastAPI 入口：全部 HTTP API。

集中解析架构：发送器只无损转发字节/报文，本服务统一组帧、按版本化契约解析、
标准化、发布。界面数据全部来自这些接口。
"""
import json as jsonlib  # noqa: F401  (保留给调试日志)
from datetime import datetime

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from .config import PURPOSES
from .db import Base, engine, get_db
from . import coverage as COV
from .coverage_demo import run_demo as coverage_run_demo
from .models import (
    ClockAnchor, ConnectionState, Consumer, ConsumptionRecord, CurrentReading,
    DeviceRelation, Frame, ParsedMessage, ProtocolContract, Publication,
    RawEnvelope, ReadingVersion, Sample, SampleConflict, StandardRun,
    TransportBatch,
)
from .models import (
    BatchSpec, CoverageCell, CoverageCorrection, CoverageExport,
    CoverageImportConflict, CoverageRun, PalletPlacement, PartitionEvent,
    ProbeInstallation, VehicleLayout, ZoneValidation,
)
from .pipeline import (
    bump_counter, close_connection, get_counter, ingest_binary_chunk, ingest_json,
)
from .publish import (
    PublishError, build_publication, consumer_ack, consumer_pull,
    get_or_create_consumer,
)
from .replay import ReplayAbort, run_replay
from .seed import seed as seed_data

app = FastAPI(title="冷链批次温控信号数据服务", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    db = next(get_db())
    try:
        seed_data(db)  # 空库时放入演示契约/关系/锚点，幂等
    finally:
        db.close()


# ---------------------------------------------------------------- 管理

@app.post("/api/admin/reset")
def admin_reset(db: Session = Depends(get_db)):
    """清空全部业务数据并重置自增序列（验收与演示用）。"""
    names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    db.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
    db.commit()
    return {"reset": True}


@app.post("/api/admin/seed")
def admin_seed(db: Session = Depends(get_db)):
    return seed_data(db)


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- 接入

class BatchIn(BaseModel):
    source: str
    protocol: str
    note: str = ""


@app.post("/api/transport-batches")
def create_batch(body: BatchIn, db: Session = Depends(get_db)):
    if body.protocol not in ("json", "binary", "mixed"):
        raise HTTPException(400, "protocol 必须是 json | binary | mixed")
    b = TransportBatch(source=body.source, protocol=body.protocol, note=body.note)
    db.add(b)
    db.commit()
    return {"id": b.id}


@app.post("/api/transport-batches/{batch_id}/close")
def close_batch(batch_id: int, db: Session = Depends(get_db)):
    b = db.get(TransportBatch, batch_id)
    if not b:
        raise HTTPException(404, "批次不存在")
    b.open = False
    b.closed_at = func.now()
    db.commit()
    return {"id": b.id, "open": False}


@app.post("/api/ingest/json")
async def ingest_json_api(batch_id: int, conn: str = "json-1",
                          msg: dict = Body(...), db: Session = Depends(get_db)):
    batch = db.get(TransportBatch, batch_id)
    if not batch:
        raise HTTPException(404, "传输批次不存在")
    pm = ingest_json(db, batch, conn, msg)
    return {"parsed_id": pm.id, "status": pm.status, "error": pm.error,
            "device_id": pm.device_id, "boot_batch": pm.boot_batch,
            "sample_seq": pm.sample_seq}


@app.post("/api/ingest/binary")
async def ingest_binary_api(request: Request, batch_id: int, conn: str = "bin-1",
                            db: Session = Depends(get_db)):
    batch = db.get(TransportBatch, batch_id)
    if not batch:
        raise HTTPException(404, "传输批次不存在")
    chunk = await request.body()
    if not chunk:
        raise HTTPException(400, "空块")
    pms = ingest_binary_chunk(db, batch, conn, chunk)
    return {"frames_parsed": [{"parsed_id": p.id, "status": p.status,
                               "sample_seq": p.sample_seq} for p in pms]}


class ConnCloseIn(BaseModel):
    batch_id: int
    conn: str


@app.post("/api/connections/close")
def close_conn_api(body: ConnCloseIn, db: Session = Depends(get_db)):
    batch = db.get(TransportBatch, body.batch_id)
    if not batch:
        raise HTTPException(404, "传输批次不存在")
    frames = close_connection(db, batch, body.conn)
    return {"finalized": [{"id": f.id, "status": f.status,
                           "start_off": f.start_off, "end_off": f.end_off}
                          for f in frames]}


# ---------------------------------------------------------------- 批次与下钻

@app.get("/api/batches")
def list_batches(db: Session = Depends(get_db)):
    batches = db.query(TransportBatch).order_by(TransportBatch.id).all()
    out = []
    for b in batches:
        frames = db.query(Frame.status, func.count()).filter(Frame.batch_id == b.id).group_by(Frame.status).all()
        pms = db.query(ParsedMessage.status, func.count()).filter(ParsedMessage.batch_id == b.id).group_by(ParsedMessage.status).all()
        fstat = {s: n for s, n in frames}
        pstat = {s: n for s, n in pms}
        samples = db.query(func.count(Sample.id)).filter(Sample.first_batch_id == b.id).scalar()
        pend_attr = (db.query(func.count(ReadingVersion.id))
                     .join(CurrentReading, CurrentReading.reading_version_id == ReadingVersion.id)
                     .join(Sample, Sample.id == ReadingVersion.sample_id)
                     .filter(Sample.first_batch_id == b.id,
                             ReadingVersion.status == "pending_attribution").scalar())
        pend_anchor = (db.query(func.count(ReadingVersion.id))
                       .join(CurrentReading, CurrentReading.reading_version_id == ReadingVersion.id)
                       .join(Sample, Sample.id == ReadingVersion.sample_id)
                       .filter(Sample.first_batch_id == b.id,
                               ReadingVersion.status == "pending_anchor").scalar())
        out.append({
            "id": b.id, "source": b.source, "protocol": b.protocol, "note": b.note,
            "open": b.open, "created_at": _iso(b.created_at),
            "envelopes": db.query(func.count(RawEnvelope.id)).filter(RawEnvelope.batch_id == b.id).scalar(),
            "frames_ok": fstat.get("ok", 0), "frames_bad_crc": fstat.get("bad_crc", 0),
            "frames_bad_length": fstat.get("bad_length", 0),
            "frames_garbage": fstat.get("garbage", 0),
            "frames_truncated": fstat.get("truncated", 0),
            "parsed_new": pstat.get("parsed_new", 0),
            "parsed_dup": pstat.get("parsed_dup", 0),
            "conflicts": pstat.get("conflict", 0),
            "pending_contract": pstat.get("pending_contract", 0),
            "parse_error": pstat.get("error", 0),
            "samples": samples,
            "pending_attribution": pend_attr, "pending_anchor": pend_anchor,
        })
    return {"batches": out}


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: int, db: Session = Depends(get_db)):
    b = db.get(TransportBatch, batch_id)
    if not b:
        raise HTTPException(404, "批次不存在")
    conns = db.query(ConnectionState).filter_by(batch_id=batch_id).all()
    envs = (db.query(RawEnvelope).filter_by(batch_id=batch_id)
            .order_by(RawEnvelope.id).all())
    frames = (db.query(Frame).filter_by(batch_id=batch_id)
              .order_by(Frame.connection_id, Frame.start_off).all())
    pms = (db.query(ParsedMessage).filter_by(batch_id=batch_id)
           .order_by(ParsedMessage.id).all())
    conflicts = (db.query(SampleConflict).join(ParsedMessage, SampleConflict.incoming_parsed_id == ParsedMessage.id)
                 .filter(ParsedMessage.batch_id == batch_id).all())
    return {
        "batch": {"id": b.id, "source": b.source, "protocol": b.protocol,
                  "note": b.note, "open": b.open, "created_at": _iso(b.created_at)},
        "connections": [{"id": c.id, "conn_key": c.conn_key, "protocol": c.protocol,
                         "open": c.open, "received_bytes": c.received_bytes,
                         "pending_bytes": len(c.buffer or b"")} for c in conns],
        "envelopes": [{"id": e.id, "kind": e.kind, "conn": e.connection_id,
                       "seq_in_conn": e.seq_in_conn,
                       "off": [e.conn_off_start, e.conn_off_end],
                       "size": len(e.payload), "received_at": _iso(e.received_at),
                       "hex": bytes(e.payload).hex()} for e in envs],
        "frames": [{"id": f.id, "conn": f.connection_id, "start_off": f.start_off,
                    "end_off": f.end_off, "status": f.status, "detail": f.detail,
                    "hex": bytes(f.payload).hex()} for f in frames],
        "parsed": [{"id": p.id, "frame_id": p.frame_id, "envelope_id": p.envelope_id,
                    "status": p.status, "device_id": p.device_id,
                    "boot_batch": p.boot_batch, "sample_seq": p.sample_seq,
                    "error": p.error, "contract_id": p.contract_id,
                    "fields": p.fields} for p in pms],
        "conflicts": [{"id": c.id, "sample_id": c.sample_id,
                       "incoming_parsed_id": c.incoming_parsed_id,
                       "incoming_fields": c.incoming_fields} for c in conflicts],
    }


@app.get("/api/frames/{frame_id}")
def frame_detail(frame_id: int, db: Session = Depends(get_db)):
    f = db.get(Frame, frame_id)
    if not f:
        raise HTTPException(404, "帧不存在")
    pm = db.query(ParsedMessage).filter_by(frame_id=f.id).order_by(ParsedMessage.id).first()
    reading = None
    if pm and pm.device_id:
        sample = (db.query(Sample).filter_by(device_id=pm.device_id, boot_batch=pm.boot_batch,
                                             sample_seq=pm.sample_seq).first())
        if sample:
            cur = db.get(CurrentReading, sample.id)
            if cur:
                rv = db.get(ReadingVersion, cur.reading_version_id)
                reading = _reading_dict(rv)
    return {"frame": {"id": f.id, "start_off": f.start_off, "end_off": f.end_off,
                      "status": f.status, "detail": f.detail,
                      "hex": bytes(f.payload).hex()},
            "parsed": {"status": pm.status, "error": pm.error,
                       "fields": pm.fields} if pm else None,
            "reading": reading}


# ---------------------------------------------------------------- 样本与曲线

@app.get("/api/samples")
def list_samples(device_id: str | None = None, cargo_batch_id: str | None = None,
                 status: str | None = None, db: Session = Depends(get_db)):
    q = (db.query(Sample, ReadingVersion)
         .join(CurrentReading, CurrentReading.sample_id == Sample.id)
         .join(ReadingVersion, ReadingVersion.id == CurrentReading.reading_version_id))
    if device_id:
        q = q.filter(Sample.device_id == device_id)
    if cargo_batch_id:
        q = q.filter(ReadingVersion.cargo_batch_id == cargo_batch_id)
    if status:
        q = q.filter(ReadingVersion.status == status)
    rows = q.order_by(Sample.device_id, Sample.boot_batch, Sample.sample_seq).all()
    return {"samples": [_sample_row(s, rv) for s, rv in rows]}


@app.get("/api/samples/{sample_id}")
def sample_detail(sample_id: int, db: Session = Depends(get_db)):
    s = db.get(Sample, sample_id)
    if not s:
        raise HTTPException(404, "样本不存在")
    cur = db.get(CurrentReading, s.id)
    rv = db.get(ReadingVersion, cur.reading_version_id) if cur else None
    history = (db.query(ReadingVersion).filter_by(sample_id=s.id)
               .order_by(ReadingVersion.id).all())
    conflicts = db.query(SampleConflict).filter_by(sample_id=s.id).all()
    return {"sample": {"id": s.id, "device_id": s.device_id, "boot_batch": s.boot_batch,
                       "sample_seq": s.sample_seq, "raw_value": s.raw_value,
                       "raw_unit": s.raw_unit, "raw_fields": s.raw_fields,
                       "first_batch_id": s.first_batch_id},
            "current": _reading_dict(rv) if rv else None,
            "history": [_reading_dict(h) for h in history],
            "conflicts": [{"id": c.id, "incoming_fields": c.incoming_fields,
                           "created_at": _iso(c.created_at)} for c in conflicts]}


@app.get("/api/curves")
def curves(cargo_batch_id: str | None = None, device_id: str | None = None,
           db: Session = Depends(get_db)):
    """批次温度曲线：按货物批次或设备取当前读数，按采样时间排序。"""
    q = (db.query(ReadingVersion, Sample)
         .join(CurrentReading, CurrentReading.reading_version_id == ReadingVersion.id)
         .join(Sample, Sample.id == ReadingVersion.sample_id)
         .filter(ReadingVersion.sample_time.isnot(None)))
    if cargo_batch_id:
        q = q.filter(ReadingVersion.cargo_batch_id == cargo_batch_id)
    if device_id:
        q = q.filter(Sample.device_id == device_id)
    rows = q.order_by(ReadingVersion.sample_time).all()
    series = {}
    for rv, s in rows:
        key = f"{s.device_id}/boot-{s.boot_batch}"
        series.setdefault(key, []).append({
            "t": rv.sample_time.isoformat(), "std_c": rv.std_c,
            "err_s": rv.time_error_s, "status": rv.status,
            "vehicle_id": rv.vehicle_id, "cargo_batch_id": rv.cargo_batch_id,
            "quality_flag": rv.quality_flag,
        })
    return {"series": series}


# ---------------------------------------------------------------- 契约与重放

class ContractIn(BaseModel):
    contract_key: str
    kind: str
    match: dict
    definition: dict
    note: str = ""


@app.get("/api/contracts")
def list_contracts(db: Session = Depends(get_db)):
    cs = (db.query(ProtocolContract)
          .order_by(ProtocolContract.contract_key, ProtocolContract.revision).all())
    return {"contracts": [{"id": c.id, "contract_key": c.contract_key,
                           "revision": c.revision, "kind": c.kind, "match": c.match,
                           "definition": c.definition, "status": c.status,
                           "note": c.note, "created_at": _iso(c.created_at)} for c in cs]}


@app.post("/api/contracts")
def create_contract(body: ContractIn, db: Session = Depends(get_db)):
    last = (db.query(ProtocolContract).filter_by(contract_key=body.contract_key)
            .order_by(ProtocolContract.revision.desc()).first())
    rev = (last.revision + 1) if last else 1
    c = ProtocolContract(contract_key=body.contract_key, revision=rev, kind=body.kind,
                         match=body.match, definition=body.definition,
                         status="draft", note=body.note)
    db.add(c)
    db.commit()
    return {"id": c.id, "revision": rev}


@app.post("/api/contracts/{contract_id}/activate")
def activate_contract(contract_id: int, db: Session = Depends(get_db)):
    c = db.get(ProtocolContract, contract_id)
    if not c:
        raise HTTPException(404, "契约不存在")
    others = (db.query(ProtocolContract)
              .filter(ProtocolContract.contract_key == c.contract_key,
                      ProtocolContract.status == "active",
                      ProtocolContract.id != c.id).all())
    for o in others:
        o.status = "retired"
    c.status = "active"
    db.commit()
    return {"id": c.id, "status": "active"}


@app.post("/api/contracts/{contract_id}/replay")
def replay_contract(contract_id: int, db: Session = Depends(get_db)):
    c = db.get(ProtocolContract, contract_id)
    if not c:
        raise HTTPException(404, "契约不存在")
    try:
        run = run_replay(db, c)
    except ReplayAbort as e:
        raise HTTPException(400, str(e))
    return {"run_id": run.id, "status": run.status, "stats": run.stats,
            "abort_reason": run.abort_reason}


@app.get("/api/runs")
def list_runs(db: Session = Depends(get_db)):
    runs = db.query(StandardRun).order_by(StandardRun.id.desc()).limit(50).all()
    return {"runs": [{"id": r.id, "kind": r.kind, "contract_key": r.contract_key,
                      "from_revision": r.from_revision, "to_revision": r.to_revision,
                      "status": r.status, "stats": r.stats,
                      "abort_reason": r.abort_reason,
                      "created_at": _iso(r.created_at)} for r in runs]}


# ---------------------------------------------------------------- 关系与锚点

class RelationIn(BaseModel):
    probe_id: str
    vehicle_id: str
    box_id: str
    cargo_batch_id: str
    valid_from: datetime
    valid_to: datetime | None = None


@app.get("/api/relations")
def list_relations(db: Session = Depends(get_db)):
    rs = db.query(DeviceRelation).order_by(DeviceRelation.probe_id, DeviceRelation.valid_from).all()
    return {"relation_revision": get_counter(db, "relation_revision"),
            "relations": [{"id": r.id, "probe_id": r.probe_id, "vehicle_id": r.vehicle_id,
                           "box_id": r.box_id, "cargo_batch_id": r.cargo_batch_id,
                           "valid_from": r.valid_from.isoformat(),
                           "valid_to": r.valid_to.isoformat() if r.valid_to else None} for r in rs]}


@app.post("/api/relations")
def create_relation(body: RelationIn, db: Session = Depends(get_db)):
    overlap = (db.query(DeviceRelation)
               .filter(DeviceRelation.probe_id == body.probe_id,
                       DeviceRelation.valid_from < (body.valid_to or datetime.max.replace(tzinfo=body.valid_from.tzinfo)),
                       (DeviceRelation.valid_to.is_(None)) | (DeviceRelation.valid_to > body.valid_from))
               .all())
    if overlap:
        raise HTTPException(409, f"与既有安装区间重叠：relation {overlap[0].id}")
    r = DeviceRelation(**body.model_dump())
    db.add(r)
    bump_counter(db, "relation_revision")
    db.commit()
    return {"id": r.id, "relation_revision": get_counter(db, "relation_revision")}


class AnchorIn(BaseModel):
    device_id: str
    boot_batch: str
    counter: int
    server_time: datetime
    error_s: float = 2.0
    source: str = "gateway_sync"


@app.get("/api/anchors")
def list_anchors(db: Session = Depends(get_db)):
    aa = db.query(ClockAnchor).order_by(ClockAnchor.device_id, ClockAnchor.counter).all()
    return {"anchors": [{"id": a.id, "device_id": a.device_id, "boot_batch": a.boot_batch,
                         "counter": a.counter, "server_time": a.server_time.isoformat(),
                         "error_s": a.error_s, "source": a.source} for a in aa]}


@app.post("/api/anchors")
def create_anchor(body: AnchorIn, db: Session = Depends(get_db)):
    a = ClockAnchor(**body.model_dump())
    db.add(a)
    db.commit()
    return {"id": a.id}


# ---------------------------------------------------------------- 发布与消费

@app.post("/api/publications/build")
def build_pub(purpose: str, db: Session = Depends(get_db)):
    try:
        pub = build_publication(db, purpose)
    except PublishError as e:
        raise HTTPException(400, str(e))
    return {"id": pub.id, "purpose": pub.purpose, "version": pub.version,
            "item_count": pub.item_count}


@app.get("/api/publications")
def list_pubs(purpose: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Publication)
    if purpose:
        q = q.filter_by(purpose=purpose)
    pubs = q.order_by(Publication.purpose, Publication.version).all()
    return {"purposes": PURPOSES,
            "publications": [{"id": p.id, "purpose": p.purpose, "version": p.version,
                              "status": p.status, "item_count": p.item_count,
                              "relation_revision": p.relation_revision,
                              "created_at": _iso(p.created_at)} for p in pubs]}


@app.get("/api/publications/{pub_id}/items")
def pub_items(pub_id: int, db: Session = Depends(get_db)):
    pub = db.get(Publication, pub_id)
    if not pub:
        raise HTTPException(404, "发布版本不存在")
    from .models import PublicationItem
    items = (db.query(PublicationItem).filter_by(publication_id=pub_id)
             .order_by(PublicationItem.seq).all())
    return {"publication": {"id": pub.id, "purpose": pub.purpose, "version": pub.version,
                            "status": pub.status, "item_count": pub.item_count},
            "items": [{"seq": i.seq, "sample_id": i.sample_id, "device_id": i.device_id,
                       "std_c": i.std_c,
                       "sample_time": i.sample_time.isoformat() if i.sample_time else None,
                       "time_error_s": i.time_error_s, "vehicle_id": i.vehicle_id,
                       "box_id": i.box_id, "cargo_batch_id": i.cargo_batch_id}
                      for i in items]}


class ConsumerIn(BaseModel):
    name: str
    purpose: str


@app.post("/api/consumers")
def create_consumer(body: ConsumerIn, db: Session = Depends(get_db)):
    try:
        c = get_or_create_consumer(db, body.name, body.purpose)
    except PublishError as e:
        raise HTTPException(400, str(e))
    return {"id": c.id, "name": c.name, "purpose": c.purpose}


@app.get("/api/consumers")
def list_consumers(db: Session = Depends(get_db)):
    cs = db.query(Consumer).order_by(Consumer.id).all()
    return {"consumers": [_consumer_dict(db, c) for c in cs]}


@app.get("/api/consumers/{consumer_id}")
def get_consumer(consumer_id: int, db: Session = Depends(get_db)):
    c = db.get(Consumer, consumer_id)
    if not c:
        raise HTTPException(404, "消费端不存在")
    return _consumer_dict(db, c)


@app.post("/api/consumers/{consumer_id}/pull")
def pull(consumer_id: int, size: int | None = None, db: Session = Depends(get_db)):
    c = db.get(Consumer, consumer_id)
    if not c:
        raise HTTPException(404, "消费端不存在")
    return consumer_pull(db, c, size)


class AckIn(BaseModel):
    version: int
    from_seq: int
    to_seq: int


@app.post("/api/consumers/{consumer_id}/ack")
def ack(consumer_id: int, body: AckIn, db: Session = Depends(get_db)):
    c = db.get(Consumer, consumer_id)
    if not c:
        raise HTTPException(404, "消费端不存在")
    try:
        return consumer_ack(db, c, body.version, body.from_seq, body.to_seq)
    except PublishError as e:
        raise HTTPException(409, str(e))


@app.get("/api/consumption-records")
def consumption_records(consumer_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(ConsumptionRecord)
    if consumer_id:
        q = q.filter_by(consumer_id=consumer_id)
    recs = q.order_by(ConsumptionRecord.id).all()
    return {"records": [{"id": r.id, "consumer_id": r.consumer_id,
                         "publication_id": r.publication_id,
                         "from_seq": r.from_seq, "to_seq": r.to_seq,
                         "item_count": r.item_count,
                         "acked_at": _iso(r.acked_at)} for r in recs]}


# ---------------------------------------------------------------- 总览

@app.get("/api/overview")
def overview(db: Session = Depends(get_db)):
    def n(model):
        return db.query(func.count(model.id)).scalar()
    return {
        "batches": n(TransportBatch), "envelopes": n(RawEnvelope),
        "frames": n(Frame), "samples": n(Sample),
        "conflicts": n(SampleConflict),
        "pending_contract": db.query(func.count(ParsedMessage.id)).filter_by(status="pending_contract").scalar(),
        "pending_anchor": db.query(func.count(ReadingVersion.id)).join(CurrentReading, CurrentReading.reading_version_id == ReadingVersion.id).filter(ReadingVersion.status == "pending_anchor").scalar(),
        "pending_attribution": db.query(func.count(ReadingVersion.id)).join(CurrentReading, CurrentReading.reading_version_id == ReadingVersion.id).filter(ReadingVersion.status == "pending_attribution").scalar(),
        "contracts": n(ProtocolContract), "publications": n(Publication),
        "consumers": n(Consumer),
    }


# ---------------------------------------------------------------- 序列化辅助

def _iso(dt):
    return dt.isoformat() if dt else None


def _reading_dict(rv: ReadingVersion) -> dict:
    return {"id": rv.id, "sample_id": rv.sample_id, "std_c": rv.std_c,
            "quality_flag": rv.quality_flag,
            "sample_time": _iso(rv.sample_time), "time_error_s": rv.time_error_s,
            "vehicle_id": rv.vehicle_id, "box_id": rv.box_id,
            "cargo_batch_id": rv.cargo_batch_id, "status": rv.status,
            "status_detail": rv.status_detail,
            "contract_key": rv.contract_key, "contract_revision": rv.contract_revision,
            "relation_revision": rv.relation_revision, "conversion": rv.conversion,
            "source": rv.source, "run_id": rv.run_id, "created_at": _iso(rv.created_at)}


def _sample_row(s: Sample, rv: ReadingVersion) -> dict:
    return {"id": s.id, "device_id": s.device_id, "boot_batch": s.boot_batch,
            "sample_seq": s.sample_seq, "raw_value": s.raw_value, "raw_unit": s.raw_unit,
            "std_c": rv.std_c, "quality_flag": rv.quality_flag,
            "sample_time": _iso(rv.sample_time), "time_error_s": rv.time_error_s,
            "vehicle_id": rv.vehicle_id, "box_id": rv.box_id,
            "cargo_batch_id": rv.cargo_batch_id, "status": rv.status,
            "first_batch_id": s.first_batch_id}


def _consumer_dict(db: Session, c: Consumer) -> dict:
    records = db.query(func.count(ConsumptionRecord.id)).filter_by(consumer_id=c.id).scalar()
    return {"id": c.id, "name": c.name, "purpose": c.purpose,
            "cursor_version": c.cursor_version, "cursor_seq": c.cursor_seq,
            "pending_page": c.pending_page, "acked_pages": records}


# ---------------------------------------------------------------- 分区覆盖

@app.post("/api/coverage/import")
def coverage_import(bundle: dict = Body(...), db: Session = Depends(get_db)):
    """导入覆盖输入（布局/清单/隔板/探头/验证/批次规格）。幂等：
    同键同载荷去重，同键不同载荷记冲突保留原值，不复制货物空间。"""
    try:
        return COV.import_bundle(db, bundle)
    except COV.CoverageError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(400, f"缺少字段：{e}")


@app.post("/api/coverage/demo")
def coverage_demo(db: Session = Depends(get_db)):
    """一键演示：补演示读数 → 导入演示输入 → 对演示窗口执行覆盖分析。"""
    try:
        return coverage_run_demo(db)
    except COV.CoverageError as e:
        raise HTTPException(400, str(e))


@app.get("/api/coverage/inputs")
def coverage_inputs(vehicle_id: str, db: Session = Depends(get_db)):
    """车辆当前覆盖输入：布局、清单、隔板、探头安装、验证资料、批次规格。"""
    layout = COV.current_layout(db, vehicle_id)
    layouts = (db.query(VehicleLayout).filter_by(vehicle_id=vehicle_id)
               .order_by(VehicleLayout.revision).all())
    placements = (db.query(PalletPlacement).filter_by(vehicle_id=vehicle_id)
                  .order_by(PalletPlacement.pallet_id, PalletPlacement.version).all())
    partitions = (db.query(PartitionEvent).filter_by(vehicle_id=vehicle_id)
                  .order_by(PartitionEvent.effective_from).all())
    probes = (db.query(ProbeInstallation).filter_by(vehicle_id=vehicle_id)
              .order_by(ProbeInstallation.installed_from).all())
    vals = (db.query(ZoneValidation).filter_by(vehicle_id=vehicle_id)
            .order_by(ZoneValidation.validation_key, ZoneValidation.revision).all())
    specs = db.query(BatchSpec).order_by(BatchSpec.cargo_batch_id).all()
    return {
        "vehicle_id": vehicle_id,
        "current_layout_revision": layout.revision if layout else None,
        "layouts": [{"revision": l.revision, "status": l.status, "coord": l.coord,
                     "length_m": l.length_m, "width_m": l.width_m, "layers": l.layers,
                     "zones": l.zones, "note": l.note} for l in layouts],
        "placements": [{"placement_key": p.placement_key, "version": p.version,
                        "manifest_id": p.manifest_id, "pallet_id": p.pallet_id,
                        "batches": p.batches, "rect": p.rect, "layer": p.layer,
                        "from_lo": _iso(p.from_lo), "from_hi": _iso(p.from_hi),
                        "to_lo": _iso(p.to_lo), "to_hi": _iso(p.to_hi),
                        "corrected_by": p.corrected_by} for p in placements],
        "partitions": [{"event_key": e.event_key, "partition_id": e.partition_id,
                        "x_position": e.x_position,
                        "effective_from": _iso(e.effective_from),
                        "effective_to": _iso(e.effective_to)} for e in partitions],
        "probes": [{"install_key": i.install_key, "probe_id": i.probe_id,
                    "x": i.x, "y": i.y, "layer": i.layer,
                    "installed_from": _iso(i.installed_from),
                    "installed_to": _iso(i.installed_to)} for i in probes],
        "validations": [{"validation_key": v.validation_key, "revision": v.revision,
                         "probe_id": v.probe_id, "structure": v.structure,
                         "install_point": v.install_point,
                         "install_condition": v.install_condition,
                         "range_rect": v.range_rect, "range_layer": v.range_layer,
                         "partition_config": v.partition_config,
                         "condition_group": v.condition_group,
                         "valid_from": _iso(v.valid_from), "valid_to": _iso(v.valid_to),
                         "status": v.status,
                         "withdrawn_from": _iso(v.withdrawn_from),
                         "withdraw_reason": v.withdraw_reason} for v in vals],
        "batch_specs": [{"cargo_batch_id": s.cargo_batch_id, "temp_min_c": s.temp_min_c,
                         "temp_max_c": s.temp_max_c, "window_minutes": s.window_minutes,
                         "min_readings": s.min_readings} for s in specs],
        "revisions": COV.input_revisions(db),
    }


class CoverageRunIn(BaseModel):
    vehicle_id: str
    cargo_batch_id: str | None = None
    window_start: datetime
    window_end: datetime


@app.post("/api/coverage/runs")
def coverage_run_create(body: CoverageRunIn, db: Session = Depends(get_db)):
    """执行一次覆盖分析（精确空间切分）。计算期间输入变化则标记 stale_inputs。"""
    try:
        run = COV.run_coverage(db, body.vehicle_id, body.cargo_batch_id,
                               body.window_start, body.window_end)
    except COV.CoverageError as e:
        raise HTTPException(400, str(e))
    return {"run_id": run.id, "status": run.status, "note": run.note,
            "stats": run.stats}


@app.get("/api/coverage/runs")
def coverage_runs(vehicle_id: str | None = None, db: Session = Depends(get_db)):
    q = db.query(CoverageRun)
    if vehicle_id:
        q = q.filter_by(vehicle_id=vehicle_id)
    runs = q.order_by(CoverageRun.id.desc()).limit(100).all()
    return {"runs": [{**COV.run_dict(db, r), "stats": None} for r in runs]}


@app.get("/api/coverage/runs/{run_id}")
def coverage_run_detail(run_id: int, db: Session = Depends(get_db)):
    run = db.get(CoverageRun, run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    layout = (db.query(VehicleLayout)
              .filter_by(vehicle_id=run.vehicle_id)
              .filter(VehicleLayout.revision == (run.used or {}).get("layout_revision", -1))
              .first())
    placements = (db.query(PalletPlacement).filter_by(vehicle_id=run.vehicle_id)
                  .order_by(PalletPlacement.pallet_id, PalletPlacement.version).all())
    partitions = (db.query(PartitionEvent).filter_by(vehicle_id=run.vehicle_id)
                  .order_by(PartitionEvent.effective_from).all())
    probes = (db.query(ProbeInstallation).filter_by(vehicle_id=run.vehicle_id).all())
    vals = (db.query(ZoneValidation).filter_by(vehicle_id=run.vehicle_id)
            .order_by(ZoneValidation.validation_key, ZoneValidation.revision).all())
    return {"run": COV.run_dict(db, run),
            "layout": {"revision": layout.revision, "coord": layout.coord,
                       "length_m": layout.length_m, "width_m": layout.width_m,
                       "layers": layout.layers, "zones": layout.zones} if layout else None,
            "placements": [{"placement_key": p.placement_key, "manifest_id": p.manifest_id,
                            "pallet_id": p.pallet_id, "batches": p.batches, "rect": p.rect,
                            "layer": p.layer, "from_lo": _iso(p.from_lo),
                            "from_hi": _iso(p.from_hi), "to_lo": _iso(p.to_lo),
                            "to_hi": _iso(p.to_hi), "corrected_by": p.corrected_by}
                           for p in placements],
            "partitions": [{"partition_id": e.partition_id, "x_position": e.x_position,
                            "effective_from": _iso(e.effective_from),
                            "effective_to": _iso(e.effective_to)} for e in partitions],
            "probes": [{"probe_id": i.probe_id, "x": i.x, "y": i.y, "layer": i.layer,
                        "installed_from": _iso(i.installed_from),
                        "installed_to": _iso(i.installed_to)} for i in probes],
            "validations": [{"validation_key": v.validation_key, "revision": v.revision,
                             "probe_id": v.probe_id, "range_rect": v.range_rect,
                             "range_layer": v.range_layer,
                             "partition_config": v.partition_config,
                             "condition_group": v.condition_group,
                             "valid_from": _iso(v.valid_from), "valid_to": _iso(v.valid_to),
                             "status": v.status, "withdrawn_from": _iso(v.withdrawn_from)}
                            for v in vals]}


@app.get("/api/coverage/runs/{run_id}/cells")
def coverage_run_cells(run_id: int, cargo_batch_id: str | None = None,
                       category: str | None = None,
                       window_start: str | None = None,
                       db: Session = Depends(get_db)):
    if not db.get(CoverageRun, run_id):
        raise HTTPException(404, "运行不存在")
    q = db.query(CoverageCell).filter_by(run_id=run_id)
    if cargo_batch_id:
        q = q.filter_by(cargo_batch_id=cargo_batch_id)
    if category:
        q = q.filter_by(category=category)
    if window_start:
        q = q.filter(CoverageCell.window_start == COV._dt(window_start))
    cells = q.order_by(CoverageCell.id).all()
    return {"cells": [COV.cell_dict(c) for c in cells]}


@app.get("/api/coverage/runs/{run_id}/unconfirmed")
def coverage_run_unconfirmed(run_id: int, db: Session = Depends(get_db)):
    """未确认原因查询：缺资格/缺读数/布局待核的空间与原因，含探头实际测量点。"""
    if not db.get(CoverageRun, run_id):
        raise HTTPException(404, "运行不存在")
    return COV.unconfirmed_of_run(db, run_id)


@app.get("/api/coverage/batch-summary")
def coverage_batch_summary(vehicle_id: str, cargo_batch_id: str,
                           db: Session = Depends(get_db)):
    """批次查询：该批次最近一次当前运行的覆盖结果（批次查询使用覆盖结果）。"""
    run = (db.query(CoverageRun)
           .filter_by(vehicle_id=vehicle_id, status="current")
           .order_by(CoverageRun.id.desc()).first())
    if not run:
        raise HTTPException(404, "该车没有当前覆盖分析，请先运行")
    stats = (run.stats or {}).get("batches", {}).get(cargo_batch_id)
    if not stats:
        raise HTTPException(404, f"运行 {run.id} 中没有批次 {cargo_batch_id} 的覆盖结果")
    return {"run_id": run.id, "run_status": run.status,
            "inputs_stale": COV.run_dict(db, run)["inputs_stale"],
            "vehicle_id": vehicle_id, "cargo_batch_id": cargo_batch_id,
            "window_start": _iso(run.window_start), "window_end": _iso(run.window_end),
            "coverage": stats}


class ExportIn(BaseModel):
    label: str = ""


@app.post("/api/coverage/runs/{run_id}/export")
def coverage_export_create(run_id: int, body: ExportIn | None = None,
                           db: Session = Depends(get_db)):
    try:
        exp = COV.export_run(db, run_id, (body.label if body else ""))
    except COV.CoverageError as e:
        raise HTTPException(404, str(e))
    return {"id": exp.id, "run_id": exp.run_id, "created_at": _iso(exp.created_at)}


@app.get("/api/coverage/exports")
def coverage_exports(db: Session = Depends(get_db)):
    exps = db.query(CoverageExport).order_by(CoverageExport.id.desc()).all()
    return {"exports": [COV.export_dict(db, e) for e in exps]}


@app.get("/api/coverage/exports/{exp_id}")
def coverage_export_detail(exp_id: int, db: Session = Depends(get_db)):
    exp = db.get(CoverageExport, exp_id)
    if not exp:
        raise HTTPException(404, "导出不存在")
    return COV.export_dict(db, exp, with_payload=True)


class PlacementCorrectIn(BaseModel):
    manifest_id: str
    vehicle_id: str
    pallet_id: str
    batches: list
    rect: dict
    layer: str
    from_lo: datetime
    from_hi: datetime | None = None
    to_lo: datetime | None = None
    to_hi: datetime | None = None


@app.post("/api/coverage/placements/{key}/correct")
def coverage_placement_correct(key: str, body: PlacementCorrectIn,
                               db: Session = Depends(get_db)):
    """清单更正：新版本替换旧版本；返回按实际引用追到的货物与分析时段。"""
    try:
        new, impact = COV.correct_placement(db, key, body.model_dump())
    except COV.CoverageError as e:
        raise HTTPException(404, str(e))
    return {"placement_key": key, "version": new.version, "impact": impact}


@app.post("/api/coverage/validations/{key}/revise")
def coverage_validation_revise(key: str, item: dict = Body(...),
                               db: Session = Depends(get_db)):
    try:
        new, impact = COV.revise_validation(db, key, item)
    except COV.CoverageError as e:
        raise HTTPException(400, str(e))
    return {"validation_key": key, "revision": new.revision, "impact": impact}


class WithdrawIn(BaseModel):
    withdrawn_from: datetime
    reason: str = ""


@app.post("/api/coverage/validations/{key}/withdraw")
def coverage_validation_withdraw(key: str, body: WithdrawIn,
                                 db: Session = Depends(get_db)):
    try:
        v, impact = COV.withdraw_validation(db, key, body.withdrawn_from, body.reason)
    except COV.CoverageError as e:
        raise HTTPException(400, str(e))
    return {"validation_key": key, "status": v.status,
            "withdrawn_from": _iso(v.withdrawn_from), "impact": impact}


class WithdrawGroupIn(BaseModel):
    condition_group: str
    withdrawn_from: datetime
    reason: str = ""


@app.post("/api/coverage/validations/withdraw-group")
def coverage_validation_withdraw_group(body: WithdrawGroupIn,
                                       db: Session = Depends(get_db)):
    """批量撤回：同一验证条件下的全部现行验证失效，波及多次运输。"""
    try:
        return COV.withdraw_group(db, body.condition_group, body.withdrawn_from,
                                  body.reason)
    except COV.CoverageError as e:
        raise HTTPException(400, str(e))


@app.get("/api/coverage/impact")
def coverage_impact(kind: str, key: str = "", vehicle_id: str | None = None,
                    effective_at: datetime | None = None,
                    db: Session = Depends(get_db)):
    """影响查询：按实际引用追到货物批次与分析时段（含补证获得资格的记录）。"""
    if kind == "layout":
        if not vehicle_id:
            raise HTTPException(400, "layout 影响查询需要 vehicle_id")
        lay = COV.current_layout(db, vehicle_id)
        rev = lay.revision if lay else 0
        out = COV.impact_of(db, "layout", vehicle_id, vehicle_id)
        out["current_layout_revision"] = rev
        out["affected_runs"] = [r for r in out["affected_runs"]]
        # 只保留引用旧布局修订的运行
        runs = []
        for r in db.query(CoverageRun).filter_by(vehicle_id=vehicle_id).all():
            if (r.used or {}).get("layout_revision", rev) < rev:
                runs.append({"run_id": r.id, "status": r.status,
                             "cargo_batch_id": r.cargo_batch_id,
                             "window_start": _iso(r.window_start),
                             "window_end": _iso(r.window_end),
                             "layout_revision": (r.used or {}).get("layout_revision")})
        out["affected_runs"] = runs
        return out
    if kind not in ("placement", "validation"):
        raise HTTPException(400, "kind 必须是 layout | placement | validation")
    return COV.impact_of(db, kind, key, vehicle_id, effective_at)


@app.get("/api/coverage/compare")
def coverage_compare(run_a: int, run_b: int, db: Session = Depends(get_db)):
    """布局更正前后比较：分窗口类别面积时间对比 + 类别变化的分片。"""
    try:
        return COV.compare_runs(db, run_a, run_b)
    except COV.CoverageError as e:
        raise HTTPException(404, str(e))


@app.get("/api/coverage/corrections")
def coverage_corrections(vehicle_id: str | None = None, db: Session = Depends(get_db)):
    q = db.query(CoverageCorrection)
    if vehicle_id:
        q = q.filter_by(vehicle_id=vehicle_id)
    corrs = q.order_by(CoverageCorrection.id.desc()).limit(100).all()
    return {"corrections": [{"id": c.id, "kind": c.kind, "vehicle_id": c.vehicle_id,
                             "entity_key": c.entity_key,
                             "effective_at": _iso(c.effective_at), "detail": c.detail,
                             "created_at": _iso(c.created_at)} for c in corrs]}


@app.get("/api/coverage/import-conflicts")
def coverage_import_conflicts(db: Session = Depends(get_db)):
    rows = db.query(CoverageImportConflict).order_by(CoverageImportConflict.id.desc()).limit(100).all()
    return {"conflicts": [{"id": c.id, "entity": c.entity, "natural_key": c.natural_key,
                           "incoming_payload": c.incoming_payload,
                           "created_at": _iso(c.created_at)} for c in rows]}
