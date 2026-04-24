"""DuckDB-accelerated MTGJSON AllPrices history import pipeline.

  1. Stream: AllPrices.json.xz → NDJSON (lightweight Python; one compact JSON object per UUID)
  2. Flatten: DuckDB reads NDJSON and unnests date→price maps into rows with a PRIMARY KEY
  3. Atomic rename of the .tmp DuckDB file into place (full rebuild only)

Public API: rebuild_history_db(), merge_today_prices()
"""
import json
import lzma
from pathlib import Path
from typing import Callable

import duckdb

_DUCKDB_SCHEMA = """
CREATE TABLE price_rows (
    uuid           VARCHAR NOT NULL,
    finish         VARCHAR NOT NULL,
    market_date    VARCHAR NOT NULL,
    price_usd      DOUBLE,
    source_updated VARCHAR NOT NULL,
    PRIMARY KEY (uuid, finish, market_date)
)
"""


# ---------------------------------------------------------------------------
# Step 1: stream AllPrices.json.xz → NDJSON
# ---------------------------------------------------------------------------

class _StreamReader:
    """Minimal buffered reader for streaming large JSON files."""

    def __init__(self, fh, chunk_size: int = 65536):
        self.fh = fh
        self._cs = chunk_size
        self._buf = ""
        self._pos = 0
        self._eof = False

    def _compact(self):
        if self._pos > 0:
            self._buf = self._buf[self._pos:]
            self._pos = 0

    def _fill(self, n: int = 1):
        while len(self._buf) - self._pos < n and not self._eof:
            if self._pos > self._cs:
                self._compact()
            chunk = self.fh.read(self._cs)
            if not chunk:
                self._eof = True
                return
            if self._pos:
                self._compact()
            self._buf += chunk

    def consume_until(self, marker: str) -> bool:
        while True:
            idx = self._buf.find(marker, self._pos)
            if idx != -1:
                self._pos = idx + len(marker)
                return True
            if self._eof:
                return False
            tail = self._buf[max(self._pos, len(self._buf) - len(marker) + 1):]
            self._buf = tail
            self._pos = 0
            chunk = self.fh.read(self._cs)
            if not chunk:
                self._eof = True
            else:
                self._buf += chunk

    def get(self) -> str:
        self._fill(1)
        if self._pos >= len(self._buf):
            return ""
        ch = self._buf[self._pos]
        self._pos += 1
        return ch

    def peek(self) -> str:
        self._fill(1)
        if self._pos >= len(self._buf):
            return ""
        return self._buf[self._pos]

    def skip_ws(self):
        while True:
            ch = self.peek()
            if ch and ch.isspace():
                self._pos += 1
            else:
                return

    def expect(self, expected: str):
        actual = self.get()
        if actual != expected:
            raise ValueError(f"Expected {expected!r}, got {actual!r}")

    def read_string(self) -> str:
        chars: list[str] = []
        escaped = False
        while True:
            ch = self.get()
            if not ch:
                raise ValueError("Unexpected EOF in JSON string")
            if escaped:
                chars.append(ch)
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                return "".join(chars)
            else:
                chars.append(ch)

    def read_object(self) -> str:
        self.skip_ws()
        start = self.get()
        if start != "{":
            raise ValueError(f"Expected '{{', got {start!r}")
        chars = ["{"]
        depth = 1
        in_string = False
        escaped = False
        while depth > 0:
            ch = self.get()
            if not ch:
                raise ValueError("Unexpected EOF in JSON object")
            chars.append(ch)
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        return "".join(chars)


def _iter_price_entries(xz_path: Path):
    """Yield (uuid, retail_dict) for each UUID in AllPrices.json.xz."""
    with lzma.open(xz_path, "rt", encoding="utf-8") as fh:
        reader = _StreamReader(fh)
        if not reader.consume_until('"data"'):
            return
        reader.skip_ws()
        reader.expect(":")
        reader.skip_ws()
        reader.expect("{")
        while True:
            reader.skip_ws()
            ch = reader.peek()
            if ch == "}":
                reader.get()
                break
            if reader.get() != '"':
                raise ValueError(f"Unexpected token in MTGJSON data: {ch!r}")
            uuid = reader.read_string()
            reader.skip_ws()
            reader.expect(":")
            payload_text = reader.read_object()
            payload = json.loads(payload_text)
            retail = (
                ((payload.get("paper") or {}).get("tcgplayer") or {}).get("retail")
            ) or {}
            yield uuid, retail
            reader.skip_ws()
            tok = reader.get()
            if tok == "}":
                break
            if tok != ",":
                raise ValueError(f"Unexpected delimiter: {tok!r}")


