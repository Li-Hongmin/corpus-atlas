# Corpus Atlas

Public marketplace repository for the [Corpus Atlas plugin](plugins/corpus-atlas/README.md).

Corpus Atlas creates local, traceable investigation workspaces for scientific research, financial
and company analysis, public policy, social issues, and news. It preserves sources, timestamps,
atomic claims, evidence relationships, and optional attachments without requiring a hosted account.

## Install in Codex

```bash
codex plugin marketplace add Li-Hongmin/corpus-atlas
```

Then install **Corpus Atlas** from the Plugins Directory.

The repository has not yet been published at the URL above; the command becomes active after the
maintainer creates and pushes the public GitHub repository.

## Develop

```bash
cd plugins/corpus-atlas
uv run corpus-atlas --help
uv run --with pytest pytest -q
```

## License

MIT
