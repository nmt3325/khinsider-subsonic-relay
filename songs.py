"""Song-title search index for the khinsider Subsonic relay.

library.json only describes albums, so search2/search3 could never return
songs.  This module consumes a separate track index published by
https://github.com/nmt3325/khinsider-index (songs.tsv.gz, ~33 MB,
~3.25M rows: album<TAB>disc<TAB>track<TAB>title), builds a local SQLite
FTS5 database from it, and answers song-title queries from memory.

The index is only used to find *candidates*.  Every hit is resolved against
the live album page through server.load_album() (30-day disk cache) so the
returned ids (track/<slug>/<idx>) are always the ones getSong and stream
expect.  If the index is missing, stale or broken, song search degrades to
an empty list and the rest of the relay is unaffected.

Upstream republishes the index weekly, so a background thread re-checks it
every SONGS_REFRESH_HOURS.  The check is manifest-first when possible and
otherwise falls back to a conditional gzip fetch.  A new index is built into
a temporary file and swapped in atomically; queries keep hitting the old
database until it is ready.
"""

import gzip
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request

DEFAULT_SONGS_URL = (
    'https://github.com/nmt3325/khinsider-index/releases/download/'
    'song-index/songs.tsv.gz')
DEFAULT_SONGS_MANIFEST_URL = (
    'https://github.com/nmt3325/khinsider-index/releases/download/'
    'song-index/songs-index.json')
SONGS_URL = os.environ.get('SONGS_URL') or DEFAULT_SONGS_URL
SONGS_MANIFEST_URL = os.environ.get('SONGS_MANIFEST_URL')
if SONGS_MANIFEST_URL is None:
    SONGS_MANIFEST_URL = (
        DEFAULT_SONGS_MANIFEST_URL if SONGS_URL == DEFAULT_SONGS_URL else '')
SONGS_DB = os.environ.get('SONGS_DB') or './songs.sqlite'
SONGS_MAX_AGE_DAYS = float(os.environ.get('SONGS_MAX_AGE_DAYS') or 0)
SONGS_REFRESH_HOURS = float(os.environ.get('SONGS_REFRESH_HOURS') or 24)
SONG_SEARCH = (os.environ.get('SONG_SEARCH') or 'auto').strip().lower()
ALBUM_LIMIT = int(os.environ.get('SONG_SEARCH_ALBUM_LIMIT') or 12)
CANDIDATES = int(os.environ.get('SONG_SEARCH_CANDIDATES') or 600)

SCHEMA = 1
TSV_SCHEMA = 1
MIN_QUERY = 3  # the trigram tokenizer cannot match shorter needles
USER_AGENT = 'khinsider-subsonic-relay'
_HASH_RE = re.compile(r'^[0-9a-f]{64}$')

_lock = threading.Lock()        # sqlite connections are not thread-safe
_build_lock = threading.Lock()  # at most one download/build at a time
_conn = None
_retired = []                   # superseded connections, closed one cycle later
_state = {'state': 'disabled' if SONG_SEARCH == 'off' else 'idle',
          'rows': 0, 'built': None, 'checked': None, 'error': None}

_NUM_ONLY = re.compile(r'^0*(\d{1,4})$')
_TRACK_N = re.compile(r'^track\s*0*(\d{1,4})$', re.I)
_DROP = re.compile(r'[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+')


def key(text):
    """Comparison key. '001' and 'Track 1' collapse to the same value."""
    s = (text or '').strip()
    m = _NUM_ONLY.match(s) or _TRACK_N.match(s)
    if m:
        return 'track%d' % int(m.group(1))
    return _DROP.sub('', s.lower())


def status():
    d = dict(_state)
    d['url'] = SONGS_URL
    d['db'] = SONGS_DB
    d['refreshHours'] = SONGS_REFRESH_HOURS
    for k in ('built', 'checked'):
        if d.get(k):
            d[k] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(d[k]))
    return d


def _note(exc):
    _state['error'] = '%s: %s' % (type(exc).__name__, exc)
    if _conn is None:
        _state['state'] = 'error'
    print('song index: %s' % _state['error'])


def _digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _hash_hex(value):
    s = str(value or '').strip().lower()
    return s if _HASH_RE.match(s) else ''


def _meta_path(db_path):
    return 'file:%s?mode=ro' % db_path


def _open_db(db_path, write=False):
    uri = False
    path = db_path
    if not write:
        uri = True
        path = _meta_path(db_path)
    return sqlite3.connect(path, uri=uri, check_same_thread=False)


def _read_meta(db_path):
    """Meta of the database currently on disk."""
    if not os.path.exists(db_path):
        return {}
    try:
        con = _open_db(db_path)
        try:
            return dict(con.execute('SELECT k, v FROM meta').fetchall())
        finally:
            con.close()
    except Exception:
        return {}


