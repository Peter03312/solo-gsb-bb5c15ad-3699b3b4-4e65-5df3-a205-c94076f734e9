"""测温设备校准证明（登记 / 作废 / 查询）与按样本的可信测温复核。

规则（与 README 一致）：
  * 校准证明可能晚于运输记录补齐：按全局唯一证明号登记设备号、有效起止时刻与签发人；
    有效期左闭右开 [valid_from, valid_until)，同一设备的有效（未作废）证明时段不得重叠；
  * 同证明号同内容重试返回原结果（200），异内容冲突（409 cert_conflict）；
    无效时段（valid_from ≥ valid_until，422 字段校验）、未知证明号（404 cert_not_found）、
    时段重叠（409 cert_period_overlap）均拒绝且不留部分记录；
  * 支持按证明号附理由作废：作废后不再覆盖读数、不参与重叠判定（时段释放可补登）；
    同号同理由重试幂等返回原结果（200），异理由冲突（409 cert_void_conflict）；
  * 可信复核查询按样本逐条列出读数匹配的证明号或缺证原因，仅用有证读数计算超温点与
    相邻有证读数的缺测区间，沿用现有保管链的区间拆分与归属；没有有证读数时明确标记无法评估；
  * 补录或作废证明后查询即时重算；证明登记/作废不改原始读数、现有测温查询、
    保管链版本或已封存案卷，也不受样本核定冻结影响（证明是设备维度证据）。
"""
import sqlite3
from typing import Any

from .db import utcnow_iso
from .schemas import TempCertIn, TempCertVoidIn
from .service import DomainError, _fingerprint, get_sample_or_404, to_utc_iso
from .temperature import (
    _attribution_at,
    _config_view,
    _custody_periods,
    _get_config_row,
    _seconds_between,
)


# --------------------------------------------------------------------------- 证明登记

def _cert_fingerprint(data: TempCertIn, valid_from: str, valid_until: str) -> str:
    return _fingerprint({
        "cert_id": data.cert_id,
        "device_id": data.device_id,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "issuer": data.issuer,
    })


def _cert_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cert_id": row["cert_id"],
        "device_id": row["device_id"],
        "valid_from": row["valid_from"],
        "valid_until": row["valid_until"],
        "issuer": row["issuer"],
        "status": "voided" if row["voided_at"] else "effective",
        "voided_at": row["voided_at"],
        "void_reason": row["void_reason"],
        "registered_at": row["created_at"],
    }


def _get_cert(conn: sqlite3.Connection, cert_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM temp_calibration_cert WHERE cert_id = ?", (cert_id,)
    ).fetchone()


