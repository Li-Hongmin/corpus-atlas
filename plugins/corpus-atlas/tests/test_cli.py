import json
from pathlib import Path

from corpus_atlas.cli import main


def run(monkeypatch, capsys, *arguments):
    monkeypatch.setattr("sys.argv", ["corpus-atlas", *map(str, arguments)])
    main()
    return capsys.readouterr().out.strip()


def test_workspace_roundtrip(tmp_path: Path, monkeypatch, capsys):
    workspace = tmp_path / "investigation"
    run(monkeypatch, capsys, "init", workspace, "--title", "Test", "--question", "What changed?")
    source = run(monkeypatch, capsys, "source", "add", workspace, "--title", "Record",
                 "--source-type", "official-record", "--url", "https://example.org")
    claim = run(monkeypatch, capsys, "claim", "add", workspace, "--text", "An event occurred")
    run(monkeypatch, capsys, "link", "add", workspace, "--source", source,
        "--claim", claim, "--relation", "supports", "--locator", "section 1")
    status = json.loads(run(monkeypatch, capsys, "status", workspace))
    assert status["sources"] == 1
    assert status["claims"] == 1
    assert status["evidence_links"] == 1
    assert status["unlinked_claims"] == 0
    snapshot = json.loads((workspace / "atlas.json").read_text())
    assert snapshot["evidence_links"][0]["source_id"] == source


def test_scholarly_import_search_and_bibtex(tmp_path: Path, monkeypatch, capsys):
    workspace = tmp_path / "papers"
    records = tmp_path / "records.json"
    records.write_text(json.dumps([{
        "title": "A Reusable Research Corpus",
        "authors": ["Ada Lovelace", "Alan Turing"],
        "year": 2026,
        "doi": "https://doi.org/10.1234/example",
        "abstract": "A local evidence system.",
        "references": ["doi:10.1000/anchor"],
    }]))
    run(monkeypatch, capsys, "init", workspace, "--title", "Papers", "--question", "What is known?", "--domain", "science")
    result = json.loads(run(monkeypatch, capsys, "paper", "import", workspace, "--from", records))
    assert result == {"created": 1, "updated": 0, "skipped": 0}
    repeated = json.loads(run(monkeypatch, capsys, "paper", "import", workspace, "--from", records))
    assert repeated == {"created": 0, "updated": 1, "skipped": 0}
    found = json.loads(run(monkeypatch, capsys, "paper", "search", workspace, "Reusable"))
    assert found[0]["doi"] == "10.1234/example"
    fake_pdf = tmp_path / "paper.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\nminimal test payload")
    retrieved = json.loads(run(monkeypatch, capsys, "paper", "retrieve", workspace,
        "--paper", found[0]["citation_key"], "--url", fake_pdf.as_uri(),
        "--source", "Test repository", "--license", "CC-BY-4.0"))
    assert retrieved["bytes"] == fake_pdf.stat().st_size
    status = json.loads(run(monkeypatch, capsys, "status", workspace))
    assert status["papers"] == 1
    assert status["paper_relations"] == 1
    assert status["paper_files"] == 1
    bibliography = tmp_path / "corpus.bib"
    run(monkeypatch, capsys, "paper", "export-bib", workspace, "--output", bibliography)
    text = bibliography.read_text()
    assert "@article{lovelace2026reusable" in text
    assert "10.1234/example" in text
