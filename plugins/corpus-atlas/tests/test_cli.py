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
