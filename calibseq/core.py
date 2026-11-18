"""校准分组序贯判定核心业务：开案、补报、判定、恢复。

统计口径
--------
- 开案时固定：各温区容差 [lower, upper]、每轮累计样本量、整案误放风险上限 alpha。
- 风险分配：alpha 按轮次与温区做 Bonferroni 均分——每轮 alpha_round = alpha / R，
  每温区 alpha_zone = alpha_round / Z。由并集界，任意轮次任意温区
  “区间漏掉真值且误判放行”的总概率 <= sum(alpha_{r,z}) = alpha。
- 判定：每温区用截至封存的全部累计读数，计算均值的 (1 - alpha_zone) 双侧 t 置信区间；
  所有温区区间均落在各自容差内 -> 放行（PASS）；
  否则未达末轮 -> 加测进入下一轮（CONTINUE）；末轮仍不齐 -> 转人工复校（MANUAL_REVIEW）。
- 风险分配是 (开案配置, 轮次) 的纯函数，与判定执行次数无关：
  计算中断后按封存读数恢复、重试不会新增风险额度占用。

并发语义
--------
- 判定分两段：先在事务内封存当前读数快照（SEALED，提交即持久化），再据快照计算定案。
  封存期间到达的补报不影响本次判定，计入后续轮次。
- 定案（RELEASED / MANUAL_REVIEW）后拒绝一切补报。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from . import stats
from .store import tx

STATUS_OPEN = "OPEN"
STATUS_RELEASED = "RELEASED"
STATUS_MANUAL = "MANUAL_REVIEW"

DECISION_PASS = "PASS"
DECISION_CONTINUE = "CONTINUE"
DECISION_MANUAL = "MANUAL_REVIEW"

MAX_ZONES = 24
MAX_ROUNDS = 12
MIN_CUM_N = 2
MAX_CUM_N = 100000
MIN_RISK = 1e-6
MAX_RISK = 0.5
MAX_BATCH = 5000
RECORD_ID_RE = re.compile(r"^[\w.\-]{1,64}$")
ZONE_ID_RE = re.compile(r"^[\w.\-]{1,32}$")


class DomainError(Exception):
    """业务错误基类，携带 HTTP 状态码与机器可读 code。"""

    code = "DOMAIN_ERROR"
    http_status = 400


class NotFound(DomainError):
    code = "NOT_FOUND"
    http_status = 404


class Validation(DomainError):
    code = "VALIDATION"
    http_status = 400


class CaseDecided(DomainError):
    code = "CASE_DECIDED"
    http_status = 409


class Conflict(DomainError):
    code = "CONFLICT"
    http_status = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- 校验


def _finite(value, field: str) -> float:
    if isinstance(value, bool):
        raise Validation(f"{field} 必须是有限数字")
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise Validation(f"{field} 必须是有限数字")
    if not math.isfinite(x):
        raise Validation(f"{field} 必须是有限数字")
    return x


def _validate_risk(risk) -> float:
    r = _finite(risk, "整案误放风险上限 overall_risk")
    if not (MIN_RISK <= r <= MAX_RISK):
        raise Validation(f"整案误放风险上限须在 [{MIN_RISK}, {MAX_RISK}] 内")
    return r


def _validate_zones(zones) -> list[dict]:
    if not isinstance(zones, list) or not (1 <= len(zones) <= MAX_ZONES):
        raise Validation(f"温区数量须在 1..{MAX_ZONES} 之间")
    out, seen = [], set()
    for i, z in enumerate(zones, 1):
        if not isinstance(z, dict):
            raise Validation("温区项必须是对象")
        zid = str(z.get("zone_id") or f"Z{i}").strip()
        if not ZONE_ID_RE.fullmatch(zid):
            raise Validation(f"温区编号非法（1..32 位字母数字._-）: {zid!r}")
        if zid in seen:
            raise Validation(f"温区编号重复: {zid}")
        seen.add(zid)
        name = str(z.get("name") or zid).strip()[:80] or zid
        lower = _finite(z.get("lower"), f"温区 {zid} 容差下限")
        upper = _finite(z.get("upper"), f"温区 {zid} 容差上限")
        if not lower < upper:
            raise Validation(f"温区 {zid} 容差下限必须小于上限")
        out.append({"zone_id": zid, "name": name, "lower": lower, "upper": upper})
    return out


def _validate_rounds(rounds) -> list[int]:
    if not isinstance(rounds, list) or not (1 <= len(rounds) <= MAX_ROUNDS):
        raise Validation(f"轮次数量须在 1..{MAX_ROUNDS} 之间")
    out, prev = [], 0
    for n in rounds:
        if isinstance(n, bool) or not isinstance(n, int) or not (MIN_CUM_N <= n <= MAX_CUM_N):
            raise Validation(f"每轮累计样本量须为 [{MIN_CUM_N}, {MAX_CUM_N}] 内的整数: {n!r}")
        if n <= prev:
            raise Validation("每轮累计样本量必须严格递增")
        out.append(n)
        prev = n
    return out


def risk_allocation(overall_risk: float, n_rounds: int, n_zones: int) -> tuple[float, float]:
    """Bonferroni 均分：返回 (每轮风险, 每温区每轮风险)。纯函数，重试不改变结果。"""
    per_round = overall_risk / n_rounds
    return per_round, per_round / n_zones


# ---------------------------------------------------------------- 开案


def create_case(conn, *, name, overall_risk, zones, rounds, now: str | None = None) -> dict:
    now = now or _now()
    name = str(name or "").strip()
    if not (1 <= len(name) <= 80):
        raise Validation("案件名称须为 1..80 字符")
    risk = _validate_risk(overall_risk)
    zrows = _validate_zones(zones)
    rrows = _validate_rounds(rounds)
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO cases(code, name, overall_risk, status, current_round, created_at)"
            " VALUES (NULL, ?, ?, 'OPEN', 1, ?)",
            (name, risk, now),
        )
        cid = cur.lastrowid
        conn.execute("UPDATE cases SET code = ? WHERE id = ?", (f"CASE-{cid:04d}", cid))
        for z in zrows:
            conn.execute(
                "INSERT INTO zones(case_id, zone_id, name, lower, upper) VALUES (?,?,?,?,?)",
                (cid, z["zone_id"], z["name"], z["lower"], z["upper"]),
            )
        for i, n in enumerate(rrows, 1):
            conn.execute(
                "INSERT INTO rounds(case_id, round_no, cumulative_n) VALUES (?,?,?)",
                (cid, i, n),
            )
    return get_case_detail(conn, cid)


# ---------------------------------------------------------------- 查询


def _get_case(conn, case_id: int):
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        raise NotFound(f"案件不存在: {case_id}")
    return row


def _run_to_dict(run) -> dict:
    snapshot = json.loads(run["snapshot"])
    return {
        "id": run["id"],
        "case_id": run["case_id"],
        "round_no": run["round_no"],
        "status": run["status"],
        "quota": run["quota"],
        "alpha_round": run["alpha_round"],
        "snapshot_counts": {z: len(v) for z, v in snapshot.items()},
        "result": json.loads(run["result"]) if run["result"] else None,
        "decision": run["decision"],
        "sealed_at": run["sealed_at"],
        "completed_at": run["completed_at"],
    }


def list_cases(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, "
        " (SELECT COUNT(*) FROM measurements m WHERE m.case_id = c.id) AS n_measurements,"
        " (SELECT COUNT(*) FROM judgment_runs r WHERE r.case_id = c.id) AS n_runs"
        " FROM cases c ORDER BY c.id DESC"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "code": r["code"],
            "name": r["name"],
            "overall_risk": r["overall_risk"],
            "status": r["status"],
            "current_round": r["current_round"],
            "created_at": r["created_at"],
            "decided_at": r["decided_at"],
            "n_measurements": r["n_measurements"],
            "n_runs": r["n_runs"],
        }
        for r in rows
    ]


def get_case_detail(conn, case_id: int) -> dict:
    case = _get_case(conn, case_id)
    cid = case["id"]
    zones = conn.execute(
        "SELECT * FROM zones WHERE case_id = ? ORDER BY zone_id", (cid,)
    ).fetchall()
    rounds = conn.execute(
        "SELECT * FROM rounds WHERE case_id = ? ORDER BY round_no", (cid,)
    ).fetchall()
    counts = dict(
        conn.execute(
            "SELECT zone_id, COUNT(*) FROM measurements WHERE case_id = ? GROUP BY zone_id",
            (cid,),
        ).fetchall()
    )
    per_round, per_zone = risk_allocation(case["overall_risk"], len(rounds), len(zones))
    runs = conn.execute(
        "SELECT * FROM judgment_runs WHERE case_id = ? ORDER BY id", (cid,)
    ).fetchall()
    measurements = conn.execute(
        "SELECT record_id, zone_id, value, ts FROM measurements WHERE case_id = ? ORDER BY rowid",
        (cid,),
    ).fetchall()
    current_quota = (
        rounds[case["current_round"] - 1]["cumulative_n"]
        if case["status"] == STATUS_OPEN
        else None
    )
    return {
        "id": cid,
        "code": case["code"],
        "name": case["name"],
        "overall_risk": case["overall_risk"],
        "status": case["status"],
        "current_round": case["current_round"],
        "total_rounds": len(rounds),
        "current_quota": current_quota,
        "created_at": case["created_at"],
        "decided_at": case["decided_at"],
        "allocation": {
            "overall": case["overall_risk"],
            "per_round": per_round,
            "per_zone": per_zone,
            "note": "整案误放风险按轮次与温区均分（Bonferroni 并集界），各轮各温区共用同一整案上限",
        },
        "zones": [
            {
                "zone_id": z["zone_id"],
                "name": z["name"],
                "lower": z["lower"],
                "upper": z["upper"],
                "count": counts.get(z["zone_id"], 0),
            }
            for z in zones
        ],
        "rounds": [
            {"round_no": r["round_no"], "cumulative_n": r["cumulative_n"], "alpha_round": per_round}
            for r in rounds
        ],
        "measurements": [dict(m) for m in measurements],
        "runs": [_run_to_dict(r) for r in runs],
        "sealed_pending": any(r["status"] == "SEALED" for r in runs),
    }


# ---------------------------------------------------------------- 补报


def report_measurements(conn, case_id: int, records, now: str | None = None) -> dict:
    """批量补报测量记录。编号 (record_id) 在案件内唯一：

    - 新编号 -> stored；同编号同内容 -> duplicate（幂等重报，不报错）；
    - 同编号不同内容 -> conflict；字段非法 -> invalid。
    定案后整批拒绝（CASE_DECIDED）。
    """
    now = now or _now()
    if not isinstance(records, list) or not records:
        raise Validation("records 必须是非空数组")
    if len(records) > MAX_BATCH:
        raise Validation(f"单次补报不超过 {MAX_BATCH} 条")
    with tx(conn):
        case = _get_case(conn, case_id)
        if case["status"] != STATUS_OPEN:
            raise CaseDecided(f"案件已定案（{case['status']}），拒绝补报")
        cid = case["id"]
        zone_ids = {r[0] for r in conn.execute("SELECT zone_id FROM zones WHERE case_id = ?", (cid,))}
        results = []
        for rec in records:
            if not isinstance(rec, dict):
                results.append({"record_id": None, "status": "invalid", "reason": "记录必须是对象"})
                continue
            rid = rec.get("record_id")
            zid = rec.get("zone_id")
            val = rec.get("value")
            if rid is None:
                rid = f"A-{uuid.uuid4().hex[:12]}"
            elif not (isinstance(rid, str) and RECORD_ID_RE.fullmatch(rid)):
                results.append({"record_id": str(rid)[:80], "status": "invalid",
                                "reason": "编号须为 1..64 位字母数字._-"})
                continue
            if zid not in zone_ids:
                results.append({"record_id": rid, "status": "invalid", "reason": f"温区不存在: {zid}"})
                continue
            try:
                v = _finite(val, "测量值")
            except Validation as e:
                results.append({"record_id": rid, "status": "invalid", "reason": str(e)})
                continue
            try:
                conn.execute(
                    "INSERT INTO measurements(case_id, record_id, zone_id, value, ts)"
                    " VALUES (?,?,?,?,?)",
                    (cid, rid, zid, v, now),
                )
                results.append({"record_id": rid, "status": "stored"})
            except sqlite3.IntegrityError:
                old = conn.execute(
                    "SELECT zone_id, value FROM measurements WHERE case_id = ? AND record_id = ?",
                    (cid, rid),
                ).fetchone()
                if old and old["zone_id"] == zid and old["value"] == v:
                    results.append({"record_id": rid, "status": "duplicate"})
                else:
                    results.append({"record_id": rid, "status": "conflict",
                                    "reason": "编号已存在且内容不一致"})
        counts = dict(
            conn.execute(
                "SELECT zone_id, COUNT(*) FROM measurements WHERE case_id = ? GROUP BY zone_id",
                (cid,),
            ).fetchall()
        )
    return {"results": results, "counts": counts}


# ---------------------------------------------------------------- 判定


def seal_for_judgment(conn, case_id: int, now: str | None = None) -> dict:
    """判定第一段：齐量检查后在事务内封存当前读数快照（提交即持久化）。

    返回 outcome:
    - sealed:   新封存完成，携带 run_id
    - pending:  已存在未完成的封存（上次判定中断），携带 run_id
    - insufficient: 有温区未达本轮累计样本量，携带 shortfall
    - decided:  案件已定案
    """
    now = now or _now()
    with tx(conn):
        case = _get_case(conn, case_id)
        if case["status"] != STATUS_OPEN:
            return {"outcome": "decided", "status": case["status"], "case_id": case_id}
        cid = case["id"]
        run = conn.execute(
            "SELECT id FROM judgment_runs WHERE case_id = ? AND status = 'SEALED' ORDER BY id",
            (cid,),
        ).fetchone()
        if run is not None:
            return {"outcome": "pending", "run_id": run["id"], "case_id": cid}
        round_no = case["current_round"]
        quota = conn.execute(
            "SELECT cumulative_n FROM rounds WHERE case_id = ? AND round_no = ?",
            (cid, round_no),
        ).fetchone()["cumulative_n"]
        zone_rows = conn.execute(
            "SELECT zone_id FROM zones WHERE case_id = ? ORDER BY zone_id", (cid,)
        ).fetchall()
        counts = dict(
            conn.execute(
                "SELECT zone_id, COUNT(*) FROM measurements WHERE case_id = ? GROUP BY zone_id",
                (cid,),
            ).fetchall()
        )
        shortfall = {
            z["zone_id"]: quota - counts.get(z["zone_id"], 0)
            for z in zone_rows
            if counts.get(z["zone_id"], 0) < quota
        }
        if shortfall:
            return {
                "outcome": "insufficient",
                "round": round_no,
                "quota": quota,
                "counts": {z["zone_id"]: counts.get(z["zone_id"], 0) for z in zone_rows},
                "shortfall": shortfall,
            }
        snapshot = {}
        for z in zone_rows:
            rows = conn.execute(
                "SELECT record_id, value FROM measurements"
                " WHERE case_id = ? AND zone_id = ? ORDER BY record_id",
                (cid, z["zone_id"]),
            ).fetchall()
            snapshot[z["zone_id"]] = [{"record_id": r["record_id"], "value": r["value"]} for r in rows]
        n_rounds = conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE case_id = ?", (cid,)
        ).fetchone()[0]
        alpha_round, _ = risk_allocation(case["overall_risk"], n_rounds, len(zone_rows))
        cur = conn.execute(
            "INSERT INTO judgment_runs(case_id, round_no, status, quota, alpha_round, snapshot, sealed_at)"
            " VALUES (?,?, 'SEALED', ?,?,?,?)",
            (cid, round_no, quota, alpha_round, json.dumps(snapshot, ensure_ascii=False), now),
        )
        return {"outcome": "sealed", "run_id": cur.lastrowid, "case_id": cid}


def finalize_run(conn, run_id: int, now: str | None = None) -> dict:
    """判定第二段：按封存快照计算各温区区间并定案。

    幂等：已完成的运行直接返回原结果（replayed=True），不新增风险额度占用。
    """
    now = now or _now()
    with tx(conn):
        run = conn.execute("SELECT * FROM judgment_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise NotFound(f"判定运行不存在: {run_id}")
        if run["status"] == "COMPLETED":
            return {"outcome": "completed", "run": _run_to_dict(run), "replayed": True}
        case = _get_case(conn, run["case_id"])
        if case["status"] != STATUS_OPEN or case["current_round"] != run["round_no"]:
            raise Conflict("案件状态与封存轮次不一致，无法定案")
        cid = case["id"]
        zones = conn.execute(
            "SELECT * FROM zones WHERE case_id = ? ORDER BY zone_id", (cid,)
        ).fetchall()
        n_rounds = conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE case_id = ?", (cid,)
        ).fetchone()[0]
        alpha_zone = run["alpha_round"] / len(zones)
        snapshot = json.loads(run["snapshot"])
        zone_results, all_within = [], True
        for z in zones:
            vals = [p["value"] for p in snapshot[z["zone_id"]]]
            ci = stats.mean_confidence_interval(vals, alpha_zone)
            within = ci["lo"] >= z["lower"] and ci["hi"] <= z["upper"]
            all_within = all_within and within
            zone_results.append({
                "zone_id": z["zone_id"],
                "name": z["name"],
                "n": ci["n"],
                "mean": ci["mean"],
                "sd": ci["sd"],
                "alpha": alpha_zone,
                "ci_lo": ci["lo"],
                "ci_hi": ci["hi"],
                "lower": z["lower"],
                "upper": z["upper"],
                "within": within,
            })
        if all_within:
            decision = DECISION_PASS
        elif run["round_no"] == n_rounds:
            decision = DECISION_MANUAL
        else:
            decision = DECISION_CONTINUE
        result = {
            "round_no": run["round_no"],
            "quota": run["quota"],
            "alpha_round": run["alpha_round"],
            "alpha_zone": alpha_zone,
            "zones": zone_results,
            "all_within": all_within,
            "decision": decision,
        }
        conn.execute(
            "UPDATE judgment_runs SET status = 'COMPLETED', result = ?, decision = ?,"
            " completed_at = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), decision, now, run_id),
        )
        if decision == DECISION_PASS:
            conn.execute(
                "UPDATE cases SET status = ?, decided_at = ? WHERE id = ?",
                (STATUS_RELEASED, now, cid),
            )
        elif decision == DECISION_MANUAL:
            conn.execute(
                "UPDATE cases SET status = ?, decided_at = ? WHERE id = ?",
                (STATUS_MANUAL, now, cid),
            )
        else:
            conn.execute(
                "UPDATE cases SET current_round = ? WHERE id = ?",
                (run["round_no"] + 1, cid),
            )
        run = conn.execute("SELECT * FROM judgment_runs WHERE id = ?", (run_id,)).fetchone()
        return {"outcome": "completed", "run": _run_to_dict(run), "replayed": False}


def judge(conn, case_id: int, now: str | None = None) -> dict:
    """尝试对当前轮判定：齐量则封存并定案；已有封存则续算（重试安全）。"""
    now = now or _now()
    seal = seal_for_judgment(conn, case_id, now=now)
    if seal["outcome"] not in ("sealed", "pending"):
        return seal
    return finalize_run(conn, seal["run_id"], now=now)


def resume(conn, case_id: int | None = None, now: str | None = None) -> dict:
    """恢复被中断的判定：完成所有 SEALED 运行（可按案件过滤）。

    服务启动时会对全库执行一次；恢复只使用封存读数，不新增风险额度占用。
    """
    now = now or _now()
    if case_id is not None:
        _get_case(conn, case_id)
        rows = conn.execute(
            "SELECT id FROM judgment_runs WHERE status = 'SEALED' AND case_id = ? ORDER BY id",
            (case_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM judgment_runs WHERE status = 'SEALED' ORDER BY id"
        ).fetchall()
    resumed = []
    for row in rows:
        finalize_run(conn, row["id"], now=now)
        resumed.append(row["id"])
    return {"resumed": resumed}
