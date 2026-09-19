import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The suite configures itself; a developer's .env must not reach it. Its keys
# would switch on live extraction here — tests/test_env.py covers the loading
# itself, with files it writes.
os.environ.setdefault("ENV_FILE", "")

# Tests must not depend on the developer environment having a GCP project.
os.environ.setdefault("ELECTION_STORE", "memory")
# The suite drives gating explicitly where it means to (tests/test_access.py
# overrides the verifier); everywhere else the app is exercised ungated.
os.environ.setdefault("AUTH_MODE", "off")
