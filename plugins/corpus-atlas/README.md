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

### Scholarly library

```bash
corpus-atlas paper discover investigations/example \
  --query "causal inference under distribution shift" --provider openalex --cache
corpus-atlas paper import investigations/example --from literature-matrix.csv
corpus-atlas paper search investigations/example "distribution shift"
corpus-atlas paper retrieve investigations/example --paper smith2026causal \
  --url 'https://repository.example/paper.pdf' --source 'Institutional repository' \
  --license 'CC-BY-4.0' --role main
corpus-atlas paper import-matrix investigations/example --from literature-matrix.csv  # open_access_pdf, supplement_urls
corpus-atlas paper cache-record investigations/example --from record.yaml      # JSON, YAML, CSV/TSV
corpus-atlas paper retrieve investigations/example --paper smith2026causal     # no --url: auto-resolve OA
corpus-atlas paper retrieve-all investigations/example --email you@example.org --max-workers 8 --transport auto
corpus-atlas paper supplements investigations/example --apply --max-workers 8
corpus-atlas paper browser-queue investigations/example                         # manual/browser worklist
corpus-atlas paper import-browser-results investigations/example --from results.csv [--apply]
corpus-atlas paper publications investigations/example          # preview; --apply records DOI + fetches published PDF [--prune-preprint]
corpus-atlas paper citations investigations/example --paper smith2026causal --cache
corpus-atlas paper check investigations/example --restore-from ~/backup   # preview restore by sha256
corpus-atlas paper dedupe investigations/example        # preview (shows deciding rule); --apply merges [--preferences prefs.yaml]
corpus-atlas paper normalize investigations/example     # canonical keys/filenames; preview; --apply
corpus-atlas paper export-bib investigations/example --output corpus.bib [--include-files]
```

Paper records retain DOI, arXiv, PMID, Semantic Scholar and OpenAlex identifiers, abstracts,
backward references, forward citations, reading status, full-text status, and separately recorded
main-text and supplementary attachments. A paper belongs to the corpus when its record is admitted;
a PDF is optional.

`retrieve-all` resolves legal open-access copies only (record/matrix `open_access_pdf`, OpenAlex
locations, Unpaywall when an email is given via `--email` or `CORPUS_ATLAS_EMAIL`, Europe PMC/PMC,
the NCBI PMC OA package, arXiv, bioRxiv/medRxiv, BMC/Springer OA), queried lazily and batched
(40 DOIs per OpenAlex request, 50 per Europe PMC request). It accepts only real PDFs and never
bypasses paywalls, logins, or CAPTCHAs. Requests use the system trust store plus common CA bundle
paths, retry 429/5xx with backoff (honouring `Retry-After`), and are rate limited per host
(`CORPUS_ATLAS_HOST_INTERVAL`, default 0.2 s). It is resumable and writes a JSON report,
`reports/fulltext-failures.csv`, and `reports/browser-queue.csv`.

`supplements` takes matrix `supplement_urls` (semicolon list or JSON), the Europe PMC supplementary
ZIP (true supplement members only; article figure renders are skipped), and publisher-page rules
(Springer Nature/BMC ESM, Elsevier `mmc`, ACS/Wiley/PLOS/Frontiers/MDPI/RSC SI links). HTML pages
are rejected. Unresolved items go to the browser queue; files fetched by hand are ingested with
`import-browser-results` (columns `citation_key,kind,file,url,source,license`), which requires a
source and license for every file. Attachments
are named with an extension inferred from magic bytes or content type and recorded with source,
license, and sha256. Discovery providers: `openalex`, `crossref`, `semanticscholar`.

`--transport auto|python|aria2` (retrieve-all, supplements): `auto` uses `aria2c` when it is on
PATH (input file, `--connections` per server/file, `--continue` resume in `.staging/aria2/`);
every payload is then validated (PDF magic / OA package, supplement rules) and recorded with the
same sha256/source/license rows as the Python transport. Failed aria2 downloads are retried with
Python unless `--no-python-fallback`; if aria2c cannot run, the whole batch uses Python.

`publications --apply` records the published DOI and venue (the arXiv ID is kept), then fetches the
published PDF from a non-preprint legal OA location (never arXiv/bioRxiv/`submittedVersion`). Once
verified (PDF, >=10 KB, and title coverage on pages 1-3 when `pdftotext` is installed), the old
attachment becomes `role=preprint`; `--prune-preprint` deletes it instead. `--no-download` skips the
PDF step.

`dedupe` keeps the record named in a preferences file (`--preferences`, default
`<workspace>/config/duplicate-preferences.yaml`; paper-lib format
`duplicates: {<sha256|citation key|DOI|arXiv>: {preferred: <citation key|DOI|arXiv|id>}}`), else the
best-scoring record; each group reports its `rule` (`preference:...` or `score:...`).
`--require-preferences` fails on groups without an explicit choice.

URLs are sanitised before they are stored or reported (DB, `atlas.json`, reports, browser queue):
credentials in the userinfo part and sensitive query keys (`token`, `key`, `apikey`, `api_key`,
`access_token`, `sig`, `signature`, `X-Amz-*`, `X-Goog-*`, `Expires`, `Policy`, `Key-Pair-Id`,
`session`, `auth`, ...) are dropped; a signed wrapper with an https `redirect_uri` is replaced by it.

YAML input uses PyYAML when installed (`pip install 'corpus-atlas[yaml]'`); otherwise a built-in
parser handles nested mappings/lists, quoted and multi-line scalars, flow `[]`/`{}`, `|`/`>` block
scalars, and comments, and rejects anchors, aliases, tags, and multiple documents with a line number.

## Codex plugin

The bundled `build-research-corpus` skill guides an agent through decision-focused acquisition,
claim atomization, competing evidence, domain-specific source rules, and bounded reporting.

## Privacy and evidence boundary

Local-first does not mean automatically private: files remain under the user's control, but users
must still avoid committing confidential material or personal data. Corpus Atlas records provenance;
database integrity or source count never proves a substantive claim.

## License

MIT
