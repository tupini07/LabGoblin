"""Immutable scientific source views and their bounded historical projections."""

import hashlib
import html
import json
import math
from pathlib import Path
import time

from xgenius.evidence import contained, observation, publish_stream
from xgenius.protocol import AdmissionWait, PACKET_BYTES, canonical, fingerprint, identifier, integer, table, text
from xgenius.state import _active_directives, _campaign, _source_revision


def selection_options(selected=None, selection_reason="Complete generation inventory"):
    if selected is not None and (not isinstance(selected, list) or len(selected) > 1000
                                 or any(not isinstance(value, str) for value in selected)):
        raise ValueError("Selected attempts must be a bounded list of IDs")
    if not isinstance(selection_reason, str) or not selection_reason.strip() or len(selection_reason.encode()) > 4096:
        raise ValueError("An explicit bounded selection reason is required")
    if selected is not None and selection_reason == "Complete generation inventory":
        raise ValueError("Selecting a subset requires its explicit selection reason")
    return {"selected": sorted(set(selected)) if selected is not None else None, "selection_reason": selection_reason}


def seal_view(state, *, kind="report", selected=None, selection_reason="Complete generation inventory", maintenance_id=None) -> dict:
    if kind not in ("report", "closure"):
        raise ValueError("Source views must be report or closure inventories")
    options = selection_options(selected, selection_reason)
    selected, selection_reason = options["selected"], options["selection_reason"]
    with state.db.write() as conn:
        campaign = _campaign(conn)
        generation = conn.execute("SELECT * FROM generations WHERE id=?", (campaign["generation"],)).fetchone()
        revision = _source_revision(conn)
        if maintenance_id:
            request = conn.execute("SELECT * FROM maintenance WHERE id=? AND kind='report' AND state='pending'",
                                   (maintenance_id,)).fetchone()
            if not request or json.loads(request["options"]) != options:
                raise ValueError("Report view does not belong to its pending scoped request")
            if request["source_revision"] != revision:
                raise AdmissionWait("Report sources changed; coalesce the pending request before publication")
            if request["view_id"]:
                return _header(conn.execute("SELECT * FROM views WHERE id=?", (request["view_id"],)).fetchone())
        if kind == "closure":
            if generation["state"] == "open":
                raise ValueError("A closure view requires sealed admission")
            if generation["view_id"]:
                prior = conn.execute("SELECT * FROM views WHERE id=?", (generation["view_id"],)).fetchone()
                if generation["final_turn"] or json.loads(prior["metadata"])["source_revision"] == revision:
                    return _header(prior)
            heads = json.loads(generation["source_heads"])
            authority = generation["authority_revision"]
            join = "JOIN closure_members m ON m.attempt_id=a.id AND m.generation=a.generation"
        else:
            heads = dict(conn.execute("""SELECT name,source_id FROM source_heads
                WHERE name IN ('goal','protocol','rationale','summary')"""))
            heads.update({f"directive:{r['id']}": r["source_id"] for r in _active_directives(conn, campaign["generation"])})
            authority, join = campaign["authority_revision"], ""
        watermark = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        metadata = {"source_revision": revision, "authority_revision": authority,
                    "selection_reason": selection_reason, "selected": selected,
                    "research_assessment": "not_inferred", "attempts": 0, "operationally_ready": True,
                    "archive_cutoff": conn.execute("SELECT COALESCE(MAX(seq),0) FROM sources").fetchone()[0],
                    "follow_up_authority_revision": campaign["authority_revision"]}
        view_id = identifier()
        conn.execute("""INSERT INTO views(id,kind,generation,watermark,source_ids,metadata,created)
            VALUES(?,?,?,?,?,?,?)""", (view_id, kind, campaign["generation"], watermark,
                                      canonical(heads).decode(), "{}", time.time()))
        conn.executemany("INSERT OR IGNORE INTO view_sources(view_id,source_id) VALUES(?,?)",
                         [(view_id, value) for value in heads.values()])
        digest = hashlib.sha256(canonical({"kind": kind, "generation": campaign["generation"],
                                           "sources": heads, "watermark": watermark, "selection_reason": selection_reason}))
        rows = conn.execute(f"""SELECT a.id,a.experiment_id,a.hypothesis_id,a.status,a.exit_code,
            a.elapsed,a.gpu_hours,a.reason,a.collection,a.collection_reason,a.validation,a.collection_event,
            a.started,a.ended,a.nonce,a.request_digest,
            json_extract(a.spec,'$.hypothesis_description') AS statement,
            json_extract(a.spec,'$.source_refs') AS source_refs,
            e.payload AS collection_payload
            FROM attempts a {join} LEFT JOIN events e ON e.id=a.collection_event
            WHERE a.generation=? ORDER BY a.id""", (campaign["generation"],))
        count = 0
        selected_found = set()
        while batch := rows.fetchmany(100):
            for row in batch:
                record = dict(row)
                payload = record.pop("collection_payload")
                observations = json.loads(payload)["observation_ids"] if payload else []
                record["source_refs"] = json.loads(record["source_refs"]) if record["source_refs"] else {}
                conn.executemany("INSERT OR IGNORE INTO view_sources(view_id,source_id) VALUES(?,?)",
                                 [(view_id, value) for value in record["source_refs"].values()])
                record["selected"] = selected is None or row["id"] in selected
                if selected is not None and record["selected"]:
                    selected_found.add(row["id"])
                record["observation_ids"] = observations
                record["admitted"] = row["nonce"] is not None
                ready = row["status"] not in ("queued", "starting", "running", "recovery_required") and row["collection"] != "pending"
                metadata["operationally_ready"] = metadata["operationally_ready"] and ready
                encoded = canonical(record)
                row_digest = hashlib.sha256(encoded).hexdigest()
                digest.update(canonical([count, row["id"], row_digest]))
                conn.execute("""INSERT INTO view_members(view_id,attempt_id,ordinal,outcome,observation_ids,digest)
                    VALUES(?,?,?,?,?,?)""", (view_id, row["id"], count, encoded.decode(), canonical(observations).decode(), row_digest))
                count += 1
        if selected is not None and set(selected) != selected_found:
            raise ValueError("Selection refers to attempts outside the complete scoped inventory")
        for source in conn.execute("SELECT s.id,s.digest FROM view_sources v JOIN sources s ON s.id=v.source_id WHERE v.view_id=? ORDER BY s.id",
                                   (view_id,)):
            digest.update(canonical(["source", source["id"], source["digest"]]))
        metadata.update(attempts=count, inventory_digest=digest.hexdigest(),
                        source_count=conn.execute("SELECT COUNT(*) FROM view_sources WHERE view_id=?", (view_id,)).fetchone()[0])
        conn.execute("UPDATE views SET metadata=? WHERE id=?", (canonical(metadata).decode(), view_id))
        if kind == "closure":
            conn.execute("UPDATE generations SET view_id=? WHERE id=?", (view_id, campaign["generation"]))
        if maintenance_id:
            conn.execute("UPDATE maintenance SET view_id=? WHERE id=?", (view_id, maintenance_id))
        return _header(conn.execute("SELECT * FROM views WHERE id=?", (view_id,)).fetchone())


