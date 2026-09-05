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
every SONGS_REFRESH_HOURS.  The check is conditional (ETag / Last-Modified
plus a content digest), so an unchanged index costs one HTTP 304 and
nothing else.  A new index is built into a temporary file and swapped in
atomically; queries keep hitting the old database until it is ready.
"""

import gzip
import hashlib
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request

SONGS_URL = os.environ.get('SONGS_URL') or (
    'https://github.com/nmt3325/khinsider-index/releases/download/'
    'song-index/songs.tsv.gz')
SONGS_DB = os.environ.get('SONGS_DB') or './songs.sqlite'
SONGS_MAX_AGE_DAYS = float(os.environ.get('SONGS_MAX_AGE_DAYS') or 0)
SONGS_REFRESH_HOURS = float(os.environ.get('SONGS_REFRESH_HOURS') or 24)
SONG_SEARCH = (os.environ.get('SONG_SEARCH') or 'auto').strip().lower()
ALBUM_LIMIT = int(os.environ.get('SONG_SEARCH_ALBUM_LIMIT') or 12)
CANDIDATES = int(os.environ.get('SONG_SEARCH_CANDIDATES') or 600)

SCHEMA = 1
MIN_QUERY = 3  # the trigram tokenizer cannot match shorter needles
USER_AGENT = 'khinsider-subsonic-relay'

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
        for line in f:
            p = line.rstrip('\n').split('\t')
            if len(p) != 4 or not p[0] or not p[3]:
                continue
            yield (p[0], int(p[1]) if p[1] else None,
                   int(p[2]) if p[2] else None, p[3])


def _build(tsv_gz, db_path, http=None):
    http = http or {}
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
    con.executemany('INSERT INTO meta(k,v) VALUES (?,?)', [
        ('schema', str(SCHEMA)), ('source', SONGS_URL),
        ('built', str(int(time.time()))), ('rows', str(rows)),
        ('etag', http.get('etag') or ''),
        ('last_modified', http.get('last_modified') or ''),
        ('digest', http.get('digest') or '')])
    con.commit()
    con.close()
    os.replace(tmp, db_path)
    return rows


def _read_meta(db_path):
    """Meta of the database currently on disk (etag / digest / built / rows)."""
    if not os.path.exists(db_path):
        return {}
    try:
        con = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True)
        try:
            return dict(con.execute('SELECT k, v FROM meta').fetchall())
        finally:
            con.close()
    except Exception:
        return {}


def _usable(db_path):
    """Open an existing database if it matches the current schema and age."""
    if not os.path.exists(db_path):
        return None
    try:
        con = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True, check_same_thread=False)
        meta = dict(con.execute('SELECT k, v FROM meta').fetchall())
        if int(meta.get('schema', 0)) != SCHEMA:
            con.close()
            return None
        built = int(meta.get('built', 0))
        if SONGS_MAX_AGE_DAYS and time.time() - built > SONGS_MAX_AGE_DAYS * 86400:
            con.close()
            return None
        _state['rows'] = int(meta.get('rows', 0))
        _state['built'] = built
        return con
    except Exception:
        return None


def _refresh(force=False):
    """Download + rebuild when the published index changed.

    The caller must hold _build_lock.  Returns True when a new database was
    swapped in.  Nothing here can take a working index down: on any failure
    the previous database keeps serving queries.
    """
    global _conn
    known = {} if force else _read_meta(SONGS_DB)
    tmp = SONGS_DB + '.tsv.gz'
    try:
        t0 = time.time()
        got = _download(SONGS_URL, tmp, known)
        _state['checked'] = int(time.time())
        if got is None:
            return False                      # HTTP 304, nothing changed
        size, http = got
        http['digest'] = _digest(tmp)
        if not force and known.get('digest') == http['digest']:
            return False                      # byte-identical, no rebuild
        _state['state'] = 'building' if _conn is None else 'refreshing'
        print('song index: downloaded %.1f MB in %.0fs' % (size / 1e6, time.time() - t0))
        rows = _build(tmp, SONGS_DB, http)
        print('song index: built %d rows in %.0fs (%s)' % (
            rows, time.time() - t0, SONGS_DB))
    except Exception as exc:
        _note(exc)
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    con = _usable(SONGS_DB)
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
        # closed one cycle later, when no query can still be holding it
        _retired.append(old)
    return True


def refresh(force=False):
    """Check upstream for a new index. True when the database was replaced."""
    if SONG_SEARCH == 'off':
        return False
    if not _build_lock.acquire(blocking=False):
        return False                          # a build is already running
    try:
        if not force and SONGS_MAX_AGE_DAYS:
            built = int(_read_meta(SONGS_DB).get('built') or 0)
            if built and time.time() - built > SONGS_MAX_AGE_DAYS * 86400:
                force = True
        return _refresh(force=force)
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
        con = _usable(SONGS_DB)
        if con is not None:
            with _lock:
                if _conn is None:
                    _conn = con
                    _state['state'] = 'ready'
                    _state['error'] = None
                return _conn
        _refresh(force=True)
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
        by_title, by_num = {}, {}
        for i, t in enumerate(album['tracks'], 1):
            by_title.setdefault(key(t.get('title')), i)
            if t.get('num'):
                by_num.setdefault(t['num'], i)
        meta = server.album_meta(slug, album)
        seen = set()
        for disc, n, title in wanted[slug]:
            idx = by_title.get(key(title))
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
