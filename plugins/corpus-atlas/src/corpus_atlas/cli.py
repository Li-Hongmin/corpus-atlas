from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .scholarly import (
    add_scholarly_parsers,
    dispatch_scholarly,
    scholarly_snapshot,
)


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, question TEXT NOT NULL,
    domain TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT, source_type TEXT NOT NULL,
    publisher TEXT, published_at TEXT, accessed_at TEXT NOT NULL, local_file TEXT,
    sha256 TEXT, status TEXT NOT NULL DEFAULT 'available', notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY, text TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_links (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    claim_id TEXT NOT NULL REFERENCES claims(id), relation TEXT NOT NULL,
    locator TEXT, quote TEXT, limitations TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_relations (
    id TEXT PRIMARY KEY,
    source_claim_id TEXT NOT NULL REFERENCES claims(id),
    target_claim_id TEXT NOT NULL REFERENCES claims(id),
    relation TEXT NOT NULL, rationale TEXT, created_at TEXT NOT NULL,
    UNIQUE(source_claim_id, target_claim_id, relation)
);
CREATE TABLE IF NOT EXISTS probes (
    id TEXT PRIMARY KEY, title TEXT NOT NULL,
    target_claim_id TEXT REFERENCES claims(id),
    modality TEXT NOT NULL, question TEXT NOT NULL,
    expected_if_true TEXT, expected_if_false TEXT,
    cost TEXT, priority REAL, status TEXT NOT NULL DEFAULT 'planned',
    notes TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS probe_results (
    id TEXT PRIMARY KEY, probe_id TEXT NOT NULL REFERENCES probes(id),
    observation_claim_id TEXT REFERENCES claims(id),
    verdict TEXT NOT NULL, summary TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY, citation_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
    authors TEXT, year INTEGER, doi TEXT UNIQUE, arxiv TEXT UNIQUE, pmid TEXT UNIQUE,
    semantic_scholar_id TEXT, openalex_id TEXT, venue TEXT, publisher TEXT,
    abstract TEXT, url TEXT, metadata_source TEXT, search_date TEXT,
    reading_status TEXT NOT NULL DEFAULT 'metadata-only',
    fulltext_status TEXT NOT NULL DEFAULT 'metadata-only', created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_relations (
    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
    direction TEXT NOT NULL, related_identifier TEXT NOT NULL,
    relation_data TEXT, UNIQUE(paper_id, direction, related_identifier)
);
CREATE TABLE IF NOT EXISTS paper_files (
    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
    role TEXT NOT NULL, local_file TEXT NOT NULL, source_url TEXT NOT NULL,
    source_name TEXT, license TEXT NOT NULL, original_filename TEXT,
    content_type TEXT, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
    retrieved_at TEXT NOT NULL, UNIQUE(paper_id, role, sha256)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def item_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def workspace(path: str, must_exist: bool = True) -> tuple[Path, Path]:
    root = Path(path).expanduser().resolve()
    database = root / "atlas.sqlite3"
    if must_exist and not database.is_file():
        raise SystemExit(f"Not a Corpus Atlas workspace: {root}")
    return root, database


def connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def write_snapshot(root: Path, connection: sqlite3.Connection) -> None:
    investigation = connection.execute("SELECT * FROM investigations LIMIT 1").fetchone()
    data = {
        "format": "corpus-atlas/0.2",
        "investigation": dict(investigation) if investigation else None,
        "sources": [dict(row) for row in connection.execute("SELECT * FROM sources ORDER BY created_at, id")],
        "claims": [dict(row) for row in connection.execute("SELECT * FROM claims ORDER BY created_at, id")],
        "evidence_links": [dict(row) for row in connection.execute("SELECT * FROM evidence_links ORDER BY created_at, id")],
        "claim_relations": [dict(row) for row in connection.execute("SELECT * FROM claim_relations ORDER BY created_at, id")],
        "probes": [dict(row) for row in connection.execute("SELECT * FROM probes ORDER BY priority DESC, created_at, id")],
        "probe_results": [dict(row) for row in connection.execute("SELECT * FROM probe_results ORDER BY created_at, id")],
        **scholarly_snapshot(connection),
    }
    (root / "atlas.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command_init(args: argparse.Namespace) -> None:
    root, database = workspace(args.path, must_exist=False)
    root.mkdir(parents=True, exist_ok=True)
    (root / "attachments").mkdir(exist_ok=True)
    if database.exists() and not args.force:
        raise SystemExit(f"Workspace already exists: {root}")
    if database.exists():
        database.unlink()
    with connect(database) as connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO investigations VALUES (?, ?, ?, ?, ?)",
            (item_id("inv"), args.title, args.question, args.domain, now()),
        )
        write_snapshot(root, connection)
    print(root)


def command_source_add(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    source_id = item_id("src")
    local_file = None
    digest = None
    if args.file:
        original = Path(args.file).expanduser().resolve()
        if not original.is_file():
            raise SystemExit(f"Attachment not found: {original}")
        target = root / "attachments" / f"{source_id}-{original.name}"
        shutil.copy2(original, target)
        local_file = str(target.relative_to(root))
        digest = sha256(target)
    with connect(database) as connection:
        connection.execute(
            """INSERT INTO sources
               (id,title,url,source_type,publisher,published_at,accessed_at,local_file,sha256,status,notes,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source_id, args.title, args.url, args.source_type, args.publisher,
             args.published_at, args.accessed_at or now(), local_file, digest,
             args.status, args.notes, now()),
        )
        write_snapshot(root, connection)
    print(source_id)


def command_claim_add(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    claim_id = item_id("clm")
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO claims VALUES (?, ?, ?, ?, ?)",
            (claim_id, args.text, args.kind, args.status, now()),
        )
        write_snapshot(root, connection)
    print(claim_id)


def command_claim_relate(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    relation_id = item_id("crl")
    try:
        with connect(database) as connection:
            connection.execute(
                "INSERT INTO claim_relations VALUES (?, ?, ?, ?, ?, ?)",
                (relation_id, args.source_claim, args.target_claim,
                 args.relation, args.rationale, now()),
            )
            write_snapshot(root, connection)
    except sqlite3.IntegrityError as error:
        raise SystemExit(f"Invalid or duplicate claim relation: {error}") from error
    print(relation_id)


def command_link_add(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    link_id = item_id("lnk")
    try:
        with connect(database) as connection:
            connection.execute(
                "INSERT INTO evidence_links VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (link_id, args.source, args.claim, args.relation, args.locator,
                 args.quote, args.limitations, now()),
            )
            write_snapshot(root, connection)
    except sqlite3.IntegrityError as error:
        raise SystemExit(f"Unknown source or claim ID: {error}") from error
    print(link_id)


def command_probe_add(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    probe_id = item_id("prb")
    if args.priority is not None and not 0 <= args.priority <= 1:
        raise SystemExit("Probe priority must be between 0 and 1")
    try:
        with connect(database) as connection:
            connection.execute(
                """INSERT INTO probes
                   (id,title,target_claim_id,modality,question,expected_if_true,
                    expected_if_false,cost,priority,status,notes,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (probe_id, args.title, args.target_claim, args.modality,
                 args.question, args.expected_if_true, args.expected_if_false,
                 args.cost, args.priority, args.status, args.notes, now()),
            )
            write_snapshot(root, connection)
    except sqlite3.IntegrityError as error:
        raise SystemExit(f"Unknown target claim ID: {error}") from error
    print(probe_id)


def command_probe_list(args: argparse.Namespace) -> None:
    _, database = workspace(args.path)
    clauses = []
    parameters: list[str] = []
    if args.status:
        clauses.append("status=?")
        parameters.append(args.status)
    if args.target_claim:
        clauses.append("target_claim_id=?")
        parameters.append(args.target_claim)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(database) as connection:
        rows = [dict(row) for row in connection.execute(
            f"SELECT * FROM probes{where} ORDER BY priority DESC, created_at, id",
            parameters,
        )]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def command_probe_set_status(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    with connect(database) as connection:
        cursor = connection.execute(
            "UPDATE probes SET status=? WHERE id=?",
            (args.status, args.probe),
        )
        if cursor.rowcount == 0:
            raise SystemExit(f"Unknown probe: {args.probe}")
        write_snapshot(root, connection)
    print(args.probe)


def command_probe_result(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    result_id = item_id("res")
    try:
        with connect(database) as connection:
            probe = connection.execute("SELECT id FROM probes WHERE id=?", (args.probe,)).fetchone()
            if not probe:
                raise SystemExit(f"Unknown probe: {args.probe}")
            connection.execute(
                "INSERT INTO probe_results VALUES (?, ?, ?, ?, ?, ?)",
                (result_id, args.probe, args.observation_claim,
                 args.verdict, args.summary, now()),
            )
            connection.execute("UPDATE probes SET status='completed' WHERE id=?", (args.probe,))
            write_snapshot(root, connection)
    except sqlite3.IntegrityError as error:
        raise SystemExit(f"Unknown observation claim ID: {error}") from error
    print(result_id)


def command_status(args: argparse.Namespace) -> None:
    _, database = workspace(args.path)
    with connect(database) as connection:
        inv = connection.execute("SELECT title, question, domain FROM investigations LIMIT 1").fetchone()
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sources", "claims", "evidence_links", "claim_relations", "probes", "probe_results")
        }
        unresolved = connection.execute("SELECT COUNT(*) FROM claims WHERE status = 'unresolved'").fetchone()[0]
        unlinked = connection.execute(
            "SELECT COUNT(*) FROM claims c WHERE NOT EXISTS (SELECT 1 FROM evidence_links e WHERE e.claim_id=c.id)"
        ).fetchone()[0]
        open_probes = connection.execute(
            "SELECT COUNT(*) FROM probes WHERE status IN ('planned','running')"
        ).fetchone()[0]
        scholarly_counts = {
            "papers": connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
            "paper_relations": connection.execute("SELECT COUNT(*) FROM paper_relations").fetchone()[0],
            "paper_files": connection.execute("SELECT COUNT(*) FROM paper_files").fetchone()[0],
        }
    print(json.dumps({"title": inv["title"], "question": inv["question"],
                      "domain": inv["domain"], **counts,
                      **scholarly_counts,
                      "unresolved_claims": unresolved, "unlinked_claims": unlinked,
                      "open_probes": open_probes},
                     ensure_ascii=False, indent=2))


def command_export(args: argparse.Namespace) -> None:
    root, database = workspace(args.path)
    with connect(database) as connection:
        write_snapshot(root, connection)
    source = root / "atlas.json"
    if args.output:
        destination = Path(args.output).expanduser().resolve()
        shutil.copy2(source, destination)
        print(destination)
    else:
        print(source.read_text(encoding="utf-8"), end="")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="corpus-atlas", description="Local-first investigation corpus")
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create an investigation workspace")
    init.add_argument("path")
    init.add_argument("--title", required=True)
    init.add_argument("--question", required=True)
    init.add_argument("--domain", choices=("general", "science", "finance", "public-affairs"), default="general")
    init.add_argument("--force", action="store_true")
    init.set_defaults(run=command_init)

    source = commands.add_parser("source", help="manage sources").add_subparsers(dest="source_command", required=True)
    source_add = source.add_parser("add")
    source_add.add_argument("path")
    source_add.add_argument("--title", required=True)
    source_add.add_argument("--url")
    source_add.add_argument("--source-type", required=True)
    source_add.add_argument("--publisher")
    source_add.add_argument("--published-at")
    source_add.add_argument("--accessed-at")
    source_add.add_argument("--file")
    source_add.add_argument("--status", choices=("available", "missing", "inaccessible", "superseded"), default="available")
    source_add.add_argument("--notes")
    source_add.set_defaults(run=command_source_add)

    claim = commands.add_parser("claim", help="manage atomic claims").add_subparsers(dest="claim_command", required=True)
    claim_add = claim.add_parser("add")
    claim_add.add_argument("path")
    claim_add.add_argument("--text", required=True)
    claim_add.add_argument("--kind", choices=("observed", "interpreted", "hypothesis"), default="observed")
    claim_add.add_argument("--status", choices=("unresolved", "supported", "contested", "rejected"), default="unresolved")
    claim_add.set_defaults(run=command_claim_add)

    claim_relate = claim.add_parser("relate", help="connect two claims without treating the relation as source evidence")
    claim_relate.add_argument("path")
    claim_relate.add_argument("--from", dest="source_claim", required=True)
    claim_relate.add_argument("--to", dest="target_claim", required=True)
    claim_relate.add_argument(
        "--relation",
        choices=("supports", "contradicts", "depends-on", "competes-with", "refines", "predicts", "explains"),
        required=True,
    )
    claim_relate.add_argument("--rationale")
    claim_relate.set_defaults(run=command_claim_relate)

    link = commands.add_parser("link", help="connect evidence to claims").add_subparsers(dest="link_command", required=True)
    link_add = link.add_parser("add")
    link_add.add_argument("path")
    link_add.add_argument("--source", required=True)
    link_add.add_argument("--claim", required=True)
    link_add.add_argument("--relation", choices=("supports", "contradicts", "qualifies", "contextualizes", "mentions"), required=True)
    link_add.add_argument("--locator")
    link_add.add_argument("--quote")
    link_add.add_argument("--limitations")
    link_add.set_defaults(run=command_link_add)

    probe = commands.add_parser("probe", help="plan and record discriminating observations").add_subparsers(dest="probe_command", required=True)
    probe_add = probe.add_parser("add")
    probe_add.add_argument("path")
    probe_add.add_argument("--title", required=True)
    probe_add.add_argument("--target-claim")
    probe_add.add_argument("--modality", required=True,
                           help="for example literature, DFT, NEB, MLIP-MD, microkinetic, experiment, spectroscopy")
    probe_add.add_argument("--question", required=True)
    probe_add.add_argument("--if-true", dest="expected_if_true")
    probe_add.add_argument("--if-false", dest="expected_if_false")
    probe_add.add_argument("--cost")
    probe_add.add_argument("--priority", type=float)
    probe_add.add_argument("--status", choices=("planned", "running", "completed", "abandoned"), default="planned")
    probe_add.add_argument("--notes")
    probe_add.set_defaults(run=command_probe_add)

    probe_list = probe.add_parser("list")
    probe_list.add_argument("path")
    probe_list.add_argument("--status", choices=("planned", "running", "completed", "abandoned"))
    probe_list.add_argument("--target-claim")
    probe_list.set_defaults(run=command_probe_list)

    probe_status = probe.add_parser("set-status")
    probe_status.add_argument("path")
    probe_status.add_argument("--probe", required=True)
    probe_status.add_argument("--status", choices=("planned", "running", "completed", "abandoned"), required=True)
    probe_status.set_defaults(run=command_probe_set_status)

    probe_result = probe.add_parser("result")
    probe_result.add_argument("path")
    probe_result.add_argument("--probe", required=True)
    probe_result.add_argument("--observation-claim")
    probe_result.add_argument("--verdict", choices=("supports", "contradicts", "qualifies", "inconclusive"), required=True)
    probe_result.add_argument("--summary")
    probe_result.set_defaults(run=command_probe_result)

    status = commands.add_parser("status", help="summarize a workspace")
    status.add_argument("path")
    status.set_defaults(run=command_status)

    export = commands.add_parser("export", help="write portable JSON")
    export.add_argument("path")
    export.add_argument("--output")
    export.set_defaults(run=command_export)
    add_scholarly_parsers(commands)
    return root


def main() -> None:
    try:
        args = parser().parse_args()
        if hasattr(args, "run"):
            args.run(args)
        else:
            dispatch_scholarly(args, workspace, connect, write_snapshot)
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
