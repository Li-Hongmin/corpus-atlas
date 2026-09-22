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

The repository is public at [Li-Hongmin/corpus-atlas](https://github.com/Li-Hongmin/corpus-atlas).

## Develop

```bash
cd plugins/corpus-atlas
uv run corpus-atlas --help
uv run --with pytest pytest -q
```

## License

MIT
