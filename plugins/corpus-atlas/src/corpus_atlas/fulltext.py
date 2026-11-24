"""Legal open-access full-text resolution, batch retrieval, supplements,
preprint publication checks, integrity checks, and citation expansion.

Every network call goes through :func:`http_get` so tests can mock it.
Nothing here bypasses paywalls, logins, or CAPTCHAs: a candidate that does not
return the expected file type directly is recorded as a failure and the next
legal candidate is tried.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scholarly import current_time, normalize_arxiv, normalize_doi, normalized_title

PROJECT_URL = "https://github.com/Li-Hongmin/corpus-atlas"
MAX_BYTES = 200 * 1024 * 1024
PDF_ACCEPT = "application/pdf,application/octet-stream;q=0.9,text/html;q=0.5,*/*;q=0.3"


def user_agent(email: str | None = None) -> str:
    contact = f"; mailto:{email}" if email else ""
    return f"Mozilla/5.0 (compatible; CorpusAtlas/0.3; +{PROJECT_URL}{contact})"


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    url: str
    error: str | None = None


def http_get(url: str, accept: str = "application/json", email: str | None = None, timeout: int = 60) -> Response:
    """Fetch a URL with a descriptive UA. Never raises for HTTP/network errors."""
    headers = {"User-Agent": user_agent(email), "Accept": accept, "Accept-Language": "en;q=0.9,*;q=0.5"}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return Response(0, "", b"", url, "response exceeds size limit")
            ctype = response.headers.get_content_type() if response.headers else ""
            return Response(getattr(response, "status", 200) or 200, ctype, body, response.geturl() or url)
    except urllib.error.HTTPError as error:
        return Response(error.code, "", b"", url, f"HTTP {error.code}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return Response(0, "", b"", url, str(getattr(error, "reason", error)))


def get_json(url: str, email: str | None = None) -> Any:
    response = http_get(url, "application/json", email)
    if response.status != 200:
        return None
    try:
        return json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------- file types
OFFICE = {"word/": ".docx", "xl/": ".xlsx", "ppt/": ".pptx"}


def infer_extension(body: bytes, content_type: str = "", filename: str = "") -> str:
    """Infer a file extension from magic bytes, then content type, then name."""
    head = body[:512]
    if head.startswith(b"%PDF-"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = zipfile.ZipFile(io.BytesIO(body)).namelist()
            for prefix, suffix in OFFICE.items():
                if any(name.startswith(prefix) for name in names):
                    return suffix
        except zipfile.BadZipFile:
            pass
        named = Path(urllib.parse.urlparse(filename).path).suffix.lower()
        return named if named in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub"} else ".zip"
    if head.startswith(b"\x1f\x8b"):
        return ".gz"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return Path(filename).suffix.lower() if Path(filename).suffix.lower() in {".doc", ".xls", ".ppt"} else ".doc"
    stripped = head.lstrip().lower()
    if stripped.startswith((b"<!doctype html", b"<html")):
        return ".html"
    if stripped.startswith(b"<?xml"):
        return ".xml"
    guessed = mimetypes.guess_extension(content_type or "") if content_type and content_type != "application/octet-stream" else None
    if guessed:
        return {".htm": ".html", ".jpe": ".jpg"}.get(guessed, guessed)
    suffix = Path(urllib.parse.urlparse(filename).path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix or ""):
        return suffix
    return ".bin"


def with_extension(name: str, extension: str) -> str:
    stem = Path(name).stem if Path(name).suffix.lower() == extension else name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "file"
    return stem + extension


# ---------------------------------------------------------------- candidates
def openalex_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    doi = paper.get("doi")
    work_id = paper.get("openalex_id")
    if doi:
        url = f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi, safe='/')}"
    elif work_id:
        url = f"https://api.openalex.org/works/{str(work_id).rsplit('/', 1)[-1]}"
    else:
        return []
    if email:
        url += f"?mailto={urllib.parse.quote(email)}"
    work = get_json(url, email) or {}
    locations = [work.get("best_oa_location")] + list(work.get("locations") or [])
    result = []
    for location in locations:
        if not location or not location.get("pdf_url"):
            continue
        if location.get("is_oa") is False:
            continue
        source = (location.get("source") or {}).get("display_name") or "OpenAlex location"
        result.append({"url": location["pdf_url"], "source": f"OpenAlex: {source}",
                       "license": location.get("license") or "open-access (license unspecified)"})
    return result


def unpaywall_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    if not (paper.get("doi") and email):
        return []
    data = get_json(f"https://api.unpaywall.org/v2/{urllib.parse.quote(paper['doi'], safe='/')}?email={urllib.parse.quote(email)}", email) or {}
    locations = [data.get("best_oa_location")] + list(data.get("oa_locations") or [])
    return [{"url": loc["url_for_pdf"], "source": f"Unpaywall: {loc.get('host_type') or 'location'}",
             "license": loc.get("license") or "open-access (license unspecified)"}
            for loc in locations if loc and loc.get("url_for_pdf")]


def europe_pmc_lookup(doi: str, email: str | None) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"query": f'DOI:"{doi}"', "format": "json", "resultType": "core", "pageSize": 1})
    data = get_json(f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{query}", email) or {}
    for result in (data.get("resultList") or {}).get("result", []):
        if normalize_doi(result.get("doi")) == doi and result.get("pmcid"):
            return result
    return None


def europe_pmc_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    if not paper.get("doi"):
        return []
    result = europe_pmc_lookup(paper["doi"], email)
    if not result or result.get("isOpenAccess") != "Y":
        return []
    pmcid = result["pmcid"]
    license_name = result.get("license") or "PMC open-access subset"
    return [{"url": f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf",
             "source": f"Europe PMC: {pmcid}", "license": license_name},
            {"url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/", "source": f"PMC: {pmcid}", "license": license_name}]


def arxiv_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    identifier = paper.get("arxiv")
    doi = paper.get("doi") or ""
    if not identifier and doi.startswith("10.48550/arxiv."):
        identifier = doi.split("arxiv.", 1)[1]
    if not identifier:
        return []
    return [{"url": f"https://arxiv.org/pdf/{identifier}", "source": f"arXiv: {identifier}",
             "license": "arXiv distribution license (or as stated on the record)"}]


def biorxiv_candidates(paper: dict[str, Any], email: str | None) -> list[dict[str, str]]:
    doi = paper.get("doi") or ""
    if not doi.startswith("10.1101/"):
        return []
    for server in ("biorxiv", "medrxiv"):
        data = get_json(f"https://api.biorxiv.org/details/{server}/{doi}", email) or {}
        versions = data.get("collection") or []
        if versions:
            latest = versions[-1]
            return [{"url": f"https://www.{server}.org/content/{doi}v{latest.get('version', 1)}.full.pdf",
                     "source": f"{server}: {doi}", "license": latest.get("license") or f"{server} license"}]
    return []


RESOLVERS = (openalex_candidates, unpaywall_candidates, europe_pmc_candidates, arxiv_candidates, biorxiv_candidates)


def resolve_candidates(paper: dict[str, Any], email: str | None = None) -> list[dict[str, str]]:
    seen: set[str] = set()
    ordered = []
    for resolver in RESOLVERS:
        for candidate in resolver(paper, email):
            if candidate["url"] not in seen:
                seen.add(candidate["url"])
                ordered.append(candidate)
    return ordered


def fetch_fulltext(paper: dict[str, Any], email: str | None = None) -> dict[str, Any]:
    """Try each legal candidate in order; return the first real PDF or all failures."""
    attempts = []
    candidates = resolve_candidates(paper, email)
    for candidate in candidates:
        response = http_get(candidate["url"], PDF_ACCEPT, email, timeout=90)
        if response.status == 200 and response.body.startswith(b"%PDF-"):
            return {"status": "downloaded", "candidate": candidate, "body": response.body,
                    "content_type": response.content_type or "application/pdf", "attempts": attempts}
        if response.status == 200:
            reason = f"not a PDF ({response.content_type or infer_extension(response.body)}); likely landing page or access wall"
        else:
            reason = response.error or f"HTTP {response.status}"
        attempts.append({"url": candidate["url"], "source": candidate["source"], "reason": reason})
    status = "no-oa-candidate" if not candidates else "failed"
    return {"status": status, "attempts": attempts}


# ---------------------------------------------------------------- storage
def store_file(connection: sqlite3.Connection, root: Path, paper: sqlite3.Row | dict, role: str, body: bytes,
               url: str, source: str, license_name: str, content_type: str, original: str = "") -> dict[str, Any]:
    extension = infer_extension(body, content_type, original or url)
    base = original or Path(urllib.parse.urlparse(url).path).name or role
    name = with_extension(f"{paper['citation_key']}-{role}-{Path(base).stem or role}", extension)
    destination = root / "attachments" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    existing = connection.execute("SELECT local_file FROM paper_files WHERE paper_id=? AND role=? AND sha256=?",
                                  (paper["id"], role, digest)).fetchone()
    if existing:
        return {"file": existing["local_file"], "sha256": digest, "bytes": len(body), "duplicate": True}
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        destination = destination.with_name(f"{destination.stem}-{digest[:8]}{extension}")
    destination.write_bytes(body)
    relative = destination.relative_to(root).as_posix()
    connection.execute("INSERT OR IGNORE INTO paper_files VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"fil_{uuid.uuid4().hex[:12]}", paper["id"], role, relative, url, source, license_name,
                        original or Path(urllib.parse.urlparse(url).path).name, content_type, digest, len(body), current_time()))
    if role == "main":
        connection.execute("UPDATE papers SET fulltext_status='local-fulltext',updated_at=? WHERE id=?", (current_time(), paper["id"]))
    return {"file": relative, "sha256": digest, "bytes": len(body)}


def resolve_email(value: str | None) -> str | None:
    return value or os.environ.get("CORPUS_ATLAS_EMAIL") or os.environ.get("UNPAYWALL_EMAIL") or None


def retrieve_all(connection: sqlite3.Connection, root: Path, email: str | None, max_workers: int,
                 limit: int | None = None, retry_failed: bool = True) -> dict[str, Any]:
    query = "SELECT * FROM papers WHERE fulltext_status!='local-fulltext' ORDER BY citation_key"
    papers = [dict(row) for row in connection.execute(query)]
    if not retry_failed:
        papers = [p for p in papers if p["fulltext_status"] != "oa-retrieval-failed"]
    if limit:
        papers = papers[:limit]
    already = connection.execute("SELECT COUNT(*) FROM papers WHERE fulltext_status='local-fulltext'").fetchone()[0]
    results = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(fetch_fulltext, paper, email): paper for paper in papers}
        for done, future in enumerate(as_completed(futures), 1):
            paper = futures[future]
            try:
                outcome = future.result()
            except Exception as error:  # keep the batch alive
                outcome = {"status": "failed", "attempts": [{"url": "", "source": "", "reason": repr(error)}]}
            row = {"citation_key": paper["citation_key"], "doi": paper["doi"], "title": paper["title"], "status": outcome["status"],
                   "attempts": outcome["attempts"]}
            if outcome["status"] == "downloaded":
                candidate = outcome["candidate"]
                stored = store_file(connection, root, paper, "main", outcome["body"], candidate["url"], candidate["source"],
                                    candidate["license"], outcome["content_type"])
                row.update(file=stored["file"], source=candidate["source"], license=candidate["license"])
            else:
                connection.execute("UPDATE papers SET fulltext_status=?,updated_at=? WHERE id=?",
                                   ("oa-retrieval-failed" if outcome["status"] == "failed" else "no-oa-candidate", current_time(), paper["id"]))
            connection.commit()  # resumable: every paper is persisted as it finishes
            results.append(row)
            print(f"[{done}/{len(papers)}] {row['status']}: {paper['citation_key']}", file=__import__("sys").stderr)
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    stamp = current_time().replace(":", "").replace("+", "Z")[:17]
    report_path = reports / f"retrieve-all-{stamp}.json"
    failures_path = reports / "fulltext-failures.csv"
    failures = [row for row in results if row["status"] != "downloaded"]
    with failures_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["citation_key", "doi", "title", "status", "landing_url", "tried", "last_reason"])
        for row in failures:
            landing = f"https://doi.org/{row['doi']}" if row["doi"] else ""
            writer.writerow([row["citation_key"], row["doi"] or "", row["title"], row["status"], landing,
                             " | ".join(a["url"] for a in row["attempts"]),
                             row["attempts"][-1]["reason"] if row["attempts"] else "no legal OA location known"])
    summary = {"attempted": len(papers), "downloaded": sum(r["status"] == "downloaded" for r in results),
               "failed": sum(r["status"] == "failed" for r in results),
               "no_oa_candidate": sum(r["status"] == "no-oa-candidate" for r in results),
               "already_local": already, "report": report_path.relative_to(root).as_posix(),
               "failures_csv": failures_path.relative_to(root).as_posix()}
    report_path.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


# ---------------------------------------------------------------- supplements
SUPPLEMENT_MARKERS = re.compile(r"(supp|supplement|si[_-]|appendix|mmc\d|esm|moesm|dataset|table_?s\d)", re.I)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff"}


def supplement_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = []
    for member in archive.infolist():
        if member.is_dir() or member.file_size <= 0:
            continue
        name = Path(member.filename).name
        if Path(name).suffix.lower() in IMAGE_SUFFIXES and not SUPPLEMENT_MARKERS.search(name):
            continue  # figure images of the main article are not supplements
        members.append(member)
    return members


def supplements(connection: sqlite3.Connection, root: Path, email: str | None, apply: bool, paper_ref: str | None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM papers WHERE doi IS NOT NULL" + (" AND (id=? OR citation_key=?)" if paper_ref else "") + " ORDER BY citation_key"
    rows = [dict(r) for r in connection.execute(sql, (paper_ref, paper_ref) if paper_ref else ())]
    report = []
    for paper in rows:
        result = europe_pmc_lookup(paper["doi"], email)
        if not result or result.get("isOpenAccess") != "Y":
            report.append({"citation_key": paper["citation_key"], "status": "no-legal-repository-supplements"})
            continue
        pmcid = result["pmcid"]
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"
        entry = {"citation_key": paper["citation_key"], "pmcid": pmcid, "source_url": url, "files": []}
        if not apply:
            entry["status"] = "available-preview"
            report.append(entry)
            continue
        response = http_get(url, "application/zip", email, timeout=120)
        if response.status != 200 or not response.body.startswith(b"PK"):
            entry["status"] = "none-found" if response.status in (200, 404) else (response.error or f"HTTP {response.status}")
            report.append(entry)
            continue
        license_name = result.get("license") or "Europe PMC open-access subset"
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            for member in supplement_members(archive):
                body = archive.read(member)
                stored = store_file(connection, root, paper, "supplement", body, f"{url}#{member.filename}",
                                    f"Europe PMC supplementary files: {pmcid}", license_name,
                                    mimetypes.guess_type(member.filename)[0] or "application/octet-stream", Path(member.filename).name)
                entry["files"].append(stored)
        entry["status"] = "downloaded" if entry["files"] else "none-found"
        report.append(entry)
    return report


# ---------------------------------------------------------------- publications
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def is_preprint(paper: dict[str, Any]) -> bool:
    doi = paper.get("doi") or ""
    return bool(paper.get("arxiv")) or doi.startswith(("10.48550/", "10.1101/")) or bool(re.search(r"arxiv|rxiv|preprint", paper.get("venue") or "", re.I))


def published_version(paper: dict[str, Any], email: str | None) -> dict[str, Any] | None:
    doi = paper.get("doi") or ""
    arxiv = paper.get("arxiv") or (doi.split("arxiv.", 1)[1] if doi.startswith("10.48550/arxiv.") else None)
    if arxiv:
        response = http_get(f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(arxiv)}", "application/atom+xml", email)
        if response.status == 200:
            try:
                entry = ET.fromstring(response.body).find("a:entry", ATOM)
            except ET.ParseError:
                entry = None
            if entry is not None and entry.findtext("arxiv:doi", namespaces=ATOM):
                return {"doi": normalize_doi(entry.findtext("arxiv:doi", namespaces=ATOM)),
                        "venue": entry.findtext("arxiv:journal_ref", namespaces=ATOM), "evidence": "arXiv record DOI"}
    if doi.startswith("10.1101/"):
        for server in ("biorxiv", "medrxiv"):
            data = get_json(f"https://api.biorxiv.org/pubs/{server}/{doi}", email) or {}
            for item in data.get("collection") or []:
                if item.get("published_doi"):
                    return {"doi": normalize_doi(item["published_doi"]), "venue": item.get("published_journal"),
                            "evidence": f"{server} publication link"}
    query = urllib.parse.urlencode({"search": paper.get("title") or "", "per-page": 5})
    data = get_json(f"https://api.openalex.org/works?{query}", email) or {}
    target = normalized_title(paper.get("title"))
    for work in data.get("results", []):
        work_doi = normalize_doi(work.get("doi")) or ""
        if (normalized_title(work.get("title")) == target and work_doi and work.get("type") != "preprint"
                and not work_doi.startswith(("10.48550/", "10.1101/")) and work_doi != doi):
            return {"doi": work_doi, "venue": ((work.get("primary_location") or {}).get("source") or {}).get("display_name"),
                    "evidence": "OpenAlex exact-title non-preprint match"}
    return None


def publications(connection: sqlite3.Connection, email: str | None, apply: bool) -> list[dict[str, Any]]:
    report = []
    for paper in [dict(r) for r in connection.execute("SELECT * FROM papers ORDER BY citation_key")]:
        if not is_preprint(paper):
            continue
        found = published_version(paper, email)
        entry = {"citation_key": paper["citation_key"], "preprint_doi": paper["doi"], "arxiv": paper["arxiv"],
                 "published": found, "applied": False}
        if found and apply:
            clash = connection.execute("SELECT citation_key FROM papers WHERE doi=? AND id!=?", (found["doi"], paper["id"])).fetchone()
            if clash:
                entry["note"] = f"published DOI already cached as {clash['citation_key']}; run dedupe"
            else:
                arxiv = paper["arxiv"] or (paper["doi"].split("arxiv.", 1)[1] if (paper["doi"] or "").startswith("10.48550/arxiv.") else None)
                connection.execute("UPDATE papers SET doi=?, arxiv=COALESCE(arxiv,?), venue=COALESCE(?,venue), updated_at=? WHERE id=?",
                                   (found["doi"], arxiv, found.get("venue"), current_time(), paper["id"]))
                entry["applied"] = True
        report.append(entry)
    return report


# ---------------------------------------------------------------- integrity
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check(connection: sqlite3.Connection, root: Path, restore_from: Path | None, apply: bool) -> dict[str, Any]:
    problems = []
    for row in connection.execute("SELECT f.*, p.citation_key FROM paper_files f JOIN papers p ON p.id=f.paper_id ORDER BY local_file"):
        path = root / row["local_file"]
        if not path.is_file():
            problems.append({"file": row["local_file"], "sha256": row["sha256"], "problem": "missing"})
        elif file_sha256(path) != row["sha256"]:
            problems.append({"file": row["local_file"], "sha256": row["sha256"], "problem": "sha256-mismatch"})
        elif not Path(row["local_file"]).suffix:
            problems.append({"file": row["local_file"], "sha256": row["sha256"], "problem": "no-extension"})
    claimed = connection.execute("SELECT COUNT(*) FROM papers p WHERE fulltext_status='local-fulltext' AND NOT EXISTS (SELECT 1 FROM paper_files f WHERE f.paper_id=p.id AND f.role='main')").fetchone()[0]
    restores = []
    if restore_from and any(p["problem"] != "no-extension" for p in problems):
        index: dict[str, Path] = {}
        for candidate in restore_from.rglob("*"):
            if candidate.is_file() and candidate.stat().st_size:
                index.setdefault(file_sha256(candidate), candidate)
        for problem in problems:
            source = index.get(problem["sha256"])
            if source and problem["problem"] != "no-extension":
                action = {"file": problem["file"], "restore_from": source.name, "applied": False}
                if apply:
                    target = root / problem["file"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    action["applied"] = True
                restores.append(action)
    files = connection.execute("SELECT COUNT(*) FROM paper_files").fetchone()[0]
    return {"files": files, "ok": files - len(problems), "problems": problems,
            "papers_claiming_fulltext_without_file": claimed, "restore": restores}


# ---------------------------------------------------------------- citations / S2
S2_FIELDS = "title,authors,year,externalIds,venue,abstract,url"


def semantic_scholar_row(item: dict[str, Any]) -> dict[str, Any]:
    ids = item.get("externalIds") or {}
    return {"title": item.get("title"), "authors": [a.get("name") for a in item.get("authors") or []], "year": item.get("year"),
            "doi": ids.get("DOI"), "arxiv": ids.get("ArXiv"), "pmid": ids.get("PubMed"), "semantic_scholar_id": item.get("paperId"),
            "venue": item.get("venue"), "abstract": item.get("abstract"), "url": item.get("url"), "metadata_source": "Semantic Scholar"}


def semantic_scholar_search(query: str, limit: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"query": query, "limit": limit, "fields": S2_FIELDS})
    data = get_json(f"https://api.semanticscholar.org/graph/v1/paper/search?{params}")
    if data is None:
        raise SystemExit("Semantic Scholar request failed (rate limited or unavailable); retry later or use --provider openalex")
    return [semantic_scholar_row(item) for item in data.get("data") or []]


def forward_citations(paper: dict[str, Any], provider: str, limit: int, email: str | None) -> list[dict[str, Any]]:
    if provider == "semanticscholar":
        ident = f"DOI:{paper['doi']}" if paper.get("doi") else paper.get("semantic_scholar_id")
        if not ident:
            return []
        data = get_json(f"https://api.semanticscholar.org/graph/v1/paper/{urllib.parse.quote(ident, safe=':/')}/citations?fields={S2_FIELDS}&limit={limit}") or {}
        return [semantic_scholar_row(item.get("citingPaper") or {}) for item in data.get("data") or []]
    work_id = paper.get("openalex_id")
    if not work_id and paper.get("doi"):
        work_id = (get_json(f"https://api.openalex.org/works/doi:{urllib.parse.quote(paper['doi'], safe='/')}", email) or {}).get("id")
    if not work_id:
        return []
    short = str(work_id).rsplit("/", 1)[-1]
    data = get_json(f"https://api.openalex.org/works?filter=cites:{short}&per-page={limit}", email) or {}
    return [{"title": w.get("title"), "authors": [a.get("author", {}).get("display_name") for a in w.get("authorships", [])],
             "year": w.get("publication_year"), "doi": w.get("doi"), "openalex_id": w.get("id"), "metadata_source": "OpenAlex"}
            for w in data.get("results", [])]
