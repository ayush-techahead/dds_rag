# Documents Module

Handles PDF upload metadata, local demo file storage, text extraction, chunking, embedding, and vector indexing into Qdrant.

Current implementation supports PDF files only. Original files are stored under `STORAGE_DIR`; document metadata and indexing status live in MongoDB. Vector chunks are stored in Qdrant.