def _header(row):
    if row is None:
        raise ValueError("Unknown retained source view")
    return {**dict(row), "source_ids": json.loads(row["source_ids"]), "metadata": json.loads(row["metadata"])}


def _page(conn, view_id, *, offset=0, limit=25, byte_limit=PACKET_BYTES):
    value = _header(conn.execute("SELECT * FROM views WHERE id=?", (view_id,)).fetchone())
    total = value["metadata"]["attempts"]
    values = []
    for row in conn.execute("""SELECT ordinal,outcome,digest FROM view_members WHERE view_id=?
        AND ordinal>=? ORDER BY ordinal LIMIT ?""", (view_id, offset, limit)):
        record = json.loads(row["outcome"])
        if fingerprint(record) != row["digest"]:
            raise ValueError("Retained view member integrity failed")
        preview = {key: record[key] for key in ("id", "status", "validation", "collection", "collection_event", "selected", "admitted")}
        for key in ("statement", "reason", "collection_reason", "experiment_id"):
            preview[key] = (record[key] or "")[:256]
        observations = []
        for oid in record["observation_ids"][:8]:
            observation = conn.execute("SELECT id,kind,size,digest,assurance,metadata FROM observations WHERE id=?", (oid,)).fetchone()
            if observation is None:
                raise ValueError("Retained view observation is unavailable")
            metadata = json.loads(observation["metadata"])
            item = {key: observation[key] for key in ("id", "kind", "size", "digest", "assurance")}
            metrics = metadata.get("metrics", {})
            sample = {}
            for key, metric in metrics.items():
                if len(sample) >= 16 or len(canonical({**sample, key: metric})) > 512:
                    break
                sample[key] = metric
            item.update(metrics=sample, metrics_truncated=len(sample) < len(metrics))
            observations.append(item)
        preview.update(ordinal=row["ordinal"], row_digest=row["digest"], observations=observations,
                       source_refs=dict(list(record["source_refs"].items())[:8]), source_ref_count=len(record["source_refs"]),
                       observation_count=len(record["observation_ids"]),
                       detail="Bounded preview; exact retained row and observations are separately retrievable")
        if len(canonical({**value, "attempts": [*values, preview]})) > byte_limit - 512:
            break
        values.append(preview)
    if offset < total and not values:
        raise ValueError("Source view header/row does not fit this response allowance")
    return {**value, "attempts": values, "coverage": {"total": total, "offset": offset,
            "end": offset + len(values), "has_more": offset + len(values) < total},
            "retrieved_at": time.time()}


