# Third-party components & licenses

fleetmem itself is **AGPL-3.0** (see `LICENSE` and `NOTICE`). It does not bundle or resell any third-party software — you
install these yourself, and each is **free and permissively licensed**. There is nothing to buy and no
new license to obtain; you simply inherit the same open-source licenses below (keep their notices if
you redistribute). Verified 2026-07-08.

## Runtime / infrastructure
| Component | Role | License |
|-----------|------|---------|
| PostgreSQL | database | PostgreSQL License (permissive, BSD-style) |
| pgvector | vector column/index | PostgreSQL License |
| Python 3 | runtime | PSF License |
| Flask | web framework | BSD-3-Clause |
| gunicorn | WSGI server | MIT |
| psycopg2-binary | Postgres driver (prebuilt wheel) | LGPL-3.0 (used as a library — no obligation on your code) |
| PyYAML | config parsing | MIT |
| requests | HTTP client | Apache-2.0 |
| mcp (Model Context Protocol SDK) | MCP server/tool transport (`mcp/server.py`) | MIT |
| nginx | mTLS front | BSD-2-Clause |
| Ollama | local LLM server | MIT |

## Models (you pull these yourself via Ollama/Hugging Face; their licenses come from the provider)
| Model | Role | License |
|-------|------|---------|
| BAAI/bge-m3 | embeddings | MIT |
| Qwen3-Embedding-0.6B | embeddings (optional challenger) | Apache-2.0 |
| Qwen3-30B-A3B-Instruct-2507 | extraction / edge classification | Apache-2.0 |

You may swap in any embedding/generation model your Ollama endpoint serves; check that model's own
license. fleetmem never ships model weights — you download them, so you accept the model license
directly from its source.

## Security material
fleetmem ships with **no certificates, keys, or tokens**. On first run the box mints its **own** local
CA + server cert (`fleetmem-init-pki.sh`) and your genesis manager's cert + token — all freshly
generated on your hardware. None of the author's security material is included.

Sources: PostgreSQL License (postgresql.org/about/licence), pgvector (github.com/pgvector/pgvector),
Ollama (github.com/ollama/ollama), bge-m3 (huggingface.co/BAAI/bge-m3),
Qwen3-30B-A3B-Instruct-2507 (huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507).
