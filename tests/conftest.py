import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# CSRF protection is on by default in production but disabled in unit tests
# so existing form-POST tests don't need to pre-fetch a token. The single
# `test_csrf_protection_blocks_unauthenticated_post` test re-enables it
# explicitly to verify the protection actually works.
from mtgcompare.web import app as _app  # noqa: E402

_app.config["WTF_CSRF_ENABLED"] = False