def page(db, view_id, *, offset=0, limit=25):
    integer(offset, "view offset", zero=True)
    integer(limit, "view page size")
    if limit > 100:
        raise ValueError("Source view pages cannot exceed 100 attempts")
    with db.read() as conn:
        return _page(conn, view_id, offset=offset, limit=limit)


def member(db, view_id, attempt_id, *, offset=0, limit=PACKET_BYTES):
    import base64
    integer(offset, "view row byte offset", zero=True)
    integer(limit, "view row byte limit")
    if limit > PACKET_BYTES:
        raise ValueError("Exact source view row pages cannot exceed 64 KiB")
    with db.read() as conn:
        row = conn.execute("""SELECT digest,length(CAST(outcome AS BLOB)) AS bytes,
            substr(CAST(outcome AS BLOB),?,?) AS body FROM view_members WHERE view_id=? AND attempt_id=?""",
                           (offset + 1, limit, view_id, attempt_id)).fetchone()
    if row is None:
        raise ValueError("Attempt does not belong to this source view")
    body = bytes(row["body"])
    complete = offset == 0 and len(body) == row["bytes"]
    if complete and hashlib.sha256(body).hexdigest() != row["digest"]:
        raise ValueError("Retained view member integrity failed")
    return {"view_id": view_id, "attempt_id": attempt_id, "digest": row["digest"],
            "bytes": row["bytes"], "offset": offset, "has_more": offset + len(body) < row["bytes"],
            "base64": base64.b64encode(body).decode("ascii"), "content": json.loads(body) if complete else None}


def read_all(conn, turn_id, view_id):
    row = conn.execute("SELECT metadata FROM views WHERE id=?", (view_id,)).fetchone()
    if row is None:
        return False
    total = json.loads(row[0])["attempts"]
    end = 0
    for page in conn.execute("SELECT start,end FROM turn_view_pages WHERE turn_id=? AND view_id=? ORDER BY start,end",
                             (turn_id, view_id)):
        if page["start"] > end:
            break
        end = max(end, page["end"])
    return end >= total


def covered_finalize(conn, view_id):
    view = _header(conn.execute("SELECT * FROM views WHERE id=?", (view_id,)).fetchone())
    source = view["source_ids"].get("rationale")
    row = conn.execute("""SELECT t.id,t.result,p.content FROM handoffs h JOIN turns t ON t.id=h.turn_id
        JOIN packets p ON p.id=t.packet_id WHERE h.source_id=? AND t.kind='research' AND t.state='accepted'""",
                       (source,)).fetchone()
    if (not row or json.loads(row["result"])["disposition"] != "finalize"
            or json.loads(row["content"]).get("authority_revision", 0) != view["metadata"]["authority_revision"]):
        return None
    for member in conn.execute("SELECT outcome FROM view_members WHERE view_id=?", (view_id,)):
        item = json.loads(member[0])
        disposition = conn.execute("""SELECT disposition,reference_ids FROM dispositions WHERE turn_id=? AND event_id=?
            AND disposition IN ('assessed','excluded')""", (row["id"], item["collection_event"])).fetchone()
        if not disposition or set(item["observation_ids"]) - set(json.loads(disposition["reference_ids"])):
            return None
    return row["id"]


def valid_assessed_turn(conn, view_id, turn_id):
    row = conn.execute("SELECT kind,state,result FROM turns WHERE id=?", (turn_id,)).fetchone()
    if not row or row["state"] != "accepted":
        return False
    if row["kind"] == "research":
        return covered_finalize(conn, view_id) == turn_id
    result = json.loads(row["result"])
    assessment = result.get("assessment")
    view = _header(conn.execute("SELECT * FROM views WHERE id=?", (view_id,)).fetchone())
    return (row["kind"] == "final_analysis" and result["disposition"] == "finalize" and assessment
            and assessment["view_id"] == view_id
            and assessment["inventory_digest"] == view["metadata"]["inventory_digest"] and read_all(conn, turn_id, view_id))


