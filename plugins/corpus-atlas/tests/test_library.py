"""Offline tests for library maintenance, retrieval sources, and the network layer."""
import csv
import io
import json
import sqlite3
import ssl
import tarfile
import urllib.error
import zipfile

import pytest

from corpus_atlas import fulltext as ft
from corpus_atlas import library as lib
from corpus_atlas import scholarly
from test_fulltext import PDF, HTML, fake_web, run, workspace


def member(name, size=100):
    info = zipfile.ZipInfo(name)
    info.file_size = size
    return info


def db(root):
    connection = sqlite3.connect(root / "atlas.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


# ------------------------------------------------------------ keys / identity
@pytest.mark.parametrize("record,expected", [
    ({"title": "A Clear Path to Evidence", "authors": ["María García"], "year": 2026}, "garcia2026clear"),
    ({"title": "数据", "authors": [], "year": 2025}, "unknown2025work"),
    ({"title": "The Laplacian Eigenmap", "author": "Belkin, Mikhail"}, "belkinlaplacian"),
    ({"title": "Attention is all you need", "authors": ["Ashish Vaswani"], "year": "2017"}, "vaswani2017attention"),
])
def test_citation_key_matches_google_scholar_style(record, expected):
    assert scholarly.citation_key(record) == expected


def test_identifier_normalization():
    assert scholarly.normalize_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert scholarly.normalize_arxiv("arXiv:2101.00001") == "2101.00001"
    assert scholarly.normalized_title("Évaluation: of  X!") == "evaluation of x"


# ------------------------------------------------------------ supplements
def test_supplement_filter_excludes_html_figures_but_keeps_evidence_files():
    assert not ft.is_supplement_member(member("article_Fig3_HTML.jpg"))
    assert ft.is_supplement_member(member("article_MOESM1_ESM.pdf"))
    assert ft.is_supplement_member(member("table-s1.xlsx"))
    assert ft.is_supplement_member(member("supplementary-figure.png"))
    assert not ft.is_supplement_member(member("empty.pdf", 0))


def test_springer_nature_discovery_keeps_only_esm_assets():
    page = ('<a href="https://media.springernature.com/original/x/esm/file_ESM.pdf">supp</a>'
            '<a href="https://www.nature.com/articles/example/figures/1">figure</a>'
            '<a href="https://static-content.springer.com/esm/art%3A10.1186/x/MediaObjects/12_MOESM1_ESM.docx">s1</a>')
    assert lib.springer_nature_links(page, "https://www.nature.com/articles/example") == [
        "https://media.springernature.com/original/x/esm/file_ESM.pdf",
        "https://static-content.springer.com/esm/art%3A10.1186/x/MediaObjects/12_MOESM1_ESM.docx"]


def test_elsevier_and_generic_rules_skip_figures():
    els = '<a href="https://ars.els-cdn.com/content/image/1-s2.0-S1-mmc1.pdf">mmc</a><a href="https://ars.els-cdn.com/content/image/1-s2.0-S1-gr1.jpg">f</a>'
    assert lib.elsevier_links(els, "https://www.sciencedirect.com/x") == ["https://ars.els-cdn.com/content/image/1-s2.0-S1-mmc1.pdf"]
    gen = '<a href="/doi/suppl/10.1021/x/suppl_file/x_si_001.pdf">SI</a><a href="/doi/figures/fig1.png">fig</a>'
    assert lib.generic_links(gen, "https://pubs.acs.org/doi/10.1021/x") == ["https://pubs.acs.org/doi/suppl/10.1021/x/suppl_file/x_si_001.pdf"]


def test_supplement_entries_accept_structured_and_simple_urls():
    simple = lib.supplement_entries({"supplement_urls": "https://a.org/s1.pdf; https://a.org/s2.csv", "oa_license": "cc-by"})
    assert [e["url"] for e in simple] == ["https://a.org/s1.pdf", "https://a.org/s2.csv"]
    assert simple[0]["license"] == "cc-by" and simple[0]["source"] == "project matrix"
    structured = lib.supplement_entries({"supplement_urls": json.dumps([{"url": "https://b.org/x", "source": "Zenodo", "license": "cc0", "filename": "data.zip"}])})
    assert structured == [{"url": "https://b.org/x", "source": "Zenodo", "license": "cc0", "filename": "data.zip"}]


def test_matrix_supplements_and_publisher_page_are_downloaded(tmp_path, monkeypatch, capsys):
    root = tmp_path / "ws"
    run(monkeypatch, capsys, "init", root, "--title", "T", "--question", "Q", "--domain", "science")
    matrix = tmp_path / "literature-matrix.csv"
    with matrix.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["title", "year", "doi", "author", "supplement_urls"])
        writer.writeheader()
        writer.writerow({"title": "Matrix paper", "year": 2022, "doi": "10.9/m", "author": "A B", "supplement_urls": "https://data.org/s1.csv"})
        writer.writerow({"title": "Nature paper", "year": 2023, "doi": "10.1038/n1", "author": "C D", "supplement_urls": ""})
    run(monkeypatch, capsys, "paper", "import-matrix", root, "--from", matrix)
    xlsx = io.BytesIO()
    with zipfile.ZipFile(xlsx, "w") as z:
        z.writestr("xl/workbook.xml", "<x/>")
    fake_web(monkeypatch, {
        "https://data.org/s1.csv": (200, "text/csv", b"a,b\n1,2\n"),
        "https://doi.org/10.1038/n1": (200, "text/html", b'<a href="https://media.springernature.com/x/esm/41_MOESM1_ESM.xlsx">S1</a><a href="/figures/1">f</a>'),
        "https://media.springernature.com/x/esm/41_MOESM1_ESM.xlsx": (200, "application/octet-stream", xlsx.getvalue()),
        "https://doi.org/10.9/m": (200, "text/html", b"<html>no links</html>"),
    })
    report = {e["citation_key"]: e for e in json.loads(run(monkeypatch, capsys, "paper", "supplements", root, "--apply"))}
    assert report["b2022matrix"]["status"] == "downloaded"
    assert report["b2022matrix"]["files"][0]["file"].endswith(".csv")
    assert report["d2023nature"]["files"][0]["file"].endswith(".xlsx")
    rows = db(root).execute("SELECT role, source_name, license FROM paper_files").fetchall()
    assert {r["role"] for r in rows} == {"supplement"}
    assert any(r["source_name"].startswith("Springer Nature article page") for r in rows)


