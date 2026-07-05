#!/usr/bin/env python3
"""
import_data.py — loads the Cyberpolygon "FerrumFox" incident scenario
(telemetry/data/*.json, elasticdump-format NDJSON exports) into a local
Elasticsearch, shifting every embedded timestamp by a constant offset so
the scenario appears to have just happened (its last event lands "now").

Why a custom loader instead of elasticdump:
  - avoids an extra Docker container / docker-network dependency
  - lets us rewrite dates while streaming, with no 665MB intermediate file
  - ES9's bulk API is what elasticdump uses under the hood anyway

Usage:
  python3 import_data.py                      # shift + load everything
  python3 import_data.py --dry-run             # validate only, no writes
  python3 import_data.py --limit 500           # quick smoke test
  python3 import_data.py --recreate            # delete indices first
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("This script needs the 'requests' package: pip3 install requests", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Date shifting
#
# The whole scenario spans a single real day (2024-04-17, ~04:00-13:30 UTC).
# We compute ONE delta = (now - SOURCE_MAX_TS) and add it to every timestamp
# we can find, wherever it lives: the top-level @timestamp field, sibling
# fields like `timestamp`/`received_at`/`ProcessCreationTime`/`fileTime`, and
# dates embedded as text inside raw fields such as `event.original`
# (a syslog-wrapped, further-JSON-embedded Suricata line). Because the delta
# is a whole number of seconds, fractional-second digits and UTC-offset
# notation are left untouched and simply carried over -- only the
# YYYY-MM-DDTHH:MM:SS part is recomputed. This preserves every relative gap
# in the original timeline (including e.g. a file's original creation date
# that predates the exercise), which matters for incident analysis.
# ---------------------------------------------------------------------------

SOURCE_MAX_TS = datetime(2024, 4, 17, 13, 29, 47)  # true max @timestamp, FerrumFox dataset (default)
SOURCE_YEAR_FOR_SYSLOG = 2024  # only used as a fallback anchor year for bare syslog-style dates (no year in string)

ISO_RE = re.compile(
    r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+|\.)?(Z|[+-]\d{2}:?\d{2})?'
)
# Some source records (e.g. the Supply Chain Attack scenario's `event_time`
# field) use a space instead of "T" between date and time, SQL-style, with no
# zone marker: "2021-06-24 13:42:42". Elasticsearch's dynamic date mapping is
# locked in from whichever variant of a field is seen first, so once a field
# is mapped from a "T" record, later space-separated values in the *same*
# field get rejected outright (document_parsing_exception) unless we
# normalize them to the same "T" form here.
SPACE_ISO_RE = re.compile(
    r'(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(\.\d+)?'
)
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
MONTH_IDX = {m: i + 1 for i, m in enumerate(MONTHS)}
SYSLOG_RE = re.compile(r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\b')


def _shift_iso(m, delta):
    y, mo, d, h, mi, s, frac, off = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)) + delta
    # A lone "." with no digits after it is a (rare) truncated-fraction artifact
    # already present in some source dumps -- drop it rather than reproduce it.
    frac = frac if (frac and frac != '.') else ''
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}{frac}{off or ''}"


def _shift_space_iso(m, delta):
    y, mo, d, h, mi, s, frac = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)) + delta
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}{frac or ''}"


def _shift_syslog(m, delta):
    mon, day, h, mi, s = m.groups()
    dt = datetime(SOURCE_YEAR_FOR_SYSLOG, MONTH_IDX[mon], int(day), int(h), int(mi), int(s)) + delta
    return f"{MONTHS[dt.month - 1]} {dt.day:2d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"


def shift_text(s, delta):
    if len(s) < 8 or (s.isdigit() or s.isalpha()):
        return s
    s = ISO_RE.sub(lambda m: _shift_iso(m, delta), s)
    s = SPACE_ISO_RE.sub(lambda m: _shift_space_iso(m, delta), s)
    s = SYSLOG_RE.sub(lambda m: _shift_syslog(m, delta), s)
    return s


def walk(obj, delta):
    if isinstance(obj, dict):
        for k in obj:
            obj[k] = walk(obj[k], delta)
        return obj
    elif isinstance(obj, list):
        return [walk(v, delta) for v in obj]
    elif isinstance(obj, str):
        return shift_text(obj, delta)
    return obj


# ---------------------------------------------------------------------------
# Bulk loading
# ---------------------------------------------------------------------------

def iter_records(path, limit=None, skip=0):
    with open(path, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f):
            if i < skip:
                continue
            line = line.strip()
            if not line:
                continue
            if limit is not None and (i - skip) >= limit:
                break
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  ! skipping malformed line {i} in {os.path.basename(path)}: {e}", file=sys.stderr)


def ensure_index(es_url, session, index, total_fields_limit):
    '''
    Make sure `index` allows at least `total_fields_limit` mapped fields.
    Wide security-telemetry schemas (many event types, each with its own
    field set, plus ES's default text->text+keyword doubling) blow past the
    default cap of 1000 easily and ES then silently rejects (bulk error,
    not a hard failure) any document that would add a field beyond it.
    Tries to create the index with the raised limit; if it already exists,
    updates the setting on it directly instead (this is a dynamic setting,
    safe to change on a live index).
    '''
    base = es_url.rstrip('/')
    resp = session.put(
        f"{base}/{index}",
        json={"settings": {"index.mapping.total_fields.limit": total_fields_limit}},
        timeout=30,
    )
    if resp.status_code == 200:
        return
    resp2 = session.put(
        f"{base}/{index}/_settings",
        json={"index.mapping.total_fields.limit": total_fields_limit},
        timeout=30,
    )
    if resp2.status_code != 200:
        print(f"  ! could not ensure total_fields.limit={total_fields_limit} on index '{index}': "
              f"create attempt -> {resp.status_code} {resp.text[:200]}; "
              f"update attempt -> {resp2.status_code} {resp2.text[:200]}", file=sys.stderr)


def bulk_flush(es_url, session, buf, index_counts):
    if not buf:
        return
    payload = "\n".join(buf) + "\n"
    resp = session.post(
        es_url.rstrip('/') + "/_bulk",
        data=payload.encode('utf-8'),
        headers={"Content-Type": "application/x-ndjson"},
        timeout=120,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("errors"):
        shown = 0
        for item in result["items"]:
            action = item.get("index", {})
            if action.get("error"):
                print(f"  ! bulk error on _id={action.get('_id')}: {action['error']}", file=sys.stderr)
                shown += 1
                if shown >= 5:
                    print("  ! (further errors suppressed)", file=sys.stderr)
                    break


def process_file(path, es_url, session, delta, batch_size, dry_run, index_counts, ts_range, limit=None, skip=0):
    index_name = None
    buf = []
    buf_docs = 0
    t0 = time.time()
    n = 0

    for rec in iter_records(path, limit=limit, skip=skip):
        index_name = rec.get("_index", index_name)
        doc_id = rec.get("_id")
        src = walk(rec.get("_source", {}), delta)

        ts = src.get("@timestamp")
        if ts:
            if ts_range[0] is None or ts < ts_range[0]:
                ts_range[0] = ts
            if ts_range[1] is None or ts > ts_range[1]:
                ts_range[1] = ts

        if not dry_run:
            action = {"index": {"_index": index_name}}
            if doc_id:
                action["index"]["_id"] = doc_id
            buf.append(json.dumps(action))
            buf.append(json.dumps(src))
            buf_docs += 1
            if buf_docs >= batch_size:
                bulk_flush(es_url, session, buf, index_counts)
                buf, buf_docs = [], 0

        n += 1
        if n % 50000 == 0:
            print(f"  ... {os.path.basename(path)}: {n} docs processed ({time.time()-t0:.1f}s)")

    if not dry_run:
        bulk_flush(es_url, session, buf, index_counts)

    index_counts[index_name] = index_counts.get(index_name, 0) + n
    print(f"  {os.path.basename(path)} -> index '{index_name}': {n} docs in {time.time()-t0:.1f}s")


def main():
    parser = argparse.ArgumentParser(description="Shift dates and load the FerrumFox scenario into Elasticsearch")
    parser.add_argument("--es-url", default=os.environ.get("ES_URL", "http://localhost:9200"))
    parser.add_argument("--es-api-key", default=os.environ.get("ES_API_KEY"),
                         help="Encoded API key (id:api_key, base64) - sent as 'Authorization: ApiKey ...'")
    parser.add_argument("--es-user", default=os.environ.get("ES_USER", "elastic"))
    parser.add_argument("--es-password", default=os.environ.get("ES_PASSWORD") or os.environ.get("ELASTIC_PASSWORD"))
    parser.add_argument("--data-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--dry-run", action="store_true", help="Parse & shift only, no writes to Elasticsearch")
    parser.add_argument("--recreate", action="store_true", help="Delete target indices before loading")
    parser.add_argument("--limit", type=int, default=None, help="Only process N lines per file (testing)")
    parser.add_argument("--skip", type=int, default=0, help="Skip the first N lines per file (testing / chunked resume)")
    parser.add_argument("--only", default=None, help="Only process files whose name contains this substring (testing)")
    parser.add_argument("--total-fields-limit", type=int, default=4000,
                         help="index.mapping.total_fields.limit to set on target indices before loading "
                              "(default: 4000; ES's own default is 1000, which wide security-telemetry "
                              "schemas -- many event types x text/keyword doubling -- can exceed)")
    parser.add_argument("--anchor", default=None, help="ISO datetime the scenario should end at (default: now, UTC)")
    parser.add_argument("--source-max-ts", default=None,
                         help="ISO datetime of the true max @timestamp in the source dataset being loaded "
                              "(default: the built-in FerrumFox constant, 2024-04-17T13:29:47). Required "
                              "for any other dataset so the shift delta lands the scenario's last event at "
                              "--anchor exactly, not off by however stale the built-in constant is for it.")
    args = parser.parse_args()

    if args.anchor:
        anchor = datetime.fromisoformat(args.anchor.replace("Z", "+00:00")).replace(tzinfo=None)
    else:
        anchor = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)

    if args.source_max_ts:
        source_max_ts = datetime.fromisoformat(args.source_max_ts.replace("Z", "+00:00")).replace(tzinfo=None)
    else:
        source_max_ts = SOURCE_MAX_TS

    global SOURCE_YEAR_FOR_SYSLOG
    SOURCE_YEAR_FOR_SYSLOG = source_max_ts.year

    delta = anchor - source_max_ts

    print(f"Source max @timestamp: {source_max_ts.isoformat()}")
    print(f"Anchor (scenario end time): {anchor.isoformat()}Z")
    print(f"Shift delta: {delta}")

    files = sorted(glob.glob(os.path.join(args.data_dir, "*.json")))
    if args.only:
        files = [f for f in files if args.only in os.path.basename(f)]
    if not files:
        print(f"No .json files found in {args.data_dir}", file=sys.stderr)
        sys.exit(1)
    print("Files to load:", ", ".join(os.path.basename(f) for f in files))

    session = requests.Session()
    if args.es_api_key:
        session.headers["Authorization"] = f"ApiKey {args.es_api_key}"
    elif args.es_password:
        session.auth = (args.es_user, args.es_password)

    if not args.dry_run:
        try:
            r = session.get(args.es_url, timeout=10)
            r.raise_for_status()
        except Exception as e:
            print(f"Could not reach Elasticsearch at {args.es_url}: {e}", file=sys.stderr)
            print("Is 'docker compose up -d' running?", file=sys.stderr)
            sys.exit(1)

        if args.recreate:
            for f in files:
                first = next(iter_records(f, limit=1), None)
                if first and first.get("_index"):
                    idx = first["_index"]
                    dr = session.delete(f"{args.es_url.rstrip('/')}/{idx}", timeout=30)
                    print(f"  recreate: deleted index '{idx}' -> {dr.status_code}")

    if not args.dry_run:
        for f in files:
            first = next(iter_records(f, limit=1), None)
            if first and first.get("_index"):
                ensure_index(args.es_url, session, first["_index"], args.total_fields_limit)

    index_counts = {}
    ts_range = [None, None]
    for f in files:
        process_file(f, args.es_url, session, delta, args.batch_size, args.dry_run, index_counts, ts_range,
                      limit=args.limit, skip=args.skip)

    if not args.dry_run:
        for idx in index_counts:
            session.post(f"{args.es_url.rstrip('/')}/{idx}/_refresh", timeout=30)

    print()
    print("=== Summary ===")
    for idx, count in index_counts.items():
        print(f"  {idx}: {count} docs")
    print(f"  new @timestamp range: {ts_range[0]}  ->  {ts_range[1]}")
    if args.dry_run:
        print("  (dry run - nothing was written to Elasticsearch)")


if __name__ == "__main__":
    main()
