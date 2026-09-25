"""Local development entry point: `python run.py` then open the printed URL.

On Railway the app is started with uvicorn directly (see Dockerfile/Procfile),
this file only exists to make local runs one command long.
"""

from __future__ import annotations

import os

import uvicorn

from app import config

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"\n  Panel  ->  http://{host}:{config.PORT}{config.ADMIN_PATH}")
    print(f"  API doc->  http://{host}:{config.PORT}/docs\n")
    uvicorn.run(
        "app.main:app",
        host=host,
        port=config.PORT,
        reload=os.environ.get("RELOAD", "1") == "1",
    )