def register_certificate(conn: sqlite3.Connection, data: TempCertIn) -> tuple[dict[str, Any], bool]:
    """登记校准证明。返回 (证明视图, 是否幂等重试)。

    同证明号同内容重试返回原记录（200，即使原记录已作废）；同号异内容 409；
    同一设备的有效证明时段重叠 409（左闭右开，端点相邻不算重叠；作废证明不参与）。
    """
    valid_from = to_utc_iso(data.valid_from)
    valid_until = to_utc_iso(data.valid_until)
    h = _cert_fingerprint(data, valid_from, valid_until)

    # 幂等查重优先（与既有读数接口语义一致）：同号同内容重试返回原记录
    existing = _get_cert(conn, data.cert_id)
    if existing is not None:
        if existing["content_hash"] != h:
            raise DomainError(
                409,
                "cert_conflict",
                f"证明号 {data.cert_id} 已登记但内容不一致（同证明号异内容冲突）",
                {"cert_id": data.cert_id,
                 "existing": {"device_id": existing["device_id"],
                              "valid_from": existing["valid_from"],
                              "valid_until": existing["valid_until"],
                              "issuer": existing["issuer"],
                              "content_hash": existing["content_hash"]},
                 "incoming": {"device_id": data.device_id, "valid_from": valid_from,
                              "valid_until": valid_until, "issuer": data.issuer,
                              "content_hash": h}},
            )
        return _cert_view(existing), True

    # 同一设备的有效（未作废）证明时段不得重叠：左闭右开区间相交判定
    overlap = conn.execute(
        "SELECT * FROM temp_calibration_cert"
        " WHERE device_id = ? AND voided_at IS NULL AND valid_from < ? AND ? < valid_until"
        " ORDER BY valid_from ASC, id ASC",
        (data.device_id, valid_until, valid_from),
    ).fetchall()
    if overlap:
        raise DomainError(
            409,
            "cert_period_overlap",
            f"设备 {data.device_id} 已存在与 [{valid_from}, {valid_until}) 重叠的有效证明 "
            f"{overlap[0]['cert_id']}（[{overlap[0]['valid_from']}, {overlap[0]['valid_until']}）），"
            "同一设备的有效证明时段不得重叠",
            {"cert_id": data.cert_id, "device_id": data.device_id,
             "incoming_period": {"valid_from": valid_from, "valid_until": valid_until},
             "overlapping": [{"cert_id": c["cert_id"], "valid_from": c["valid_from"],
                              "valid_until": c["valid_until"]} for c in overlap]},
        )

    try:
        cur = conn.execute(
            "INSERT INTO temp_calibration_cert"
            " (cert_id, device_id, valid_from, valid_until, issuer, content_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (data.cert_id, data.device_id, valid_from, valid_until, data.issuer, h, utcnow_iso()),
        )
    except sqlite3.IntegrityError:
        # 并发下证明号唯一键竞争：按已提交结果判定
        raced = _get_cert(conn, data.cert_id)
        if raced is not None and raced["content_hash"] == h:
            return _cert_view(raced), True
        raise DomainError(
            409, "cert_conflict",
            f"证明号 {data.cert_id} 并发写入且内容冲突",
        )

    row = conn.execute(
        "SELECT * FROM temp_calibration_cert WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return _cert_view(row), False


# --------------------------------------------------------------------------- 证明作废 / 查询

def void_certificate(
    conn: sqlite3.Connection, cert_id: str, data: TempCertVoidIn
) -> tuple[dict[str, Any], bool]:
    """按证明号附理由作废。返回 (证明视图, 是否幂等重试)。

    未知证明号 404；已作废证明同理由重试幂等返回原结果（200），异理由 409。
    作废后证明不再覆盖读数、不参与重叠判定；证明行永久留痕（不删除）。
    """
    row = _get_cert(conn, cert_id)
    if row is None:
        raise DomainError(
            404, "cert_not_found",
            f"校准证明不存在: {cert_id}",
            {"cert_id": cert_id},
        )
    if row["voided_at"] is not None:
        if row["void_reason"] == data.reason:
            return _cert_view(row), True
        raise DomainError(
            409,
            "cert_void_conflict",
            f"证明 {cert_id} 已于 {row['voided_at']} 作废，本次提交的作废理由与留痕不一致",
            {"cert_id": cert_id,
             "existing_void_reason": row["void_reason"],
             "incoming_void_reason": data.reason},
        )
    conn.execute(
        "UPDATE temp_calibration_cert SET voided_at = ?, void_reason = ? WHERE id = ?",
        (utcnow_iso(), data.reason, row["id"]),
    )
    return _cert_view(_get_cert(conn, cert_id)), False


def get_certificate(conn: sqlite3.Connection, cert_id: str) -> dict[str, Any]:
    """按证明号查询（含作废状态与理由留痕）；未知证明号 404。"""
    row = _get_cert(conn, cert_id)
    if row is None:
        raise DomainError(
            404, "cert_not_found",
            f"校准证明不存在: {cert_id}",
            {"cert_id": cert_id},
        )
    return _cert_view(row)


# --------------------------------------------------------------------------- 可信测温复核查询

def _match_cert(certs: list[sqlite3.Row], occurred_at: str) -> sqlite3.Row | None:
    """读数时刻匹配的有效证明：左闭右开 [valid_from, valid_until)。"""
    for c in certs:
        if c["valid_from"] <= occurred_at < c["valid_until"]:
            return c
    return None


def get_trusted_temperature_review(conn: sqlite3.Connection, sample_code: str) -> dict[str, Any]:
    """按样本的可信测温复核：逐条列出读数匹配的证明号或缺证原因，
    仅用有证读数计算超温点与相邻有证读数的缺测区间（沿用现有保管链拆分与归属）。

    证明匹配在查询时即时计算：补录或作废证明后再次查询即反映最新覆盖；
    原始读数、现有测温查询、保管链版本与已封存案卷均不受影响。
    """
    sample = get_sample_or_404(conn, sample_code)
    config = _get_config_row(conn, sample["id"])
    if config is None:
        raise DomainError(
            404, "temp_config_not_found",
            f"样本 {sample_code} 尚未设定测温配置",
            {"sample_code": sample_code},
        )

    readings = conn.execute(
        "SELECT * FROM temp_reading WHERE sample_id = ? ORDER BY occurred_at ASC, id ASC",
        (sample["id"],),
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM custody_event WHERE sample_id = ? AND superseded_by IS NULL"
        " ORDER BY occurred_at ASC, id ASC",
        (sample["id"],),
    ).fetchall()
    periods = _custody_periods(sample, events)

    # 设备 → 当前有效证明（作废证明不再覆盖读数）
    certs_by_device: dict[str, list[sqlite3.Row]] = {}
    for device_id in sorted({r["device_id"] for r in readings}):
        certs_by_device[device_id] = conn.execute(
            "SELECT * FROM temp_calibration_cert WHERE device_id = ? AND voided_at IS NULL"
            " ORDER BY valid_from ASC, id ASC",
            (device_id,),
        ).fetchall()

    min_c = config["min_celsius"]
    max_c = config["max_celsius"]
    max_interval = config["max_interval_seconds"]

    reading_views: list[dict[str, Any]] = []
    trusted: list[tuple[sqlite3.Row, sqlite3.Row]] = []   # (读数行, 匹配证明行)，按发生时间有序
    for r in readings:
        certs = certs_by_device[r["device_id"]]
        matched = _match_cert(certs, r["occurred_at"])
        value = r["value_celsius"]
        if matched is not None:
            coverage = "covered"
            cert_view: dict[str, Any] | None = {
                "cert_id": matched["cert_id"],
                "device_id": matched["device_id"],
                "valid_from": matched["valid_from"],
                "valid_until": matched["valid_until"],
                "issuer": matched["issuer"],
            }
            uncovered_reason = None
            trusted.append((r, matched))
        else:
            coverage = "uncovered"
            cert_view = None
            if not certs:
                uncovered_reason = {
                    "code": "no_certificate_for_device",
                    "message": f"设备 {r['device_id']} 无有效校准证明（未登记或已全部作废），读数不参与评估",
                }
            else:
                uncovered_reason = {
                    "code": "reading_outside_certificate_period",
                    "message": f"读数时刻 {r['occurred_at']} 不在设备 {r['device_id']} 任何有效证明"
                               "有效期内（左闭右开），读数不参与评估",
                    "device_valid_periods": [
                        {"cert_id": c["cert_id"], "valid_from": c["valid_from"],
                         "valid_until": c["valid_until"]}
                        for c in certs
                    ],
                }
        reading_views.append({
            "reading_id": r["reading_id"],
            "device_id": r["device_id"],
            "occurred_at": r["occurred_at"],
            "value_celsius": value,
            "in_range": min_c <= value <= max_c,
            "recorded_at": r["created_at"],
            "coverage": coverage,
            "certificate": cert_view,
            "uncovered_reason": uncovered_reason,
        })

    # 超温点：仅统计有证读数（附匹配证明号与当时归属）
    out_of_range: list[dict[str, Any]] = []
    for r, cert in trusted:
        value = r["value_celsius"]
        if min_c <= value <= max_c:
            continue
        below = value < min_c
        out_of_range.append({
            "reading_id": r["reading_id"],
            "device_id": r["device_id"],
            "occurred_at": r["occurred_at"],
            "value_celsius": value,
            "direction": "below_min" if below else "above_max",
            "limit_celsius": min_c if below else max_c,
            "cert_id": cert["cert_id"],
            "attribution": _attribution_at(periods, r["occurred_at"]),
        })

    # 缺测区间：相邻有证读数间隔超过最大测温间隔；跨当前有效链移交/签收时刻的段须拆分
    trusted_rows = [r for r, _ in trusted]
    split_times = sorted({
        ev["occurred_at"] for ev in events if ev["event_type"] in ("transfer", "receive")
    })
    gaps: list[dict[str, Any]] = []
    for prev, nxt in zip(trusted_rows, trusted_rows[1:]):
        duration = _seconds_between(prev["occurred_at"], nxt["occurred_at"])
        if duration <= max_interval:
            continue
        bounds = [prev["occurred_at"]]
        bounds += [t for t in split_times if prev["occurred_at"] < t < nxt["occurred_at"]]
        bounds.append(nxt["occurred_at"])
        segments = [
            {
                "start": a,
                "end": b,
                "duration_seconds": _seconds_between(a, b),
                "attribution": _attribution_at(periods, a),
            }
            for a, b in zip(bounds, bounds[1:])
        ]
        gaps.append({
            "start": prev["occurred_at"],
            "end": nxt["occurred_at"],
            "duration_seconds": duration,
            "max_interval_seconds": max_interval,
            "before_reading_id": prev["reading_id"],
            "after_reading_id": nxt["reading_id"],
            "segments": segments,
        })

    evaluable = len(trusted_rows) > 0
    return {
        "sample_code": sample["sample_code"],
        "config": _config_view(config, sample["sample_code"]),
        "reading_count": len(reading_views),
        "trusted_reading_count": len(trusted_rows),
        "evaluable": evaluable,
        "unevaluable_reason": None if evaluable else "没有有效证明覆盖的读数，无法评估超温点与缺测区间",
        "readings": reading_views,
        "out_of_range_points": out_of_range,
        "gap_intervals": gaps,
    }
