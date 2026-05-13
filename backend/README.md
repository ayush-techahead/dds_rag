# rag_chatbot_backend

FastAPI backend base for an admin panel and future knowledge-grounded chatbot. This project currently implements only the foundational backend with MongoDB, JWT authentication, users, and a simple chat placeholder flow.

It intentionally does not include document ingestion, website crawling, embeddings, vector search, Qdrant client logic, LLM integration, LangChain, LangGraph, or LlamaIndex.

## Stack

- Python 3.11+
- FastAPI and Uvicorn
- MongoDB with Motor and Beanie ODM
- Pydantic v2 and pydantic-settings
- JWT auth with PyJWT
- Password hashing with Passlib
- Docker and Docker Compose
- Ruff
- Pytest

## Project Structure

The app is a modular monolith. Each business area owns its models, schemas, repositories, and services.

- `app/api`: versioned HTTP endpoints and dependencies
- `app/core`: config, security, logging, exceptions
- `app/db`: MongoDB connection and Beanie initialization
- `app/modules/auth`: registration and login logic
- `app/modules/users`: user model and user access logic
- `app/modules/chat`: chat session and placeholder message logic
- `app/modules/documents`: PDF/Markdown upload metadata, local file storage, text extraction, and indexing status
- `app/modules/websites`: website source configuration, scheduled crawl jobs, page metadata, and indexing
- `app/modules/ingestion`: reserved for parsing, cleaning, chunking, indexing
- `app/modules/embeddings`: reserved for embedding generation
- `app/modules/knowledge_base`: reserved for retrieval and RAG context building
- `app/modules/vector_store`: reserved for Qdrant integration

MongoDB stores application data and document metadata. Qdrant stores embedded document chunks for future retrieval.

## Environment

Create a local `.env` file:

```bash
cp .env.example .env
```

For local development outside Docker, update `MONGODB_URI`:

```env
MONGODB_URI=mongodb://localhost:27017
```

Required environment variables:

- `PROJECT_NAME`
- `ENVIRONMENT`
- `DEBUG`
- `API_V1_PREFIX`
- `MONGODB_URI`
- `MONGODB_DB_NAME`
- `JWT_SECRET_KEY`
- `JWT_ALGORITHM`
- `ACCESS_TOKEN_EXPIRE_MINUTES`
- `BACKEND_CORS_ORIGINS`
- `QDRANT_URL`
- `QDRANT_API_KEY`
- `QDRANT_COLLECTION_NAME`
- `STORAGE_DIR`
- `MAX_UPLOAD_SIZE_MB`
- `EMBEDDING_DIMENSION`
- `DOCUMENT_CHUNK_SIZE`
- `DOCUMENT_CHUNK_OVERLAP`
- `DOCUMENT_CHUNK_STRATEGY`
- `SCHEDULER_ENABLED`
- `SCHEDULER_TICK_SECONDS`
- `WEBSITE_CRAWL_TIMEOUT_SECONDS`
- `WEBSITE_MAX_HTML_BYTES`

## Run Locally

Start MongoDB locally, then run:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

API docs:

- Swagger UI: `http://localhost:8000/docs`
- OpenAPI JSON: `http://localhost:8000/openapi.json`
- Committed OpenAPI for agents / codegen: `openapi.json` (regenerate with the command in `docs/API_FOR_FRONTEND_AGENT.md`)

## Run With Docker

```bash
cp .env.example .env
docker compose up --build
```

Services:

- Backend: `http://localhost:8000`
- MongoDB: `localhost:27017`
- Qdrant REST: `http://localhost:6333`
- Qdrant gRPC: `localhost:6334`

## Tests

Tests use the separate MongoDB database `rag_chatbot_backend_test`. Make sure MongoDB is running locally before running tests outside Docker.

```bash
pytest
```

Run linting:

```bash
ruff check .
```

## API Routes

- `GET /api/v1/health`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `GET /api/v1/users/me`
- `POST /api/v1/chat/sessions`
- `GET /api/v1/chat/sessions`
- `GET /api/v1/chat/sessions/{session_id}`
- `POST /api/v1/chat/sessions/{session_id}/messages`
- `POST /api/v1/documents/upload`
- `POST /api/v1/documents/zip-sessions` (phase 1: flat Markdown manifest, no embeddings)
- `POST /api/v1/documents/zip-sessions/{session_id}/ingest` (phase 2: batch embed + Qdrant)
- `DELETE /api/v1/documents/zip-sessions/{session_id}`
- `GET /api/v1/documents`
- `GET /api/v1/documents/{document_id}`
- `POST /api/v1/websites`
- `GET /api/v1/websites`
- `GET /api/v1/websites/{website_id}`
- `PATCH /api/v1/websites/{website_id}`
- `POST /api/v1/websites/{website_id}/crawl`
- `GET /api/v1/websites/{website_id}/crawl-jobs`

## Document Upload

Authenticated users can upload PDF and Markdown files:

```bash
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -F "file=@/path/to/document.pdf"
```