def _stream_to_ndjson(
    xz_path: Path,
    ndjson_path: Path,
    *,
    progress_cb: Callable[[int, str, str], None] | None = None,
) -> int:
    """Write per-UUID retail maps as NDJSON. Returns UUID count."""
    count = 0
    with ndjson_path.open("w", encoding="utf-8") as out:
        for uuid, retail in _iter_price_entries(xz_path):
            row = {
                "uuid":   uuid,
                "normal": retail.get("normal") or {},
                "foil":   retail.get("foil")   or {},
                "etched": retail.get("etched") or {},
            }
            out.write(json.dumps(row, separators=(",", ":")) + "\n")
            count += 1
            if progress_cb and count % 10000 == 0:
                progress_cb(
                    40 + min(12, count // 5000),
                    "Decompressing history",
                    f"Streamed {count:,} cards to NDJSON...",
                )
    return count


# ---------------------------------------------------------------------------
# Step 2: DuckDB load NDJSON → price_rows
# ---------------------------------------------------------------------------

def _build_load_sql(ndjson_str: str, source_updated: str, *, upsert: bool) -> str:
    """Return DuckDB SQL that loads price_rows from the given NDJSON file.

    upsert=False: plain INSERT INTO (table must be empty; no conflict check).
    upsert=True:  INSERT OR REPLACE INTO (upserts into existing table).
    """
    verb = "INSERT OR REPLACE INTO" if upsert else "INSERT INTO"
    return f"""
{verb} price_rows
WITH src AS (
    SELECT
        uuid,
        json_transform("normal",  '"MAP(VARCHAR, DOUBLE)"') AS normal_map,
        json_transform("foil",    '"MAP(VARCHAR, DOUBLE)"') AS foil_map,
        json_transform("etched",  '"MAP(VARCHAR, DOUBLE)"') AS etched_map
    FROM read_ndjson('{ndjson_str}',
                     columns = {{uuid: 'VARCHAR', normal: 'JSON', foil: 'JSON', etched: 'JSON'}})
)
SELECT uuid, 'normal' AS finish,
       unnest(map_keys(normal_map)) AS market_date,
       unnest(map_values(normal_map)) AS price_usd,
       '{source_updated}' AS source_updated
FROM src WHERE normal_map IS NOT NULL AND cardinality(normal_map) > 0
UNION ALL
SELECT uuid, 'foil',
       unnest(map_keys(foil_map)),
       unnest(map_values(foil_map)),
       '{source_updated}'
FROM src WHERE foil_map IS NOT NULL AND cardinality(foil_map) > 0
UNION ALL
SELECT uuid, 'etched',
       unnest(map_keys(etched_map)),
       unnest(map_values(etched_map)),
       '{source_updated}'
FROM src WHERE etched_map IS NOT NULL AND cardinality(etched_map) > 0
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rebuild_history_db(
    xz_path: Path,
    source_updated: str,
    duckdb_path: Path,
    *,
    progress_cb: Callable[[int, str, str], None] | None = None,
) -> int:
    """Full rebuild: AllPrices.json.xz → NDJSON → DuckDB.

    Builds to a .tmp file and renames atomically on success.
    Returns the number of price rows written to DuckDB.
    """
    cache_dir = xz_path.parent
    ndjson_path = cache_dir / "AllPrices.ndjson"
    duckdb_tmp = Path(str(duckdb_path) + ".tmp")

    for f in (duckdb_tmp, ndjson_path):
        if f.exists():
            f.unlink()

    if progress_cb:
        progress_cb(40, "Decompressing history", "Streaming AllPrices.json.xz to NDJSON...")

    uuid_count = _stream_to_ndjson(xz_path, ndjson_path, progress_cb=progress_cb)

    if progress_cb:
        progress_cb(52, "Building DuckDB price DB", f"Streamed {uuid_count:,} cards. Loading into DuckDB...")

    ndjson_str = str(ndjson_path).replace("\\", "/")
    conn = duckdb.connect(str(duckdb_tmp))
    try:
        conn.execute(_DUCKDB_SCHEMA)
        conn.execute(_build_load_sql(ndjson_str, source_updated, upsert=False))
        row_count: int = conn.execute("SELECT COUNT(*) FROM price_rows").fetchone()[0]
    finally:
        conn.close()

    ndjson_path.unlink(missing_ok=True)

    if row_count == 0:
        duckdb_tmp.unlink(missing_ok=True)
        raise RuntimeError("DuckDB flatten produced 0 rows — aborting rebuild.")

    duckdb_tmp.replace(duckdb_path)

    if progress_cb:
        progress_cb(92, "Finishing import", f"Local MTGJSON history DB ready with {row_count:,} price points.")

    return row_count


def merge_today_prices(
    xz_path: Path,
    duckdb_path: Path,
    source_updated: str,
    *,
    progress_cb: Callable[[int, str, str], None] | None = None,
) -> int:
    """Upsert today's prices from AllPricesToday.json.xz into an existing DuckDB.

    Uses the same NDJSON → DuckDB SQL path as rebuild_history_db for speed.
    Gaps in existing history are left intact. Returns the number of rows upserted.
    """
    cache_dir = xz_path.parent
    ndjson_path = cache_dir / "AllPricesToday.ndjson"

    if progress_cb:
        progress_cb(40, "Merging today's prices", "Streaming AllPricesToday.json.xz to NDJSON...")

    uuid_count = _stream_to_ndjson(xz_path, ndjson_path, progress_cb=progress_cb)

    if progress_cb:
        progress_cb(58, "Merging today's prices", f"Streamed {uuid_count:,} cards. Upserting into DuckDB...")

    ndjson_str = str(ndjson_path).replace("\\", "/")
    conn = duckdb.connect(str(duckdb_path))
    try:
        conn.execute(_build_load_sql(ndjson_str, source_updated, upsert=True))
        row_count: int = conn.execute(
            "SELECT COUNT(*) FROM price_rows WHERE source_updated = ?", [source_updated]
        ).fetchone()[0]
    finally:
        conn.close()

    ndjson_path.unlink(missing_ok=True)

    if progress_cb:
        progress_cb(95, "Merging today's prices", f"Merged {row_count:,} today's price points.")
    return row_count
