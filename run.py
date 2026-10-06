import socket
import sys

import uvicorn

from backend.config import HOST, PORT


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


if __name__ == "__main__":
    if _port_in_use():
        sys.exit(
            f"Port {PORT} is already in use - another server instance is probably "
            f"running. Stop it first (e.g. `pkill -f run.py`)."
        )
    uvicorn.run("backend.app:app", host=HOST, port=PORT, reload=False)
