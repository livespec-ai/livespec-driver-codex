"""Put `dev-tooling/` on sys.path before any module in this directory is collected.

The modules under test are loaded by putting `dev-tooling/` on sys.path, as
their runtime does. That insert used to happen only as a side effect of
importing `test_codex_hook_cache_fixtures`, so a test module that imports a
`dev-tooling/` module above that import could be collected only after some
other module had already run the insert. Run alone, as the Red-Green-Replay
hook's single-file Red leg runs it, such a module failed collection with
`ModuleNotFoundError`, and the hook recorded that collection error as a Red
(livespec-driver-codex run 01M819KQBSEQ). pytest imports this conftest before
it collects any module in this directory, so every module here collects alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

DEV_TOOLING = Path(__file__).resolve().parents[2] / "dev-tooling"
if str(DEV_TOOLING) not in sys.path:
    sys.path.insert(0, str(DEV_TOOLING))
