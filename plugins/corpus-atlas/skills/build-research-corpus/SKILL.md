---
name: build-research-corpus
description: Build and maintain a local, traceable investigation corpus from web pages, documents, datasets, scholarly papers, company filings, government records, policy materials, and news. Use for scientific literature research, financial or company analysis, public-policy and social-issue investigations, event timelines, source verification, competing-hypothesis analysis, claim-evidence mapping, and any task where searches and retrieved materials should remain reusable in a local workspace. Do not use for casual questions that do not benefit from persistent sources or for unsupported automatic truth judgments.
---

# Build Research Corpus

Turn an investigation into a durable local workspace. Preserve sources before synthesizing them,
and keep direct observations separate from interpretations and hypotheses.

## Start From The Decision

State:

1. the decision or belief that the investigation could change;
2. the plausible outcomes;
3. the cheapest direct observation that distinguishes those outcomes.

If no possible result changes the next action, answer directly without creating a corpus.

## Create Or Reuse A Workspace

Prefer a user-supplied directory. Otherwise create a descriptive directory beneath the current
working directory. Initialize it with:

```bash
corpus-atlas init /absolute/path/to/investigation --title "Investigation title" \
  --question "Decision-relevant question" --domain general
```

Use `science`, `finance`, or `public-affairs` instead of `general` when one source regime dominates.
Read the matching file in `references/` before collecting domain-specific evidence.

## Acquire Sources

Search current, authoritative sources appropriate to the domain. Prefer primary records and direct
data, then independent expert analysis, then contemporaneous reporting. Treat search rankings,
snippets, social posts, generated summaries, and market commentary as discovery signals unless they
are themselves the object of study.

For each admitted source, save or reference the closest lawful original and register it:

```bash
corpus-atlas source add /absolute/path/to/investigation \
  --title "Source title" --url "https://example.org/source" \
  --source-type official-record --publisher "Publisher" \
  --published-at 2026-08-20 --accessed-at 2026-08-20 \
  --file /absolute/path/to/local-copy.pdf
```

Do not bypass authentication, paywalls, robots restrictions, or access controls. Record a missing
or inaccessible source explicitly instead of replacing it with an unverified copy.

## Atomize Claims And Relationships

Create one falsifiable proposition per claim:

```bash
corpus-atlas claim add /absolute/path/to/investigation \
  --text "Atomic proposition" --kind observed --status unresolved
```

Connect a source to a claim only after reading the relevant material:

```bash
corpus-atlas link add /absolute/path/to/investigation \
  --source SOURCE_ID --claim CLAIM_ID --relation supports \
  --locator "p. 12, Table 2" --quote "Short exact excerpt"
```

Use `supports`, `contradicts`, `qualifies`, `contextualizes`, or `mentions`. A citation is not
automatically support. Record the locator, the shortest useful quotation, and any scope limitation.

## Compare Explanations

Actively seek disconfirming records, alternative causal explanations, base rates, missing periods,
selection mechanisms, corrections, and conflicts of interest. Do not average incompatible evidence
without explaining why it differs. Separate:

- directly supported facts;
- interpretations derived from several facts;
- hypotheses that remain testable;
- unknowns caused by missing or inaccessible evidence.

For fast-moving topics, preserve event time, publication time, access time, and later correction time
separately. Never use a later report as if it were contemporaneous knowledge.

## Report And Preserve

Inspect coverage before concluding:

```bash
corpus-atlas status /absolute/path/to/investigation
corpus-atlas export /absolute/path/to/investigation --output corpus.json
```

Report the bounded answer, strongest evidence, strongest contradiction, unresolved uncertainty,
search cutoff, and the next observation most likely to change the conclusion. Do not claim that a
large corpus, clean database, or successful download proves the conclusion.
