"""Uvicorn entry: ``python -m review_hub.server``.

Local-first bind contract: 127.0.0.1 unless REVIEW_HUB_HOST says otherwise.
There is no authentication by default - see review_hub/server/auth.py: set
REVIEW_HUB_ACCESS_TOKEN before ever binding beyond loopback.
"""

from __future__ import annotations

import os

import uvicorn

from review_hub.server.app import create_app


def main() -> None:
    app = create_app()
    host = os.environ.get("REVIEW_HUB_HOST", "127.0.0.1")
    port = int(os.environ.get("REVIEW_HUB_PORT", "8734"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
