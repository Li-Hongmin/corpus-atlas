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

### Hypothesis-driven discovery

Corpus Atlas can also maintain the reasoning state of an iterative scientific investigation. Keep
competing hypotheses explicit, then record the observations that can distinguish them instead of
using model confidence as a substitute for evidence.

```bash
H1=$(corpus-atlas claim add investigations/example \
  --text "A condition-induced working state changes the relevant mechanism" \
  --kind hypothesis)
H0=$(corpus-atlas claim add investigations/example \
  --text "The nominal static state is sufficient" \
  --kind hypothesis)

corpus-atlas claim relate investigations/example \
  --from "$H1" --to "$H0" --relation competes-with

corpus-atlas probe add investigations/example \
  --title "Discriminating calculation" \
  --target-claim "$H1" --modality DFT \
  --question "Does the candidate state survive first-principles relaxation and alter the relevant barrier?" \
  --if-true "The state survives and changes the mechanistic ordering" \
  --if-false "The state collapses or is mechanistically irrelevant" \
  --cost medium --priority 0.9

corpus-atlas probe list investigations/example --status planned
```

`claim relate` stores reasoning relations such as `competes-with`, `depends-on`, `predicts`, and
`explains`; it does not turn one claim into source evidence for another. `probe` stores planned
literature checks, computations, measurements, or experiments together with their expected outcomes.
Recording a probe result does not automatically promote a hypothesis to truth.

The bundled `discover-scientific-mechanism` skill adds a falsification-driven workflow for scientific
discovery. The CO2-hydrogenation example under `examples/co2-hydrogenation-discovery/` demonstrates
how to use public literature, ML potentials, targeted DFT, reaction networks, kinetics, and prospective
experimental validation without defining novelty as prediction accuracy.

### Scholarly library

```bash
corpus-atlas paper discover investigations/example \
  --query "causal inference under distribution shift" --provider openalex --cache
corpus-atlas paper import investigations/example --from literature-matrix.csv
corpus-atlas paper search investigations/example "distribution shift"
corpus-atlas paper retrieve investigations/example --paper smith2026causal \
  --url 'https://repository.example/paper.pdf' --source 'Institutional repository' \
  --license 'CC-BY-4.0' --role main
corpus-atlas paper dedupe investigations/example
corpus-atlas paper export-bib investigations/example --output corpus.bib
```

Paper records retain DOI, arXiv, PMID, Semantic Scholar and OpenAlex identifiers, abstracts,
backward references, forward citations, reading status, full-text status, and separately recorded
main-text and supplementary attachments. A paper belongs to the corpus when its record is admitted;
a PDF is optional.

## Codex plugin

The bundled `build-research-corpus` skill guides an agent through decision-focused acquisition,
claim atomization, competing evidence, domain-specific source rules, and bounded reporting. The
`discover-scientific-mechanism` skill builds on that evidence layer to maintain competing hypotheses,
select discriminating probes, record falsification attempts, and prepare prospective validation.

## Privacy and evidence boundary

Local-first does not mean automatically private: files remain under the user's control, but users
must still avoid committing confidential material or personal data. Corpus Atlas records provenance;
database integrity, source count, a successful simulation, or an AI-generated hypothesis never proves
a substantive scientific claim.

## License

MIT