def test_html_supplement_is_rejected_and_queued(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Walled", "doi": "10.9/w", "year": 2020, "authors": ["E F"],
                                                      "supplement_urls": "https://pub.org/supp"}])
    fake_web(monkeypatch, {"https://pub.org/supp": (200, "text/html", HTML)})
    report = json.loads(run(monkeypatch, capsys, "paper", "supplements", root, "--apply", "--no-publisher-pages"))
    assert report[0]["status"] == "failed" and "HTML" in report[0]["errors"][0]["error"]
    queue = json.loads(run(monkeypatch, capsys, "paper", "browser-queue", root))
    assert queue[0]["kind"] == "supplement" and queue[0]["landing_url"] == "https://doi.org/10.9/w"


# ------------------------------------------------------------ main text
def test_supplement_pdf_does_not_satisfy_main_text(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Main", "doi": "10.9/x", "year": 2020, "authors": ["G H"],
                                                      "supplement_urls": "https://s.org/s.pdf"}])
    fake_web(monkeypatch, {"https://s.org/s.pdf": (200, "application/pdf", PDF)})
    run(monkeypatch, capsys, "paper", "supplements", root, "--apply", "--no-publisher-pages")
    status = db(root).execute("SELECT fulltext_status FROM papers").fetchone()[0]
    assert status == "metadata-only"


