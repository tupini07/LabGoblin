"""Read-only operational/scientific projections; CSV is an export, not authority."""

import csv
import io
import json
import os
from pathlib import Path

from labgoblin.evidence import SizeLimitError, hash_file
from labgoblin.processes import unlink_file
from labgoblin.protocol import canonical, identifier, integer


COLUMNS = """a.id,a.generation,a.experiment_id,a.hypothesis_id,a.status,a.created,a.started,
    a.ended,a.exit_code,a.elapsed,a.gpu_hours,a.collection,a.validation,a.reason,a.collection_reason,
    a.collection_event,h.statement,h.label,e.payload AS collection_payload"""
JOINS = """FROM attempts a LEFT JOIN hypotheses h ON h.id=a.hypothesis_id
    LEFT JOIN events e ON e.id=a.collection_event"""


def _project(conn, row) -> dict:
    result = dict(row)
    payload = result.pop("collection_payload")
    ids = json.loads(payload)["observation_ids"] if payload else []
    records = []
    metrics = {}
    total_metrics = 0
    for oid in ids[:32]:
        value = conn.execute("SELECT id,kind,size,digest,assurance,metadata FROM observations WHERE id=?", (oid,)).fetchone()
        if value is None:
            raise ValueError(f"Collection references unavailable observation {oid}")
        metadata = json.loads(value["metadata"])
        records.append({key: value[key] for key in ("id", "kind", "size", "digest", "assurance")})
        if value["kind"] == "metrics":
            values = metadata.get("metrics", {})
            total_metrics += len(values)
            for name, number in values.items():
                candidate = {**metrics, name: number}
                if len(candidate) <= 32 and len(canonical(candidate)) <= 4096:
                    metrics = candidate
    result.update(observations=records, metrics=metrics,
                  observation_coverage={"total": len(ids), "returned": len(records), "has_more": len(ids) > len(records)},
                  metric_coverage={"total_in_returned_observations": total_metrics, "returned": len(metrics),
                                   "truncated": total_metrics > len(metrics)},
                  scientific_acceptance="not_inferred")
    return result


def page(db, *, generation=None, hypothesis_id=None, limit=25, offset=0) -> dict:
    integer(limit, "result page size")
    integer(offset, "result offset", zero=True)
    if limit > 100:
        raise ValueError("Result pages cannot exceed 100 attempts")
    conditions, params = [], []
    if generation is not None:
        conditions.append("a.generation=?")
        params.append(generation)
    if hypothesis_id is not None:
        conditions.append("a.hypothesis_id=?")
        params.append(hypothesis_id)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with db.read() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM attempts a{where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT {COLUMNS} {JOINS}{where} ORDER BY a.created,a.id LIMIT ? OFFSET ?",
                            (*params, limit, offset)).fetchall()
        values = [_project(conn, row) for row in rows]
    return {"attempts": values, "total": total, "offset": offset, "limit": limit,
            "has_more": offset + len(values) < total}


def hypothesis(db, hypothesis_id: str, *, limit=25, offset=0) -> dict:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown hypothesis")
    return {**dict(row), "metadata": json.loads(row["metadata"]),
            "results": page(db, hypothesis_id=hypothesis_id, limit=limit, offset=offset)}


def export(db, target: Path, *, generation=None, max_bytes=256 * 1024 * 1024) -> dict:
    integer(max_bytes, "CSV byte limit")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{identifier()}.tmp")
    fields = ["id", "generation", "experiment_id", "hypothesis_id", "statement", "status",
              "exit_code", "elapsed", "gpu_hours", "collection", "validation", "reason",
              "collection_reason", "collection_event", "observation_ids", "scientific_acceptance"]
    count = size = 0
    try:
        with db.read() as conn, temporary.open("xb") as stream:
            where, params = (" WHERE a.generation=?", (generation,)) if generation is not None else ("", ())
            rows = conn.execute(f"SELECT {COLUMNS} {JOINS}{where} ORDER BY a.created,a.id", params)

            def write(row=None):
                nonlocal size
                buffer = io.StringIO(newline="")
                writer = csv.DictWriter(buffer, fields, extrasaction="ignore")
                writer.writeheader() if row is None else writer.writerow(row)
                body = buffer.getvalue().encode("utf-8")
                if size + len(body) > max_bytes:
                    raise SizeLimitError(f"CSV export exceeds its explicit {max_bytes}-byte allowance")
                stream.write(body)
                size += len(body)

            write()
            while batch := rows.fetchmany(100):
                for row in batch:
                    data = dict(row)
                    payload = data.pop("collection_payload")
                    data["observation_ids"] = json.dumps(json.loads(payload)["observation_ids"] if payload else [])
                    data["scientific_acceptance"] = "not_inferred"
                    write(data)
                    count += 1
            stream.flush()
            os.fsync(stream.fileno())
        digest = hash_file(temporary, max_bytes)
        os.link(temporary, target)
    finally:
        unlink_file(temporary, missing_ok=True)
    return {"path": str(target), "attempts": count, "bytes": size, "sha256": digest,
            "generation": generation, "consistency": "one read-only SQLite snapshot"}
