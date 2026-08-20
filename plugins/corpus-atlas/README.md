# Corpus Atlas

Corpus Atlas turns searches into durable, local-first investigation workspaces. It stores source
provenance, atomic claims, evidence relationships, timestamps, and optional local attachments without
requiring a hosted account.

It is designed for scientific research, financial and company analysis, policy and social questions,
and source-grounded news investigations. It does not decide truth automatically: it makes the evidence
behind a conclusion inspectable and reusable.

## Install for development

```bash
git clone https://github.com/Li-Hongmin/corpus-atlas.git
cd corpus-atlas
uv tool install -e .
```

Python 3.11 or newer is required. The runtime uses only the Python standard library.

## Quick start

```bash
corpus-atlas init investigations/example \
  --title "Example investigation" \
  --question "What observation would change the decision?" \
  --domain general

corpus-atlas source add investigations/example \
  --title "Primary source" --url "https://example.org/source" \
  --source-type official-record --accessed-at 2026-08-20

corpus-atlas claim add investigations/example \
  --text "A falsifiable proposition" --kind observed

corpus-atlas status investigations/example
```

Each investigation contains `atlas.sqlite3`, a human-readable `atlas.json`, and an ignored
`attachments/` directory. Exported JSON is portable; attachments remain local unless deliberately
shared.

## Codex plugin

The bundled `build-research-corpus` skill guides an agent through decision-focused acquisition,
claim atomization, competing evidence, domain-specific source rules, and bounded reporting.

## Privacy and evidence boundary

Local-first does not mean automatically private: files remain under the user's control, but users
must still avoid committing confidential material or personal data. Corpus Atlas records provenance;
database integrity or source count never proves a substantive claim.

## License

MIT
