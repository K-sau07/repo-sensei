"""Container health check for a worker: the worker on this host answers a ping via the broker."""

from __future__ import annotations

import socket
import sys

from sensei_workers.app import celery_app


def main() -> int:
    # Workers are named worker@<hostname> (see worker.Dockerfile); %h may add a domain.
    name = f"worker@{socket.gethostname()}"
    replies = celery_app.control.ping(timeout=5.0) or []
    names = {node for reply in replies for node in reply}
    return 0 if any(node == name or node.startswith(f"{name}.") for node in names) else 1


if __name__ == "__main__":
    sys.exit(main())