def _validate_report(conn, value):
    fields = {"turn_id", "packet_id", "view_id", "title", "summary", "limitations", "claims", "references"}
    table(value, "report result", fields)
    if fields - set(value):
        raise ValueError("Report result is missing required fields")
    for key in fields - {"claims", "references"}:
        text(value[key], f"report.{key}")
    if len(canonical(value)) > 16384 or len(value["title"].encode()) > 256:
        raise ValueError("Report result exceeds its bounded result allowance")
    view = _header(conn.execute("SELECT * FROM views WHERE id=?", (value["view_id"],)).fetchone())
    if not isinstance(value["claims"], list) or not isinstance(value["references"], list):
        raise ValueError("Report claims and references must be arrays")
    allowed = {view["id"], *view["source_ids"].values()}
    for reference in value["references"]:
        text(reference, "report reference")
        if (reference not in allowed and not conn.execute("SELECT 1 FROM view_sources WHERE view_id=? AND source_id=?",
                                                         (view["id"], reference)).fetchone()
                and not conn.execute("""SELECT 1 FROM view_members m,json_each(m.observation_ids) j
                    WHERE m.view_id=? AND j.value=? LIMIT 1""", (view["id"], reference)).fetchone()):
            raise ValueError("Report reference is outside its immutable source view")
    for claim in value["claims"]:
        table(claim, "report numeric claim", {"observation_id", "metric", "value"})
        text(claim.get("observation_id"), "claim observation")
        text(claim.get("metric"), "claim metric")
        number = claim.get("value")
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError("Report values must be finite captured numbers")
        row = conn.execute("""SELECT o.metadata FROM view_members m,json_each(m.observation_ids) j
            JOIN observations o ON o.id=j.value WHERE m.view_id=? AND o.id=?
            AND json_extract(m.outcome,'$.selected')=1""", (view["id"], claim["observation_id"])).fetchone()
        if not row or json.loads(row["metadata"]).get("metrics", {}).get(claim["metric"]) != number:
            raise ValueError("Report numeric claim differs from captured evidence in the selected scope")
    return view


def _records(conn, view_id):
    for row in conn.execute("SELECT outcome,digest FROM view_members WHERE view_id=? ORDER BY ordinal", (view_id,)):
        value = json.loads(row["outcome"])
        if fingerprint(value) != row["digest"]:
            raise ValueError("Retained report inventory was modified")
        yield value


def _markdown(value):
    result = str(value)
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "#", "!", "|"):
        result = result.replace(character, "\\" + character)
    return result.replace("\n", " ")


def _chart(points, metric):
    if not points:
        return "<p>No captured numeric metric is available for a chart.</p>"
    scale = max(abs(value) for _, value in points) or 1
    scaled = [value / scale for _, value in points]
    low, high = min(0, *scaled), max(0, *scaled)
    span = high - low or 1
    x = lambda value: 200 + 480 * (value - low) / span
    rows = []
    for index, ((label, value), normalized) in enumerate(zip(points, scaled)):
        y = 35 + index * 30
        rows.append(f'<text x="8" y="{y+13}">{html.escape(label[:26])}</text>'
                    f'<rect x="{min(x(0),x(normalized)):.2f}" y="{y}" width="{abs(x(normalized)-x(0)):.2f}" '
                    f'height="19" fill="#427bd6"/><text x="695" y="{y+13}">{html.escape(str(value))}</text>')
    return (f'<figure><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 880 {65+30*len(points)}" role="img" '
            f'aria-label="{html.escape(metric, quote=True)}">'
            + "".join(rows) + "</svg><figcaption>"
            + f"First {len(points)} selected captured observations containing {html.escape(metric)}; "
            "the full attempt denominator is in the table.</figcaption></figure>")


