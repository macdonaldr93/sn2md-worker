# sn2md-worker

Background worker that converts Supernote `.note` files from a Google Drive
folder into Markdown for an Obsidian vault, using
[`sn2md`](https://github.com/dsummersl/sn2md) with Gemini as the LLM
backend.

- Product context: [`docs/product-brief.md`](docs/product-brief.md)
- Implementation design: [`docs/technical-brief.md`](docs/technical-brief.md)
- Verification scripts: [`scripts/verify/README.md`](scripts/verify/README.md)

## Development

```sh
uv sync
uv run sn2md-worker
```

The service defaults to port 8080. `/healthz` and `/readyz` return 200 when
healthy. Configuration comes from `config.toml` (see `config.example.toml`)
with env-var overrides prefixed `SN2MD_WORKER__` (double-underscore nesting).