def test_record_oa_pdf_and_ncbi_package(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [
        {"title": "Matrix pdf", "doi": "10.9/a", "year": 2020, "authors": ["I J"], "open_access_pdf": "http://repo.org/a.pdf"},
        {"title": "Package pdf", "doi": "10.9/b", "year": 2020, "authors": ["K L"]}])
    package = io.BytesIO()
    with tarfile.open(fileobj=package, mode="w:gz") as tar:
        for name, data in (("PMC1/small.pdf", b"%PDF-1 tiny"), ("PMC1/main.pdf", PDF + b"x" * 50), ("PMC1/fig.jpg", b"\xff\xd8\xff")):
            info = tarfile.TarInfo(name); info.size = len(data); tar.addfile(info, io.BytesIO(data))
    oa = b'<OA><records><record id="PMC1" license="CC BY"><link format="tgz" href="ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/x.tar.gz"/></record></records></OA>'
    seen = fake_web(monkeypatch, {
        "https://repo.org/a.pdf": (200, "application/pdf", PDF),
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search": {"resultList": {"result": [{"doi": "10.9/b", "pmcid": "PMC1", "isOpenAccess": "Y"}]}},
        "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id=PMC1": (200, "application/xml", oa),
        "https://ftp.ncbi.nlm.nih.gov/pub/pmc/x.tar.gz": (200, "application/x-gzip", package.getvalue()),
    })
    summary = json.loads(run(monkeypatch, capsys, "paper", "retrieve-all", root))
    assert summary["downloaded"] == 2
    assert "https://repo.org/a.pdf" in [s["url"] for s in seen]  # http upgraded to https
    rows = {r["source_name"]: r for r in db(root).execute("SELECT * FROM paper_files")}
    assert rows["NCBI OA package: PMC1"]["license"] == "CC BY"
    assert rows["NCBI OA package: PMC1"]["bytes"] == len(PDF) + 50


def test_failed_main_text_goes_to_browser_queue_and_import(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Closed", "doi": "10.9/c", "year": 2020, "authors": ["M N"],
                                                      "open_access_pdf": "https://pub.org/c.pdf"}])
    fake_web(monkeypatch, {"https://pub.org/c.pdf": (403, "", b"")})
    run(monkeypatch, capsys, "paper", "retrieve-all", root)
    queue_path = root / "reports" / "browser-queue.csv"
    rows = list(csv.DictReader(queue_path.open()))
    assert rows[0]["kind"] == "main" and rows[0]["citation_key"] == "n2020closed"
    fetched = tmp_path / "downloads"; fetched.mkdir()
    (fetched / "c.pdf").write_bytes(PDF); (fetched / "s1.csv").write_text("x,y\n")
    results = tmp_path / "browser-results.csv"
    with results.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["citation_key", "kind", "file", "url", "source", "license"])
        writer.writeheader()
        writer.writerow({"citation_key": "n2020closed", "kind": "main", "file": "downloads/c.pdf", "url": "https://pub.org/c.pdf", "source": "publisher page (browser)", "license": "cc-by"})
        writer.writerow({"citation_key": "n2020closed", "kind": "supplement", "file": "downloads/s1.csv", "url": "https://pub.org/s1.csv", "source": "publisher page (browser)", "license": "cc-by"})
        writer.writerow({"citation_key": "n2020closed", "kind": "supplement", "file": "downloads/s1.csv", "url": "", "source": "", "license": ""})
    preview = json.loads(run(monkeypatch, capsys, "paper", "import-browser-results", root, "--from", results))
    assert [r["status"] for r in preview] == ["ready-preview", "ready-preview", "needs-source-and-license"]
    applied = json.loads(run(monkeypatch, capsys, "paper", "import-browser-results", root, "--from", results, "--apply"))
    assert [r["status"] for r in applied][:2] == ["imported", "imported"]
    assert db(root).execute("SELECT fulltext_status FROM papers").fetchone()[0] == "local-fulltext"


# ------------------------------------------------------------ dedupe / normalize / bib
def test_dedupe_preview_then_apply_merges_title_duplicates(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [
        {"title": "Graph neural networks for chemistry", "year": 2021, "authors": ["Ada Lovelace"], "arxiv": "2101.00001"},
        {"title": "Graph Neural Networks for Chemistry.", "year": 2022, "authors": ["Ada Lovelace"], "doi": "10.9/gnn", "venue": "J. Chem"}])
    preview = json.loads(run(monkeypatch, capsys, "paper", "dedupe", root))
    assert len(preview) == 1 and preview[0]["applied"] is False
    assert db(root).execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
    applied = json.loads(run(monkeypatch, capsys, "paper", "dedupe", root, "--apply"))
    assert applied[0]["keep"] == "lovelace2022graph"
    row = db(root).execute("SELECT * FROM papers").fetchone()
    assert (row["doi"], row["arxiv"]) == ("10.9/gnn", "2101.00001")


