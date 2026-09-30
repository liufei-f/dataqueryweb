"""Keep every test away from the real records database.

``dataqueryweb.web`` builds its app at import time (``app = create_app()``), and that
opens ``RecordStore()`` at the default path — the curator's own ``data/records.sqlite3``.
Importing it from a test would open, and migrate, real saved records. This runs before any
test module is imported and points the default at a throwaway file.
"""

import os
import tempfile
from pathlib import Path

os.environ["DATAQUERY_DB"] = str(Path(tempfile.mkdtemp(prefix="dataquery-test-")) / "records.sqlite3")
