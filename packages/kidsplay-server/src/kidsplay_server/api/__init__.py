"""FastAPI API routes for the KidsPlay server.

Submodules:
    app     — application factory (``create_app``).
    deps    — shared FastAPI dependency functions.
    media   — media CRUD, ingest, and assignment endpoints.
    devices — profile and device CRUD endpoints.
    sync    — device-facing manifest and file-download endpoints.
    queue   — background import queue endpoints.
    importers — installed importer listing and URL matching.
"""
