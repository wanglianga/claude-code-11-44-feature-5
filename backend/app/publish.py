"""质量发布与消费。

发布：对某用途取当前索引中达到该用途门槛的读数，生成完整快照版本；
      旧版本转为 superseded，消费端总能读到完整一致的版本。
消费：游标 (version, seq)；未确认的分页重发原样内容，确认才前进；
      失败恢复不会跳过未确认分页，重复拉取不产生重复样本。
"""
from sqlalchemy.orm import Session

from .config import PURPOSES, settings
from .models import (
    Consumer, ConsumptionRecord, CurrentReading, Publication, PublicationItem,
    ReadingVersion, Sample,
)
from .pipeline import get_counter


class PublishError(Exception):
    pass


def build_publication(db: Session, purpose: str) -> Publication:
    if purpose not in PURPOSES:
        raise PublishError(f"未知用途 {purpose!r}，可选：{list(PURPOSES)}")
    bar = PURPOSES[purpose]
    rows = (db.query(ReadingVersion, Sample)
            .join(CurrentReading, CurrentReading.reading_version_id == ReadingVersion.id)
            .join(Sample, Sample.id == ReadingVersion.sample_id)
            .filter(ReadingVersion.quality_flag == "ok",
                    ReadingVersion.sample_time.isnot(None),
                    ReadingVersion.time_error_s <= bar["max_time_error_s"])
            .order_by(ReadingVersion.sample_time, Sample.id)
            .all())
    if bar["require_attribution"]:
        rows = [(rv, s) for rv, s in rows if rv.status == "ok" and rv.cargo_batch_id]

    old = (db.query(Publication)
           .filter_by(purpose=purpose, status="current").all())
    for p in old:
        p.status = "superseded"
    version = max([p.version for p in db.query(Publication).filter_by(purpose=purpose)] or [0]) + 1
    pub = Publication(purpose=purpose, version=version, item_count=len(rows),
                      relation_revision=get_counter(db, "relation_revision"),
                      stats={"bar": bar, "candidates": len(rows)})
    db.add(pub)
    db.flush()
    for i, (rv, s) in enumerate(rows):
        db.add(PublicationItem(
            publication_id=pub.id, seq=i, sample_id=s.id, device_id=s.device_id,
            std_c=rv.std_c, sample_time=rv.sample_time, time_error_s=rv.time_error_s,
            vehicle_id=rv.vehicle_id, box_id=rv.box_id, cargo_batch_id=rv.cargo_batch_id,
        ))
    db.commit()
    return pub


def get_or_create_consumer(db: Session, name: str, purpose: str) -> Consumer:
    c = db.query(Consumer).filter_by(name=name).first()
    if c:
        return c
    if purpose not in PURPOSES:
        raise PublishError(f"未知用途 {purpose!r}")
    c = Consumer(name=name, purpose=purpose)
    db.add(c)
    db.commit()
    return c


def _page_items(db: Session, publication_id: int, frm: int, size: int):
    return (db.query(PublicationItem)
            .filter(PublicationItem.publication_id == publication_id,
                    PublicationItem.seq >= frm)
            .order_by(PublicationItem.seq).limit(size).all())


def consumer_pull(db: Session, consumer: Consumer, size: int | None = None) -> dict:
    size = size or settings.CONSUMER_PAGE_SIZE
    # 有未确认分页 → 原样重发（失败恢复不跳过、重复拉取不产生重复样本）
    if consumer.pending_page:
        pp = consumer.pending_page
        pub = (db.query(Publication)
               .filter_by(purpose=consumer.purpose, version=pp["version"]).first())
        items = _page_items(db, pub.id, pp["from"], pp["to"] - pp["from"] + 1)
        return {"consumer": consumer.name, "purpose": consumer.purpose,
                "version": pub.version, "from": pp["from"], "to": pp["to"],
                "redelivered": True, "items": [_item_dict(i) for i in items],
                "has_more": pp["to"] + 1 < pub.item_count}

    current = (db.query(Publication)
               .filter_by(purpose=consumer.purpose, status="current").first())
    if not current:
        return {"consumer": consumer.name, "purpose": consumer.purpose,
                "version": None, "items": [], "has_more": False,
                "note": "该用途还没有当前发布版本"}

    if consumer.cursor_version != current.version:
        # 旧版本已读完（或首次读取）→ 从当前版本开头开始
        if consumer.cursor_version is None or consumer.cursor_seq >= _version_count(db, consumer.purpose, consumer.cursor_version):
            consumer.cursor_version = current.version
            consumer.cursor_seq = 0
        else:
            # 旧版本还没读完，继续把旧版本读完（按版本完整交付）
            current = (db.query(Publication)
                       .filter_by(purpose=consumer.purpose,
                                  version=consumer.cursor_version).first())
    if consumer.cursor_seq >= current.item_count:
        return {"consumer": consumer.name, "purpose": consumer.purpose,
                "version": current.version, "items": [], "has_more": False,
                "note": "当前版本已读完"}

    frm = consumer.cursor_seq
    items = _page_items(db, current.id, frm, size)
    to = frm + len(items) - 1
    consumer.pending_page = {"version": current.version, "from": frm, "to": to}
    db.commit()
    return {"consumer": consumer.name, "purpose": consumer.purpose,
            "version": current.version, "from": frm, "to": to,
            "redelivered": False, "items": [_item_dict(i) for i in items],
            "has_more": to + 1 < current.item_count}


def consumer_ack(db: Session, consumer: Consumer, version: int, frm: int, to: int) -> dict:
    pp = consumer.pending_page
    if not pp or pp["version"] != version or pp["from"] != frm or pp["to"] != to:
        raise PublishError(f"确认与未确认分页不符：pending={pp}, ack=({version},{frm},{to})")
    pub = (db.query(Publication)
           .filter_by(purpose=consumer.purpose, version=version).first())
    rec = (db.query(ConsumptionRecord)
           .filter_by(consumer_id=consumer.id, publication_id=pub.id, from_seq=frm)
           .first())
    if not rec:
        rec = ConsumptionRecord(consumer_id=consumer.id, publication_id=pub.id,
                                from_seq=frm, to_seq=to, item_count=to - frm + 1)
        db.add(rec)
    consumer.cursor_version = version
    consumer.cursor_seq = to + 1
    consumer.pending_page = None
    db.commit()
    return {"acked": True, "cursor_version": version, "cursor_seq": to + 1,
            "record_id": rec.id}


def _version_count(db: Session, purpose: str, version: int) -> int:
    pub = db.query(Publication).filter_by(purpose=purpose, version=version).first()
    return pub.item_count if pub else 0


def _item_dict(i: PublicationItem) -> dict:
    return {"seq": i.seq, "sample_id": i.sample_id, "device_id": i.device_id,
            "std_c": i.std_c, "sample_time": i.sample_time.isoformat() if i.sample_time else None,
            "time_error_s": i.time_error_s, "vehicle_id": i.vehicle_id,
            "box_id": i.box_id, "cargo_batch_id": i.cargo_batch_id}
