"""Container healthcheck: exit 0 if the API answers /healthz on this machine."""

import os
import sys
import urllib.request

PORT = os.environ.get("PORT", "8000")

try:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/v1/healthz", timeout=3) as response:
        sys.exit(0 if response.status == 200 else 1)
except OSError:
    sys.exit(1)