def test_normalize_renames_keys_and_attachments(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [{"title": "Laplacian eigenmaps", "year": 2003, "authors": ["Mikhail Belkin"], "doi": "10.9/l"}])
    connection = db(root)
    connection.execute("UPDATE papers SET citation_key='legacy-key'")
    connection.commit()
    fake_web(monkeypatch, {"https://pub.org/l.pdf": (200, "application/pdf", PDF)})
    run(monkeypatch, capsys, "paper", "retrieve", root, "--paper", "legacy-key", "--url", "https://pub.org/l.pdf", "--source", "S", "--license", "cc-by")
    preview = json.loads(run(monkeypatch, capsys, "paper", "normalize", root))
    assert preview[0]["to"] == "belkin2003laplacian" and preview[0]["files"][0]["to"] == "attachments/belkin2003laplacian.pdf"
    run(monkeypatch, capsys, "paper", "normalize", root, "--apply")
    assert (root / "attachments" / "belkin2003laplacian.pdf").read_bytes() == PDF
    assert json.loads(run(monkeypatch, capsys, "paper", "check", root))["problems"] == []


def test_bibtex_export_is_canonical(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [
        {"title": "Fast & 100% exact_solvers", "year": 2020, "authors": ["Ludwig van Beethoven", "Curie, Marie"], "doi": "10.9/f", "venue": "Journal of Tests"},
        {"title": "A conference work", "year": 2021, "authors": ["Bo Li"], "venue": "Proceedings of the Conference on X"},
        {"title": "A preprint", "year": 2022, "authors": ["Cy Wu"], "arxiv": "2201.00001"}])
    out = tmp_path / "lib.bib"
    run(monkeypatch, capsys, "paper", "export-bib", root, "--output", out)
    text = out.read_text()
    assert "@article{beethoven2020fast," in text  # particle-aware family name
    assert "author = {van Beethoven, Ludwig and Curie, Marie}" in text
    assert r"title = {Fast \& 100\% exact\_solvers}" in text
    assert "@inproceedings{li2021conference," in text and "booktitle = {Proceedings of the Conference on X}" in text
    assert "eprint = {2201.00001}" in text and "archiveprefix = {arXiv}" in text


# ------------------------------------------------------------ cache-record
def test_cache_record_accepts_yaml_and_keeps_graph(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path, monkeypatch, capsys, [])
    source = tmp_path / "record.yaml"
    source.write_text("title: Evidence synthesis\nyear: 2024\nauthors:\n  - Ann Smith\ndoi: 10.9/e\nabstract: Evidence\nreferences: [\"10.9/r1\", \"10.9/r2\"]\ncitations:\n  - 10.9/c1\n")
    out = json.loads(run(monkeypatch, capsys, "paper", "cache-record", root, "--from", source))
    assert out["created"] == 1
    connection = db(root)
    assert connection.execute("SELECT abstract FROM papers").fetchone()[0] == "Evidence"
    assert connection.execute("SELECT COUNT(*) FROM paper_relations").fetchone()[0] == 3
    # re-caching without status keeps local-fulltext
    connection.execute("UPDATE papers SET fulltext_status='local-fulltext'"); connection.commit()
    run(monkeypatch, capsys, "paper", "cache-record", root, "--from", source)
    assert db(root).execute("SELECT fulltext_status FROM papers").fetchone()[0] == "local-fulltext"


def test_structured_json_records_key(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"records": [{"title": "One"}, {"title": "Two"}]}))
    assert [r["title"] for r in scholarly.load_records(path)] == ["One", "Two"]


# ------------------------------------------------------------ network layer
def test_http_get_retries_429_then_succeeds(monkeypatch):
    calls = []

    class Ok:
        status = 200
        headers = type("H", (), {"get_content_type": lambda self: "application/pdf"})()
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def read(self, n): return PDF
        def geturl(self): return "https://x.org/a.pdf"

    def urlopen(request, timeout, context):
        calls.append(request.get_header("User-agent"))
        assert isinstance(context, ssl.SSLContext)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "slow", {"Retry-After": "0"}, None)
        return Ok()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(ft.time, "sleep", lambda s: None)
    response = ft.http_get("https://x.org/a.pdf", ft.PDF_ACCEPT)
    assert response.status == 200 and len(calls) == 2
    assert calls[0] == ft.user_agent(None, api=False)


def test_http_get_does_not_retry_403(monkeypatch):
    calls = []
    def urlopen(request, timeout, context):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 403, "no", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert ft.http_get("https://x.org/b").status == 403 and len(calls) == 1


def test_ssl_context_loads_system_bundle():
    assert isinstance(ft.ssl_context(), ssl.SSLContext)