def _meta_matches_runtime(meta):
    return (int(meta.get('schema') or 0) == SCHEMA and
            int(meta.get('tsv_schema') or 0) == TSV_SCHEMA and
            meta.get('source') == SONGS_URL)


def _trusted_meta(meta):
    return _meta_matches_runtime(meta) and bool(_hash_hex(meta.get('content_digest')))


def _needs_check(meta):
    built = int(meta.get('built') or 0)
    if SONGS_MAX_AGE_DAYS <= 0 or not built:
        return False
    return time.time() - built > SONGS_MAX_AGE_DAYS * 86400


def _apply_state(meta):
    if meta.get('rows'):
        _state['rows'] = int(meta.get('rows') or 0)
    if meta.get('built'):
        _state['built'] = int(meta.get('built') or 0)
    if meta.get('checked'):
        _state['checked'] = int(meta.get('checked') or 0)


def _write_meta(db_path, updates):
    if not os.path.exists(db_path):
        return False
    try:
        con = _open_db(db_path, write=True)
        try:
            con.execute('CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)')
            rows = [(k, '' if v is None else str(v)) for k, v in updates.items()]
            con.executemany('INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)', rows)
            con.commit()
        finally:
            con.close()
        return True
    except Exception:
        return False


def _usable(db_path, runtime_bound=False, apply_state=True):
    """Open an existing database if it passes local schema checks."""
    if not os.path.exists(db_path):
        return None
    try:
        con = _open_db(db_path)
        meta = dict(con.execute('SELECT k, v FROM meta').fetchall())
        if int(meta.get('schema', 0)) != SCHEMA:
            con.close()
            return None
        if runtime_bound and not _meta_matches_runtime(meta):
            con.close()
            return None
        if apply_state:
            _apply_state(meta)
        return con
    except Exception:
        return None


def _manifest_request(url):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict):
        raise ValueError('manifest is not an object')
    if int(data.get('schema_version') or 0) != TSV_SCHEMA:
        raise ValueError('unexpected manifest schema')
    manifest = {
        'schema_version': TSV_SCHEMA,
        'sha256': _hash_hex(data.get('sha256')),
        'content_sha256': _hash_hex(data.get('content_sha256')),
    }
    if not manifest['sha256'] and not manifest['content_sha256']:
        raise ValueError('manifest has no usable hashes')
    return manifest


def _load_manifest(url):
    if not url:
        return None
    try:
        return _manifest_request(url)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return None
        print('song index: manifest fetch failed (%s)' % exc)
        return None
    except Exception as exc:
        print('song index: manifest ignored (%s)' % exc)
        return None


def _download(url, dest, known=None):
    """Fetch url into dest. Returns (size, http-meta), or None if unchanged."""
    headers = {'User-Agent': USER_AGENT}
    if known:
        if known.get('etag'):
            headers['If-None-Match'] = known['etag']
        if known.get('last_modified'):
            headers['If-Modified-Since'] = known['last_modified']
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r, open(dest, 'wb') as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
            info = {'etag': r.headers.get('ETag') or '',
                    'last_modified': r.headers.get('Last-Modified') or ''}
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None
        raise
    return os.path.getsize(dest), info


