from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


USER_AGENT = "CorpusAtlas/0.2 (+https://github.com/Li-Hongmin/corpus-atlas)"


def current_time() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_doi(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text or None


def normalize_arxiv(value: Any) -> str | None:
    text = str(value or "").strip()
    text = re.sub(r"^(?:arxiv:|https?://arxiv.org/(?:abs|pdf)/)", "", text, flags=re.I)
    return re.sub(r"\.pdf$", "", text) or None


def normalized_title(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def authors_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        result = []
        for author in value:
            if isinstance(author, dict):
                result.append(author.get("name") or " ".join(filter(None, [author.get("given"), author.get("family")])))
            else:
                result.append(str(author))
        return [item.strip() for item in result if item and item.strip()]
    return [item.strip() for item in re.split(r"\s*;\s*|\s+and\s+", str(value)) if item.strip()]


def citation_key(record: dict[str, Any]) -> str:
    authors = authors_list(record.get("authors") or record.get("author"))
    surname = (authors[0].split()[-1] if authors else "unknown").lower()
    surname = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", surname).encode("ascii", "ignore").decode()) or "unknown"
    year = str(record.get("year") or "nd")
    words = [word for word in normalized_title(record.get("title")).split() if word not in {"a", "an", "the"} and re.search(r"[a-z]", word)]
    return f"{surname}{year}{(words[0] if words else 'untitled')}"


def parse_relation(value: Any) -> list[Any]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        return [item.strip() for item in re.split(r"[;\n]", str(value)) if item.strip()]


def stable_identifier(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("doi", "DOI", "arxiv", "pmid", "openalex_id", "id", "title"):
            if value.get(key):
                return f"{key.lower()}:{value[key]}"
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def load_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in {".csv", ".tsv"}:
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream, delimiter="\t" if path.suffix.lower() == ".tsv" else ","))
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list) and all(isinstance(row, dict) for row in data):
        return data
    raise SystemExit("Input must contain a JSON object/list or CSV/TSV records")


def unique_key(connection: sqlite3.Connection, base: str, paper_id: str | None = None) -> str:
    candidate = base
    suffix = ord("a")
    while connection.execute("SELECT 1 FROM papers WHERE citation_key=? AND id IS NOT ?", (candidate, paper_id)).fetchone():
        candidate = f"{base}{chr(suffix)}"
        suffix += 1
    return candidate


def find_existing(connection: sqlite3.Connection, record: dict[str, Any]) -> sqlite3.Row | None:
    for field, value in (("doi", normalize_doi(record.get("doi"))), ("arxiv", normalize_arxiv(record.get("arxiv") or record.get("eprint"))), ("pmid", record.get("pmid"))):
        if value:
            row = connection.execute(f"SELECT * FROM papers WHERE {field}=?", (str(value),)).fetchone()
            if row:
                return row
    title = normalized_title(record.get("title"))
    if title:
        for row in connection.execute("SELECT * FROM papers"):
            if normalized_title(row["title"]) == title and (not record.get("year") or str(row["year"]) == str(record.get("year"))):
                return row
    return None