def _render_outputs(state, view, value, report_id, *, max_bytes):
    import base64
    from xgenius import journal
    directory = contained(state.root, Path("reports") / report_id)
    with state.db.read() as conn:
        def sources():
            for row in conn.execute("SELECT source_id FROM view_sources WHERE view_id=? ORDER BY source_id", (view["id"],)):
                yield journal.entry(state.db, row["source_id"])
        points, figures, metric = [], [], None
        for record in _records(conn, view["id"]):
            if not record["selected"]:
                continue
            if len(points) >= 16 and len(figures) >= 8:
                break
            for oid in record["observation_ids"]:
                row = conn.execute("SELECT path,kind,metadata,assurance,size FROM observations WHERE id=?", (oid,)).fetchone()
                metadata = json.loads(row["metadata"])
                metrics = metadata.get("metrics", {})
                if metrics and len(points) < 16:
                    metric = metric or next(iter(metrics))
                    if metric in metrics:
                        points.append((record["experiment_id"], metrics[metric]))
                if (Path(row["path"]).suffix.lower() in (".png", ".jpg", ".jpeg") and row["assurance"] == "captured"
                        and row["size"] <= 1024 * 1024 and len(figures) < 8):
                    body = observation(state.db, oid)["body"]
                    mime = ("image/png" if body.startswith(b"\x89PNG\r\n\x1a\n") else
                            "image/jpeg" if body.startswith(b"\xff\xd8\xff") else None)
                    if mime:
                        figures.append((oid, mime, base64.b64encode(body).decode("ascii")))

        def html_chunks():
            yield ("""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'">
<title>""" + html.escape(value["title"]) + """</title><style>
body{font:16px system-ui,sans-serif;line-height:1.5;margin:3rem auto;padding:0 1rem;max-width:1100px;color:#243246;background:#f5f7fb}
h1,h2{color:#142940}section,figure{background:white;padding:1.3rem;border:1px solid #dce3ec;border-radius:12px}
table{border-collapse:collapse;width:100%;background:white;font-size:13px}th,td{padding:.6rem;border:1px solid #dce3ec;text-align:left;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere}img,svg{max-width:100%;height:auto}svg{font:12px system-ui}figcaption{font-size:13px;color:#59667a}
code{overflow-wrap:anywhere}.scope{background:#e7effd;padding:1rem}small{color:#59667a}</style><main>
<h1>""" + html.escape(value["title"]) + "</h1>"
                   + f'<p class="scope">Historical cutoff view <code>{view["id"]}</code>; '
                   + f'complete denominator: {view["metadata"]["attempts"]} attempts. This report is not a closure assessment.</p>'
                   + "<section><h2>Interpretation</h2><p>" + html.escape(value["summary"]).replace("\n", "<br>")
                   + "</p><h2>Limitations</h2><p>" + html.escape(value["limitations"]).replace("\n", "<br>")
                   + "</p><small>Only structured numeric claims are mechanically value-checked; interpretation is not certified.</small></section>"
                   + _chart(points, metric) + "<h2>Complete scoped inventory</h2><p>"
                   + html.escape(view["metadata"]["selection_reason"])
                   + "</p><table><thead><tr><th>Attempt / experiment</th><th>Execution</th><th>Collection / validation</th>"
                   + "<th>Selection</th><th>Evidence revisions</th></tr></thead><tbody>").encode()
            for record in _records(conn, view["id"]):
                columns = [record["id"] + "\n" + record["experiment_id"], record["status"],
                           record["collection"] + " / " + record["validation"],
                           "selected" if record["selected"] else "not selected",
                           ", ".join(record["observation_ids"]) or "No captured observation"]
                yield ("<tr>" + "".join("<td>" + html.escape(item) + "</td>" for item in columns) + "</tr>").encode()
            yield b"</tbody></table><h2>Checked numeric claims</h2><pre>"
            yield html.escape(json.dumps(value["claims"], indent=2, ensure_ascii=False)).encode("utf-8")
            yield b"</pre><h2>Retained source revisions</h2>"
            for source in sources():
                yield (f'<section id="source-{html.escape(source["id"], quote=True)}"><h3>'
                       + html.escape(source["kind"]) + " <code>" + source["id"] + "</code></h3><pre>"
                       + html.escape(source["text"]) + "</pre></section>").encode()
            yield b"<h2>Available local figures</h2><p>At most eight small captured PNG/JPEG figures; no external assets.</p>"
            for oid, mime, image in figures:
                yield f'<figure><img alt="Captured figure {oid}" src="data:{mime};base64,{image}"><figcaption>{oid}</figcaption></figure>'.encode()
            yield ("<p>Inventory digest: <code>" + view["metadata"]["inventory_digest"]
                   + "</code>. Exact row details and source bytes are in the manifest and retained view.</p></main></html>").encode()

        def markdown_chunks():
            yield (f'# {_markdown(value["title"])}\n\nHistorical cutoff view `{view["id"]}`; not a closure assessment.\n\n'
                   + f'Complete denominator: **{view["metadata"]["attempts"]} attempts**.\n\n'
                   + f'## Interpretation\n\n{_markdown(value["summary"])}\n\n## Limitations\n\n{_markdown(value["limitations"])}\n\n'
                   + "Only structured numeric claims are mechanically checked.\n\n"
                   + "| Attempt | Experiment | Execution | Collection | Validation | Selection |\n|---|---|---|---|---|---|\n").encode()
            for row in _records(conn, view["id"]):
                yield ("| " + " | ".join(_markdown(row[key]) for key in
                       ("id", "experiment_id", "status", "collection", "validation", "selected")) + " |\n").encode()
            yield ("\n## Checked claims\n\n```json\n" + json.dumps(value["claims"], indent=2, ensure_ascii=False)
                   + "\n```\n\n## Retained sources\n\n").encode()
            for source in sources():
                yield f'- `{source["id"]}` ({_markdown(source["kind"])}, SHA-256 `{source["digest"]}`)\n'.encode()

        def manifest_chunks():
            yield canonical({"type": "view", "view": view, "interpretation": value}) + b"\n"
            for source in sources():
                yield canonical({"type": "source", "source": {key: source[key] for key in
                                ("id", "kind", "origin", "digest", "created", "metadata", "text")}}) + b"\n"
            for row in _records(conn, view["id"]):
                yield canonical({"type": "attempt", "record": row}) + b"\n"
                for oid in row["observation_ids"]:
                    evidence = dict(conn.execute("SELECT id,kind,size,digest,assurance,metadata FROM observations WHERE id=?", (oid,)).fetchone())
                    evidence["metadata"] = json.loads(evidence["metadata"])
                    yield canonical({"type": "observation", "observation": evidence}) + b"\n"
            yield canonical({"type": "end", "attempts": view["metadata"]["attempts"],
                             "inventory_digest": view["metadata"]["inventory_digest"]}) + b"\n"

        return {name: publish_stream(directory / filename, chunks(), limit=max_bytes)
                for name, filename, chunks in (("html", "report.html", html_chunks), ("markdown", "report.md", markdown_chunks),
                                               ("manifest", "sources.jsonl", manifest_chunks))}