def _rows(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            parts = line.rstrip('\n').split('\t')
            if len(parts) != 4 or not parts[0] or not parts[3]:
                raise ValueError('invalid TSV row %d' % lineno)
            try:
                disc = int(parts[1]) if parts[1] else None
                num = int(parts[2]) if parts[2] else None
            except ValueError:
                raise ValueError('invalid TSV row %d' % lineno)
            yield (parts[0], disc, num, parts[3])


def _inspect_archive(path):
    info = {'digest': _digest(path), 'content_digest': '', 'rows': 0}
    h = hashlib.sha256()
    with gzip.open(path, 'rb') as f:
        for lineno, line in enumerate(f, 1):
            h.update(line)
            parts = line.rstrip(b'\n').split(b'\t')
            if len(parts) != 4 or not parts[0] or not parts[3]:
                raise ValueError('invalid TSV row %d' % lineno)
            try:
                if parts[1]:
                    int(parts[1])
                if parts[2]:
                    int(parts[2])
            except ValueError:
                raise ValueError('invalid TSV row %d' % lineno)
            info['rows'] += 1
    if not info['rows']:
        raise ValueError('empty TSV')
    info['content_digest'] = h.hexdigest()
    return info


def _meta_updates(rows, http, archive, built=None, checked=None, manifest=None):
    return {
        'schema': SCHEMA,
        'tsv_schema': TSV_SCHEMA,
        'source': SONGS_URL,
        'built': int(built or time.time()),
        'checked': int(checked or time.time()),
        'rows': int(rows),
        'etag': http.get('etag') or '',
        'last_modified': http.get('last_modified') or '',
        'digest': archive.get('digest') or '',
        'content_digest': archive.get('content_digest') or '',
        'manifest_url': SONGS_MANIFEST_URL or '',
        'manifest_sha256': (manifest or {}).get('sha256') or '',
        'manifest_content_sha256': (manifest or {}).get('content_sha256') or '',
    }


def _touch_meta(meta, manifest=None):
    if not meta:
        return False
    updates = {'checked': int(time.time())}
    if manifest is not None:
        updates['manifest_url'] = SONGS_MANIFEST_URL or ''
        updates['manifest_sha256'] = manifest.get('sha256') or ''
        updates['manifest_content_sha256'] = manifest.get('content_sha256') or ''
    ok = _write_meta(SONGS_DB, updates)
    if ok:
        fresh = dict(meta)
        fresh.update({k: str(v) for k, v in updates.items()})
        _apply_state(fresh)
    return ok


def _swap_in(db_path):
    global _conn
    con = _usable(db_path)
    if con is None:
        _state['error'] = 'database rejected after build'
        if _conn is None:
            _state['state'] = 'error'
        return False
    with _lock:
        old, _conn = _conn, con
        _state['state'] = 'ready'
        _state['error'] = None
    if old is not None:
        _retired.append(old)
    return True


def _build(tsv_gz, db_path, meta):
    tmp = db_path + '.building'
    for stale in (tmp, tmp + '-journal'):
        if os.path.exists(stale):
            os.remove(stale)
    con = sqlite3.connect(tmp)
    con.execute('PRAGMA journal_mode=OFF')
    con.execute('PRAGMA synchronous=OFF')
    con.execute('CREATE TABLE song(album TEXT NOT NULL, disc INT, n INT, title TEXT NOT NULL)')
    con.executemany('INSERT INTO song(album,disc,n,title) VALUES (?,?,?,?)', _rows(tsv_gz))
    con.execute('CREATE VIRTUAL TABLE fts USING fts5(title, content="song", '
                'content_rowid="rowid", tokenize="trigram")')
    con.execute('INSERT INTO fts(rowid, title) SELECT rowid, title FROM song')
    con.execute('CREATE INDEX song_album ON song(album)')
    con.execute('CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)')
    rows = con.execute('SELECT count(*) FROM song').fetchone()[0]
    if rows <= 0:
        raise ValueError('empty TSV')
    con.executemany('INSERT INTO meta(k,v) VALUES (?,?)',
                    [(k, '' if v is None else str(v)) for k, v in meta.items()])
    con.commit()
    con.close()
    os.replace(tmp, db_path)
    return rows


def _sync(force=False):
    """Shared startup/refresh path. Keeps the last-good DB on every failure."""
    global _conn
    known = _read_meta(SONGS_DB)
    manifest = _load_manifest(SONGS_MANIFEST_URL)
    trusted = _trusted_meta(known)
    checked = int(time.time())
    _state['checked'] = checked
    if manifest and trusted and manifest.get('content_sha256') == known.get('content_digest'):
        if _usable(SONGS_DB) is not None:
            _touch_meta(known, manifest)
            _state['error'] = None
            return False

    tmp = SONGS_DB + '.tsv.gz'
    headers = known if trusted and not force else {}
    try:
        t0 = time.time()
        got = _download(SONGS_URL, tmp, headers)
        _state['checked'] = int(time.time())
        if got is None:
            _touch_meta(known, manifest)
            _state['error'] = None
            return False
        size, http = got
        archive = _inspect_archive(tmp)
        if manifest and manifest.get('sha256') and manifest['sha256'] != archive['digest']:
            raise ValueError('gzip sha256 mismatch')
        if (manifest and manifest.get('content_sha256') and
                manifest['content_sha256'] != archive['content_digest']):
            raise ValueError('content sha256 mismatch')
        if trusted and archive['content_digest'] == known.get('content_digest'):
            if _usable(SONGS_DB) is not None:
                updates = _meta_updates(int(known.get('rows') or archive['rows']), http,
                                        archive, built=int(known.get('built') or time.time()),
                                        checked=int(time.time()), manifest=manifest)
                _write_meta(SONGS_DB, updates)
                _apply_state(_read_meta(SONGS_DB))
                _state['error'] = None
                return False
        _state['state'] = 'building' if _conn is None else 'refreshing'
        print('song index: downloaded %.1f MB in %.0fs' % (size / 1e6, time.time() - t0))
        rows = _build(tmp, SONGS_DB, _meta_updates(archive['rows'], http, archive,
                                                   checked=int(time.time()),
                                                   manifest=manifest))
        print('song index: built %d rows in %.0fs (%s)' % (
            rows, time.time() - t0, SONGS_DB))
        return _swap_in(SONGS_DB)
    except Exception as exc:
        _note(exc)
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def refresh(force=False):
    """Check upstream for a new index. True when the database was replaced."""
    if SONG_SEARCH == 'off':
        return False
    if not _build_lock.acquire(blocking=False):
        return False                          # a build is already running
    try:
        meta = _read_meta(SONGS_DB)
        return _sync(force=force or _needs_check(meta))
    finally:
        _build_lock.release()


def _close_retired():
    while _retired:
        con = _retired.pop()
        try:
            with _lock:
                con.close()
        except Exception:
            pass


def _ensure():
    """Open, or download+build, the song database. Safe to call repeatedly."""
    global _conn
    if SONG_SEARCH == 'off':
        return None
    with _lock:
        if _conn is not None:
            return _conn
    if not _build_lock.acquire(blocking=False):
        return None    # first build in flight; song search stays empty for now
    try:
        meta = _read_meta(SONGS_DB)
        runtime_bound = _meta_matches_runtime(meta)
        con = _usable(SONGS_DB, runtime_bound=runtime_bound, apply_state=runtime_bound)
        if con is not None and runtime_bound:
            with _lock:
                if _conn is None:
                    _conn = con
                    _state['state'] = 'ready'
                    _state['error'] = None
                else:
                    con.close()
            if not _needs_check(meta):
                return _conn
        elif con is not None:
            con.close()
            con = None
        _sync(force=(con is None) or _needs_check(meta))
    finally:
        _build_lock.release()
    with _lock:
        return _conn


def _refresher():
    interval = max(60.0, SONGS_REFRESH_HOURS * 3600)
    while True:
        time.sleep(interval)
        _close_retired()
        try:
            if refresh():
                print('song index: refreshed from %s' % SONGS_URL)
        except Exception as exc:
            _note(exc)


def start():
    """Kick off the first build and the periodic refresh, without blocking."""
    if SONG_SEARCH == 'off':
        return
    threading.Thread(target=_ensure, name='song-index', daemon=True).start()
    if SONGS_REFRESH_HOURS > 0:
        threading.Thread(target=_refresher, name='song-index-refresh',
                         daemon=True).start()


def _match(query):
    return '"%s"' % query.replace('"', '""')


def candidates(query, limit=None):
    """Ranked (album, disc, n, title) index rows for a query."""
    con = _ensure()
    query = (query or '').strip()
    if con is None or len(query) < MIN_QUERY:
        return []
    try:
        with _lock:
            rows = con.execute(
                'SELECT s.album, s.disc, s.n, s.title FROM fts '
                'JOIN song s ON s.rowid = fts.rowid '
                'WHERE fts MATCH ? LIMIT ?',
                (_match(query), limit or CANDIDATES)).fetchall()
    except Exception as exc:
        print('song search failed: %s' % exc)
        return []
    needle = key(query)

    def rank(row):
        k = key(row[3])
        if k == needle:
            return (0, 0, row[0])
        if k.startswith(needle):
            return (1, len(k), row[0])
        return (2, k.find(needle), row[0])

    return sorted(rows, key=rank)


def search(query, count=20, offset=0):
    """Subsonic song dicts for search2/search3, resolved against live pages."""
    import server  # late import: server imports this module at startup

    rows = candidates(query)
    if not rows:
        return []
    wanted = {}
    order = []
    for album, disc, n, title in rows:
        if album not in wanted:
            if len(wanted) >= ALBUM_LIMIT:
                continue
            wanted[album] = []
            order.append(album)
        wanted[album].append((disc, n, title))

    out = []
    for slug in order:
        album = server.load_album(slug)
        if not album or not album.get('tracks'):
            continue
        by_title, by_disc_title, by_disc_num, by_num = {}, {}, {}, {}
        for i, t in enumerate(album['tracks'], 1):
            title_key = key(t.get('title'))
            by_title.setdefault(title_key, []).append(i)
            if t.get('disc'):
                by_disc_title.setdefault((t['disc'], title_key), []).append(i)
            if t.get('num'):
                by_num.setdefault(t['num'], i)
                if t.get('disc'):
                    by_disc_num.setdefault((t['disc'], t['num']), i)
        meta = server.album_meta(slug, album)
        seen = set()
        for disc, n, title in wanted[slug]:
            idx = None
            title_key = key(title)
            matches = by_title.get(title_key) or []
            if len(matches) == 1:
                idx = matches[0]
            elif disc and len(by_disc_title.get((disc, title_key)) or []) == 1:
                idx = by_disc_title[(disc, title_key)][0]
            if idx is None and disc and n:
                idx = by_disc_num.get((disc, n))
            if idx is None and n:
                idx = by_num.get(n)
            if idx is None or idx in seen:
                continue
            seen.add(idx)
            out.append(server.song_child(slug, album['title'], meta,
                                         album['tracks'][idx - 1], idx))
        if len(out) >= offset + count:
            break
    return out[offset:offset + count]