Single-file upload stores the file under `STORAGE_DIR`, extracts text (`pypdf` or UTF-8 Markdown), chunks with `resolve_chunker_for_ingestion` when `DOCUMENT_CHUNK_STRATEGY` is `auto`, embeds with **OpenAI** embeddings (`OpenAIEmbeddingProvider`, same as chat RAG queries), and upserts to Qdrant. Requires `OPENAI_API_KEY`.

**Bulk Markdown ZIP (two phases):** **(1)** `POST /api/v1/documents/zip-sessions` uploads the `.zip`, stores it until `ZIP_SESSION_TTL_HOURS`, and returns a **sorted flat list** of every indexable Markdown path (root and nested folders, `/` separators). **(2)** `POST /api/v1/documents/zip-sessions/{session_id}/ingest` with JSON body runs one embedding batch: either **`markdown_skip`** / **`markdown_path_limit`** (defaults: skip = session `next_suggested_skip`, limit = `ZIP_INGEST_PATH_BATCH_DEFAULT`, capped by `ZIP_INGEST_MAX_PATH_BATCH`) or **`path_indices`** (explicit manifest rows, max `ZIP_INGEST_MAX_PATH_INDICES`, mutually exclusive with skip/limit). Each batch creates **one `markdown_zip` document per inner Markdown file** (`original_filename` is `{archive.zip}/{nested/path.md}`; Qdrant `filename` matches), shares one **OpenAI** embedding pass for the whole batch for efficiency, then upserts per file. Payloads include `source_type`, `zip_archive_filename`, `zip_inner_path`, `zip_manifest_index`, batch skip/limit, etc. The ingest response includes `indexed_document_ids` and aggregate `chunk_count` / `vector_count` across those files. When the manifest is fully consumed in skip/limit mode, the session **closes** and the stored ZIP is removed. Requires `OPENAI_API_KEY`. Hard limits: `ZIP_INGEST_MAX_UNCOMPRESSED_BYTES`, `ZIP_INGEST_MAX_ENTRY_BYTES`, `ZIP_INGEST_MAX_MARKDOWN_LISTED`. Paths under `node_modules`, `.git`, `venv`, `.venv`, `__pycache__`, `dist`, `build`, `.next`, `target`, `.turbo` are ignored.

**One-command ZIP ingest (CLI):** from the repo root, with the API running and a user account:

```bash
python scripts/ingest_markdown_zip.py path/to/archive.zip --email you@example.com --password 'your-password'
```

First-time account: add `--register`. Or set `ACCESS_TOKEN` (and optionally `API_BASE_URL`) instead of email/password. The script creates the zip session and loops `ingest` with `{}` until all Markdown files are indexed, then prints the final JSON response.

Supported single-file types:

- `.pdf`
- `.md`
- `.markdown`

Chunking for **single uploads**, **ZIP inner files**, and **website crawl text** goes through `resolve_chunker_for_ingestion` (same `DOCUMENT_CHUNK_STRATEGY` switch): with **`auto`**, **(1)** FAQ-like (≥ two Q/A blocks) → pair chunks; **(2)** else if Markdown `#` headings → heading chunks then paragraphs/windows; **(3)** else paragraphs then windows. With `section_aware` / `markdown_headers` / `recursive`, ZIP inner Markdown uses the same legacy `get_chunker` rules as single-file Markdown.

## Chat API

Authenticated routes under `/api/v1/chat`:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/sessions` | Create a session (`title` optional) |
| GET | `/sessions` | List sessions |
| GET | `/sessions/{session_id}` | Session detail including messages |
| POST | `/sessions/{session_id}/messages` | Send `{"content": "..."}` — persists the user message, calls the configured LLM with recent history, persists the assistant reply, returns **both** messages (each includes `id`, `role`, `created_at`) |
| POST | `/sessions/{session_id}/messages/stream` | Same JSON body as above; **SSE** (`text/event-stream`). Events: `loading`, repeated `delta` (`text`), then `done` (`message` = full assistant text), or `error` (`detail`). User and assistant rows are persisted like the non-streaming path; see `openapi.json` and `docs/API_FOR_FRONTEND_AGENT.md`. |

Configure OpenAI or any OpenAI-compatible API via `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `LLM_MODEL` (see `.env.example`).

## Website Scheduling

Website sources support these crawl frequencies:

- `never`
- `only_once`
- `12h`
- `1d`

The scheduler runs inside the FastAPI process when `SCHEDULER_ENABLED=true`. Every `SCHEDULER_TICK_SECONDS`, it scans for due website sources and runs crawls. The current crawler fetches the configured URL itself, extracts readable text from HTML, chunks it, embeds it, writes vectors to Qdrant, and records crawl job/page metadata in MongoDB.

Create a website source:

```bash
curl -X POST http://localhost:8000/api/v1/websites \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","name":"Example","frequency":"1d"}'
```

Manually trigger a crawl:

```bash
curl -X POST http://localhost:8000/api/v1/websites/WEBSITE_ID/crawl \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

## Future Work

- Replace the local deterministic embedding provider with a semantic model provider.
- Add website URL configuration and crawl job tracking.
- Add ingestion pipelines for parsing, cleaning, chunking, and indexing.
- Add embedding provider integration.
- Add Qdrant collection management, upsert, search, and filters.
- Add knowledge-base retrieval and answer generation from indexed sources only.