def publish_report(state, view_id, *, value=None, invocation_id=None, max_bytes=32 * 1024 * 1024):
    integer(max_bytes, "report publication limit")
    if max_bytes > 256 * 1024 * 1024:
        raise ValueError("Report publication limit cannot exceed 256 MiB per output")
    turn_id = value["turn_id"] if value is not None else None
    with state.db.read() as conn:
        view = _header(conn.execute("SELECT * FROM views WHERE id=?", (view_id,)).fetchone())
        if value is not None:
            turn, packet, _ = state._owned_maintenance(conn, value, invocation_id, "report")
            if turn["state"] == "accepted":
                prior = conn.execute("SELECT * FROM reports WHERE turn_id=?", (turn_id,)).fetchone()
                return {**dict(prior), "outputs": json.loads(prior["outputs"])}
            if packet["view_id"] != view_id or value.get("view_id") != view_id or not read_all(conn, turn_id, view_id):
                raise ValueError("Report must receive every page of its exact source inventory")
            _validate_report(conn, value)
        else:
            value = {"title": "Research inventory", "summary": "Deterministic retained inventory; no model interpretation requested.",
                     "limitations": "Execution and measurement validity are separate. No scientific acceptance is inferred.",
                     "claims": [], "references": list(view["source_ids"].values()), "view_id": view_id}
    report_id = turn_id or identifier()
    outputs = _render_outputs(state, view, value, report_id, max_bytes=max_bytes)
    with state.db.write() as conn:
        if turn_id:
            turn, _, request = state._owned_maintenance(conn, value, invocation_id, "report")
            if turn["state"] != "accepted":
                _validate_report(conn, value)
                conn.execute("UPDATE turns SET state='accepted',ended=?,result=? WHERE id=?",
                             (time.time(), canonical(value).decode(), turn_id))
                conn.execute("UPDATE maintenance SET state='completed' WHERE id=?", (request["id"],))
        conn.execute("INSERT OR IGNORE INTO reports(id,view_id,turn_id,outputs,created) VALUES(?,?,?,?,?)",
                     (report_id, view_id, turn_id, canonical(outputs).decode(), time.time()))
        if turn_id:
            conn.execute("UPDATE blockers SET resolved=? WHERE id=?", (time.time(), f"report-publication-{turn_id}"))
    return {"id": report_id, "view_id": view_id, "turn_id": turn_id, "outputs": outputs}