def cache_record(connection: sqlite3.Connection, record: dict[str, Any]) -> tuple[str, bool]:
    if not record.get("title"):
        raise ValueError("paper title is required")
    existing = find_existing(connection, record)
    paper_id = existing["id"] if existing else f"pap_{uuid.uuid4().hex[:12]}"
    authors = authors_list(record.get("authors") or record.get("author"))
    values = {
        "title": str(record["title"]).strip(), "authors": json.dumps(authors, ensure_ascii=False),
        "year": int(record["year"]) if str(record.get("year") or "").isdigit() else None,
        "doi": normalize_doi(record.get("doi")), "arxiv": normalize_arxiv(record.get("arxiv") or record.get("eprint")),
        "pmid": str(record.get("pmid") or "") or None, "semantic_scholar_id": record.get("semantic_scholar_id"),
        "openalex_id": record.get("openalex_id"), "venue": record.get("venue") or record.get("journal"),
        "publisher": record.get("publisher"), "abstract": record.get("abstract"), "url": record.get("url"),
        "metadata_source": record.get("metadata_source") or record.get("source"),
        "search_date": record.get("search_date") or datetime.now().date().isoformat(),
        "reading_status": record.get("reading_status") or "metadata-only",
        "fulltext_status": record.get("fulltext_status") or "metadata-only",
    }
    key = unique_key(connection, citation_key(record), paper_id if existing else None)
    if existing:
        merged = {name: values[name] if values[name] not in (None, "", "[]") else existing[name] for name in values}
        connection.execute("""UPDATE papers SET citation_key=?, title=?,authors=?,year=?,doi=?,arxiv=?,pmid=?,semantic_scholar_id=?,openalex_id=?,venue=?,publisher=?,abstract=?,url=?,metadata_source=?,search_date=?,reading_status=?,fulltext_status=?,updated_at=? WHERE id=?""",
                           (key, *merged.values(), current_time(), paper_id))
    else:
        stamp = current_time()
        connection.execute("""INSERT INTO papers (id,citation_key,title,authors,year,doi,arxiv,pmid,semantic_scholar_id,openalex_id,venue,publisher,abstract,url,metadata_source,search_date,reading_status,fulltext_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (paper_id, key, *values.values(), stamp, stamp))
    for direction in ("references", "citations"):
        for related in parse_relation(record.get(direction)):
            identifier = stable_identifier(related)
            connection.execute("INSERT OR IGNORE INTO paper_relations VALUES (?,?,?,?,?)",
                               (f"rel_{uuid.uuid4().hex[:12]}", paper_id, direction, identifier,
                                json.dumps(related, ensure_ascii=False) if isinstance(related, dict) else None))
    return paper_id, not bool(existing)


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def openalex_abstract(index: Any) -> str | None:
    if not isinstance(index, dict):
        return None
    positions = [(position, word) for word, values in index.items() for position in values]
    return " ".join(word for _, word in sorted(positions)) or None


def discover(query: str, provider: str, limit: int) -> list[dict[str, Any]]:
    encoded = urllib.parse.quote(query)
    if provider == "openalex":
        data = request_json(f"https://api.openalex.org/works?search={encoded}&per-page={limit}")
        rows = []
        for work in data.get("results", []):
            rows.append({"title": work.get("title"), "authors": [a.get("author", {}).get("display_name") for a in work.get("authorships", [])],
                         "year": work.get("publication_year"), "doi": work.get("doi"), "openalex_id": work.get("id"),
                         "venue": (work.get("primary_location") or {}).get("source", {}).get("display_name"),
                         "url": (work.get("primary_location") or {}).get("landing_page_url"),
                         "abstract": openalex_abstract(work.get("abstract_inverted_index")),
                         "references": work.get("referenced_works", []), "metadata_source": "OpenAlex"})
        return rows
    data = request_json(f"https://api.crossref.org/works?query={encoded}&rows={limit}")
    rows = []
    for work in data.get("message", {}).get("items", []):
        date_parts = (work.get("published-print") or work.get("published-online") or {}).get("date-parts", [[]])
        rows.append({"title": (work.get("title") or [""])[0], "authors": work.get("author", []),
                     "year": date_parts[0][0] if date_parts and date_parts[0] else None, "doi": work.get("DOI"),
                     "venue": (work.get("container-title") or [None])[0], "publisher": work.get("publisher"),
                     "url": work.get("URL"), "abstract": work.get("abstract"),
                     "references": work.get("reference", []), "metadata_source": "Crossref"})
    return rows


def download_file(url: str, destination: Path) -> tuple[str, int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        content_type = response.headers.get_content_type()
        while block := response.read(1024 * 1024):
            output.write(block); digest.update(block); size += len(block)
    return digest.hexdigest(), size, content_type


def bib_escape(value: Any) -> str:
    return str(value or "").replace("\\", "\\textbackslash{}").replace("{", "\\{").replace("}", "\\}")


def scholarly_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        "papers": [dict(row) for row in connection.execute("SELECT * FROM papers ORDER BY citation_key")],
        "paper_relations": [dict(row) for row in connection.execute("SELECT * FROM paper_relations ORDER BY paper_id,direction")],
        "paper_files": [dict(row) for row in connection.execute("SELECT * FROM paper_files ORDER BY paper_id,role")],
    }


def add_scholarly_parsers(commands: argparse._SubParsersAction) -> None:
    paper = commands.add_parser("paper", help="discover, cache, retrieve, and export scholarly records").add_subparsers(dest="paper_command", required=True)
    imp = paper.add_parser("import"); imp.add_argument("path"); imp.add_argument("--from", dest="input", required=True)
    discover_p = paper.add_parser("discover"); discover_p.add_argument("path"); discover_p.add_argument("--query", required=True); discover_p.add_argument("--provider", choices=("openalex", "crossref"), default="openalex"); discover_p.add_argument("--limit", type=int, default=10); discover_p.add_argument("--cache", action="store_true")
    search = paper.add_parser("search"); search.add_argument("path"); search.add_argument("query")
    retrieve = paper.add_parser("retrieve"); retrieve.add_argument("path"); retrieve.add_argument("--paper", required=True); retrieve.add_argument("--url", required=True); retrieve.add_argument("--license", required=True); retrieve.add_argument("--source", required=True); retrieve.add_argument("--role", choices=("main", "supplement"), default="main"); retrieve.add_argument("--filename")
    dedupe = paper.add_parser("dedupe"); dedupe.add_argument("path")
    bib = paper.add_parser("export-bib"); bib.add_argument("path"); bib.add_argument("--output", required=True)


def dispatch_scholarly(args: argparse.Namespace, workspace: Callable, connect: Callable, write_snapshot: Callable) -> None:
    root, database = workspace(args.path)
    with connect(database) as connection:
        connection.executescript("PRAGMA foreign_keys=ON")
        if args.paper_command == "import":
            created = updated = skipped = 0
            for record in load_records(Path(args.input).expanduser().resolve()):
                try:
                    _, fresh = cache_record(connection, record); created += int(fresh); updated += int(not fresh)
                except (ValueError, sqlite3.IntegrityError): skipped += 1
            write_snapshot(root, connection)
            print(json.dumps({"created": created, "updated": updated, "skipped": skipped}, indent=2)); return
        if args.paper_command == "discover":
            rows = discover(args.query, args.provider, max(1, min(args.limit, 100)))
            if args.cache:
                for row in rows: cache_record(connection, row)
                write_snapshot(root, connection)
            print(json.dumps(rows, ensure_ascii=False, indent=2)); return
        if args.paper_command == "search":
            term = f"%{args.query}%"
            rows = [dict(row) for row in connection.execute("SELECT * FROM papers WHERE title LIKE ? OR authors LIKE ? OR abstract LIKE ? OR doi LIKE ? ORDER BY year DESC", (term,term,term,term))]
            print(json.dumps(rows, ensure_ascii=False, indent=2)); return
        if args.paper_command == "retrieve":
            paper_row = connection.execute("SELECT * FROM papers WHERE id=? OR citation_key=?", (args.paper,args.paper)).fetchone()
            if not paper_row: raise SystemExit(f"Unknown paper: {args.paper}")
            name = args.filename or Path(urllib.parse.urlparse(args.url).path).name or f"{paper_row['citation_key']}.pdf"
            destination = root / "attachments" / f"{paper_row['citation_key']}-{args.role}-{name}"
            try: digest, size, content_type = download_file(args.url, destination)
            except (urllib.error.URLError, TimeoutError) as error: destination.unlink(missing_ok=True); raise SystemExit(f"Download failed: {error}") from error
            if args.role == "main" and destination.read_bytes()[:5] != b"%PDF-": destination.unlink(missing_ok=True); raise SystemExit("Main-text download is not a PDF")
            connection.execute("INSERT OR IGNORE INTO paper_files VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (f"fil_{uuid.uuid4().hex[:12]}", paper_row["id"], args.role, str(destination.relative_to(root)), args.url, args.source, args.license, name, content_type, digest, size, current_time()))
            if args.role == "main": connection.execute("UPDATE papers SET fulltext_status='local-fulltext',updated_at=? WHERE id=?", (current_time(),paper_row["id"]))
            write_snapshot(root, connection); print(json.dumps({"file": str(destination), "sha256": digest, "bytes": size}, indent=2)); return
        if args.paper_command == "dedupe":
            rows = list(connection.execute("SELECT id,citation_key,title,year,doi,arxiv FROM papers ORDER BY citation_key")); groups: dict[str,list[str]] = {}
            for row in rows:
                key = f"doi:{row['doi']}" if row["doi"] else f"arxiv:{row['arxiv']}" if row["arxiv"] else f"title:{normalized_title(row['title'])}:{row['year']}"
                groups.setdefault(key, []).append(row["citation_key"])
            print(json.dumps({key:value for key,value in groups.items() if len(value)>1}, ensure_ascii=False, indent=2)); return
        if args.paper_command == "export-bib":
            entries=[]
            for row in connection.execute("SELECT * FROM papers ORDER BY citation_key"):
                fields={"title":row["title"],"author":" and ".join(json.loads(row["authors"] or "[]")),"year":row["year"],"doi":row["doi"],"journal":row["venue"],"publisher":row["publisher"],"url":row["url"]}
                body=",\n".join(f"  {key} = {{{bib_escape(value)}}}" for key,value in fields.items() if value not in (None,""))
                entries.append(f"@article{{{row['citation_key']},\n{body}\n}}")
            output=Path(args.output).expanduser().resolve(); output.write_text("\n\n".join(entries)+"\n",encoding="utf-8"); print(output); return
