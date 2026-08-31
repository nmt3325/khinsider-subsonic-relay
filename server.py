#!/usr/bin/env python3
"""
khinsider-subsonic-relay: a Subsonic API-compatible relay server for
downloads.khinsider.com.

No audio files are hosted. Album/track pages are fetched on demand from the
live site (Cloudflare bypass via curl_cffi TLS impersonation) and cached.
/rest/stream resolves the track's real CDN URL and returns a 302 redirect,
so clients download directly from khinsider's CDN (vgmtreasurechest.com).
Set PROXY_STREAM=1 or pass &proxy=1 to stream bytes through this server.

Metadata
--------
The album page's info block is parsed for Year, Published by, Developed by,
Platforms, Album type, Catalog Number and Date Added, and mapped onto
Subsonic / OpenSubsonic fields:

    Year         -> year, releaseDate, originalReleaseDate
    Published by -> artist / albumArtist (falls back to Developed by)
    Platforms    -> genre, genres[]
    Album type   -> genre, genres[]
    Date Added   -> created
    Catalog Number, Developed by, Uploaded by -> kept in the album cache

Album pages are fetched anyway in order to list tracks, so parsing this costs
no extra requests. Subsonic clients render what the *server* sends, not the
ID3 tags inside the audio file, so there is no reason to read ID3 out of the
streamed file (which would need an extra range request per track).

If library.json carries the same metadata per album (produced by
khinsider-index/scripts/crawl_album_meta.py + build_library.py), then album
lists, browse and search responses are enriched too, publishers become
browsable album artists in getArtists, and getGenres returns the real
platform / album-type genres.

Config (env):
  SUBSONIC_USER / SUBSONIC_PASSWORD  - login (default: admin / admin)
  PORT                               - listen port (default: 8080)
  LIBRARY_PATH                       - library.json (auto-downloaded if missing,
                                       .gz accepted)
  LIBRARY_URL                        - override download URL for library.json
  CACHE_DIR                          - page cache dir (default: ./cache)
  PROXY_STREAM                       - '1' to always proxy instead of 302
  GENRE_SOURCES                      - which fields become genres, in order
                                       (default: platform,album_type)
  ARTIST_MODE                        - auto | publisher | letter (default auto):
                                       what getArtists lists
  FALLBACK_ARTIST                    - artist used when no publisher is known
"""
import gzip
import hashlib
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag
from curl_cffi import requests as creq
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

BASE = 'https://downloads.khinsider.com'
API_VERSION = '1.16.1'
SERVER_TYPE = 'khinsider-relay'
SERVER_VERSION = '0.2.0'
USERNAME = os.environ.get('SUBSONIC_USER', 'admin')
PASSWORD = os.environ.get('SUBSONIC_PASSWORD', 'admin')
CACHE_DIR = os.environ.get('CACHE_DIR', './cache')
LIBRARY_PATH = os.environ.get('LIBRARY_PATH', './library.json')
LIBRARY_URL = os.environ.get(
    'LIBRARY_URL',
    'https://github.com/nmt3325/khinsider-index/releases/download/v2026.09.01/library.json',
)
PROXY_STREAM = os.environ.get('PROXY_STREAM', '') == '1'
GENRE_SOURCES = [s.strip().lower() for s in os.environ.get('GENRE_SOURCES', 'platform,album_type').split(',') if s.strip()]
ARTIST_MODE = os.environ.get('ARTIST_MODE', 'auto').strip().lower()
FALLBACK_ARTIST = os.environ.get('FALLBACK_ARTIST', 'KHInsider')

# bump when the shape of a cached album changes so old caches are re-parsed
ALBUM_CACHE_VERSION = 2

AUDIO_EXT_RE = re.compile(r'\.(mp3|flac|ogg|m4a|opus|wma|wav)$', re.I)
CONTENT_TYPES = {
    'mp3': 'audio/mpeg',
    'flac': 'audio/flac',
    'ogg': 'audio/ogg',
    'm4a': 'audio/mp4',
    'opus': 'audio/opus',
    'wma': 'audio/x-ms-wma',
    'wav': 'audio/wav',
}

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(os.path.join(CACHE_DIR, 'albums'), exist_ok=True)
os.makedirs(os.path.join(CACHE_DIR, 'tracks'), exist_ok=True)

sess = creq.Session(impersonate='chrome')

# ---------------- metadata helpers ----------------


def _as_list(v):
    """Accept 'x', ['x','y'] or {'x': url} (scrape.py style) -> ['x', ...]."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, dict):
        return [str(k).strip() for k in v.keys() if str(k).strip()]
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()]


def _as_year(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 1000 <= v <= 2999 else None
    m = re.search(r'(?:19|20)\d{2}', str(v))
    return int(m.group(0)) if m else None


def _dedupe(seq):
    out, seen = [], set()
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def genre_list(meta):
    """Map platform / album type (per GENRE_SOURCES) onto Subsonic genres."""
    out = []
    for src in GENRE_SOURCES:
        if src in ('platform', 'platforms'):
            out += meta.get('platforms') or []
        elif src in ('album_type', 'albumtype', 'type'):
            if meta.get('album_type'):
                out.append(meta['album_type'])
        elif src in ('developer', 'developers'):
            out += meta.get('developers') or []
        elif src in ('publisher', 'publishers'):
            out += meta.get('publishers') or []
    return _dedupe(out)


def artist_name(meta):
    """Album artist: publisher, else developer, else FALLBACK_ARTIST."""
    for key in ('publishers', 'developers'):
        vals = meta.get(key) or []
        if vals:
            return ', '.join(vals)
    return FALLBACK_ARTIST


def pub_id(name):
    return 'pub/' + urllib.parse.quote(name, safe='')


def artist_id(meta):
    for key in ('publishers', 'developers'):
        vals = meta.get(key) or []
        if vals and USE_PUB_ARTISTS:
            return pub_id(vals[0])
    if meta.get('letter'):
        return 'letter/' + meta['letter']
    return None


def _iso_created(date_added):
    """'2026-04-07' / ISO string -> Subsonic 'created' timestamp."""
    if not date_added:
        return None
    s = str(date_added)
    if 'T' in s:
        return s
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', s)
    if m:
        return s + 'T00:00:00.000Z'
    return None


def _derive_letter(title):
    ch = (title or '').strip()[:1].upper()
    return ch if 'A' <= ch <= 'Z' else '0-9'


def normalize_meta(a):
    """library.json row -> internal metadata dict (tolerates several shapes)."""
    slug = a.get('slug')
    title = a.get('title') or a.get('name') or slug
    album_type = a.get('album_type') or a.get('albumType')
    if isinstance(album_type, (list, tuple, dict)):
        vals = _as_list(album_type)
        album_type = vals[0] if vals else None
    if not album_type and a.get('genres') is not None:
        vals = _as_list(a.get('genres'))
        album_type = vals[0] if vals else None
    track_count = a.get('track_count') or a.get('songCount')
    if track_count is None and isinstance(a.get('tracks'), int):
        track_count = a.get('tracks')
    meta = {
        'title': title,
        'letter': a.get('letter') or _derive_letter(title),
        'year': _as_year(a.get('year') if a.get('year') is not None else (a.get('release_year') or a.get('release_date'))),
        'publishers': _as_list(a.get('publishers') if a.get('publishers') is not None else a.get('publisher')),
        'developers': _as_list(a.get('developers') if a.get('developers') is not None else a.get('developer')),
        'platforms': _as_list(a.get('platforms') if a.get('platforms') is not None else a.get('platform')),
        'album_type': album_type or None,
        'date_added': a.get('date_added') or a.get('dateAdded'),
        'catalog_number': a.get('catalog_number') or a.get('catalogNumber'),
        'track_count': track_count if isinstance(track_count, int) else None,
    }
    return meta

# ---------------- library ----------------


def _load_json_file(path):
    if path.endswith('.gz'):
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            return json.load(f)
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def ensure_library():
    if not os.path.exists(LIBRARY_PATH):
        print(f'downloading library from {LIBRARY_URL} ...')
        urllib.request.urlretrieve(LIBRARY_URL, LIBRARY_PATH)
    return _load_json_file(LIBRARY_PATH)


_lib = ensure_library()
ALBUMS = {}          # slug -> metadata dict (title, letter, year, publishers, ...)
LETTER_ALBUMS = {}   # letter -> [(slug, title)] sorted by title
PUB_ALBUMS = {}      # publisher (or developer) -> [(slug, title)]
GENRE_ALBUMS = {}    # genre name -> [slug]
SEARCH = []          # (lower_title, slug)
LIB_META_ALBUMS = 0  # how many rows actually carry extra metadata

for a in _lib['albums']:
    slug = a['slug']
    meta = normalize_meta(a)
    ALBUMS[slug] = meta
    LETTER_ALBUMS.setdefault(meta['letter'], []).append((slug, meta['title']))
    SEARCH.append((meta['title'].lower(), slug))
    if meta['year'] or meta['publishers'] or meta['platforms'] or meta['album_type']:
        LIB_META_ALBUMS += 1
    for p in (meta['publishers'] or meta['developers']):
        PUB_ALBUMS.setdefault(p, []).append((slug, meta['title']))
for L in LETTER_ALBUMS:
    LETTER_ALBUMS[L].sort(key=lambda x: x[1].lower())
for p in PUB_ALBUMS:
    PUB_ALBUMS[p].sort(key=lambda x: x[1].lower())
LETTERS = ['0-9'] + [chr(c) for c in range(ord('A'), ord('Z') + 1)]
LETTERS = [L for L in LETTERS if L in LETTER_ALBUMS]
PUBLISHERS = sorted(PUB_ALBUMS.keys(), key=lambda s: s.lower())
USE_PUB_ARTISTS = ARTIST_MODE == 'publisher' or (ARTIST_MODE == 'auto' and bool(PUB_ALBUMS))
for slug, meta in ALBUMS.items():
    for g in genre_list(meta):
        GENRE_ALBUMS.setdefault(g, []).append(slug)
for g in GENRE_ALBUMS:
    GENRE_ALBUMS[g].sort(key=lambda s: ALBUMS[s]['title'].lower())
GENRES = sorted(GENRE_ALBUMS.keys(), key=lambda s: s.lower())
print(f'library loaded: {len(ALBUMS)} albums in {len(LETTERS)} sections, '
      f'{LIB_META_ALBUMS} with metadata, {len(PUBLISHERS)} publishers, {len(GENRES)} genres '
      f'(artists = {"publisher" if USE_PUB_ARTISTS else "letter"})')

# ---------------- subsonic plumbing ----------------

SUBSONIC_NS = 'http://subsonic.org/restapi'


def check_auth(q):
    u = q.get('u')
    p = q.get('p')
    t = q.get('t')
    s = q.get('s')
    if u != USERNAME:
        return False
    if p is not None:
        if p.startswith('enc:'):
            try:
                p = bytes.fromhex(p[4:]).decode('utf-8', 'replace')
            except ValueError:
                return False
        return p == PASSWORD
    if t and s:
        return hashlib.md5((PASSWORD + s).encode()).hexdigest() == t.lower()
    return False


def _tags(nodes, name):
    out = []
    for n in nodes:
        if not isinstance(n, Tag):
            continue
        if n.name == name:
            out.append(n)
        out += n.find_all(name)
    return out


def _fill(el, value):
    """Render a python value into an XML element (attributes + children)."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k == '_text':
                el.text = str(v)
            elif isinstance(v, dict):
                _fill(ET.SubElement(el, k), v)
            elif isinstance(v, list):
                for item in v:
                    child = ET.SubElement(el, k)
                    if isinstance(item, dict):
                        _fill(child, item)
                    else:
                        child.text = str(item)
            elif isinstance(v, bool):
                el.set(k, 'true' if v else 'false')
            elif v is not None:
                el.set(k, str(v))
    elif isinstance(value, list):
        for item in value:
            _fill(el, item)
    elif value is not None:
        el.text = str(value)


def _json_prep(value):
    """'_text' is XML element text; JSON uses the 'value' key instead."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == '_text':
                if 'value' not in value:
                    out['value'] = v
                continue
            out[k] = _json_prep(v)
        return out
    if isinstance(value, list):
        return [_json_prep(v) for v in value]
    return value


def respond(payload, fmt, status='ok', error=None):
    if fmt == 'json':
        body = {'status': status, 'version': API_VERSION, 'type': SERVER_TYPE,
                'serverVersion': SERVER_VERSION, 'openSubsonic': True}
        if error:
            body['error'] = _json_prep(error)
        else:
            body.update(_json_prep(payload or {}))
        return JSONResponse({'subsonic-response': body})
    root = ET.Element('subsonic-response', {
        'xmlns': SUBSONIC_NS, 'status': status, 'version': API_VERSION,
        'type': SERVER_TYPE, 'serverVersion': SERVER_VERSION, 'openSubsonic': 'true'})
    if error:
        _fill(ET.SubElement(root, 'error'), error)
    else:
        for k, v in (payload or {}).items():
            _fill(ET.SubElement(root, k), v)
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding='unicode')
    return Response(content=xml, media_type='text/xml; charset=utf-8')


def sub_error(fmt, code, message):
    return respond(None, fmt, status='failed', error={'code': code, 'message': message})

# ---------------- page cache ----------------


def _cache_path(kind, key):
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', key)
    if len(safe) > 120:
        safe = safe[:80] + '-' + hashlib.sha1(key.encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, kind, safe + '.json')


def _cache_get(kind, key, max_age=None):
    path = _cache_path(kind, key)
    try:
        st = os.stat(path)
    except OSError:
        return None
    if max_age and time.time() - st.st_mtime > max_age:
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _cache_put(kind, key, data):
    path = _cache_path(kind, key)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(tmp, path)

# ---------------- khinsider page parsing ----------------

SIZE_RE = re.compile(r'^\s*([\d.]+)\s*(B|KB|MB|GB)\s*$', re.I)
DURATION_RE = re.compile(r'^\s*(\d{1,3}):([0-5]\d)(?::([0-5]\d))?\s*$')
NUM_RE = re.compile(r'^\s*(\d{1,4})\.?\s*$')
MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
          'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
INFO_LABELS = {
    'platforms': 'platforms',
    'platform': 'platforms',
    'year': 'year',
    'album type': 'album_type',
    'developed by': 'developers',
    'published by': 'publishers',
    'catalog number': 'catalog_number',
    'date added': 'date_added',
    'number of files': 'file_count',
    'total filesize': 'total_filesize',
    'uploaded by': 'uploaders',
}


def _human2bytes(s):
    if not s:
        return None
    m = SIZE_RE.match(str(s))
    if not m:
        return None
    mult = {'b': 1, 'kb': 1024, 'mb': 1024 ** 2, 'gb': 1024 ** 3}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def _duration(s):
    if not s:
        return None
    m = DURATION_RE.match(str(s))
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is None:
        return int(a) * 60 + int(b)
    return int(a) * 3600 + int(b) * 60 + int(c)


def _parse_khdate(s):
    """'Apr 7th, 2026' -> '2026-04-07'."""
    if not s:
        return None
    s = str(s)
    m = re.search(r'([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})', s)
    if m:
        mon = MONTHS.get(m.group(1)[:3].lower())
        if mon:
            return '%04d-%02d-%02d' % (int(m.group(3)), mon, int(m.group(2)))
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', s)
    return m.group(0) if m else None


def _info_lines(p):
    """Split the album info paragraph into logical lines on <br>."""
    lines, cur = [], []
    for node in p.children:
        name = getattr(node, 'name', None)
        if name == 'table':
            break
        if name == 'br':
            lines.append(cur)
            cur = []
            continue
        cur.append(node)
    lines.append(cur)
    return [ln for ln in lines if ln]


def _line_text(nodes):
    parts = []
    for n in nodes:
        if isinstance(n, Comment):
            continue
        parts.append(n.get_text(' ', strip=True) if isinstance(n, Tag) else str(n))
    return re.sub(r'\s+', ' ', ' '.join(parts)).strip()


def parse_album_info(soup):
    """Parse the album info block (Year / Published by / Platforms / ...)."""
    info = {}
    target = None
    for cand in soup.select('#pageContent p[align=left]'):
        low = cand.get_text(' ', strip=True).lower()
        if any(k in low for k in ('year:', 'platforms:', 'platform:', 'album type:',
                                  'published by:', 'developed by:', 'date added:')):
            target = cand
            break
    if target is None:
        return info
    for nodes in _info_lines(target):
        text = _line_text(nodes)
        m = re.match(r'^([A-Za-z][A-Za-z0-9 /]{1,30}?)\s*:\s*(.*)$', text)
        if not m:
            continue
        key = INFO_LABELS.get(m.group(1).strip().lower())
        if not key:
            continue
        rest = m.group(2).strip()
        links = [a.get_text(' ', strip=True) for a in _tags(nodes, 'a')]
        links = _dedupe([x for x in links if x and 'change log' not in x.lower()])
        bolds = _dedupe([b.get_text(' ', strip=True) for b in _tags(nodes, 'b')])
        if key in ('platforms', 'developers', 'publishers', 'uploaders'):
            vals = links or [x.strip() for x in rest.split(',') if x.strip()]
            info[key] = _dedupe(vals)
        elif key == 'album_type':
            info[key] = (links[0] if links else (bolds[0] if bolds else rest)) or None
        elif key == 'year':
            info[key] = _as_year(bolds[0] if bolds else rest)
        elif key == 'date_added':
            info[key] = _parse_khdate(bolds[0] if bolds else rest)
        elif key == 'catalog_number':
            info[key] = ((bolds[0] if bolds else rest) or '').strip() or None
        elif key == 'file_count':
            m2 = re.search(r'\d+', (bolds[0] if bolds else rest) or '')
            info[key] = int(m2.group(0)) if m2 else None
        elif key == 'total_filesize':
            info[key] = rest or None
    return info


def _songlist_roles(table):
    """Map <td> index -> role using the songlist header row.

    The 'Song Name' header is followed by an unlabelled track-length column,
    so every header after it is shifted by one cell.
    """
    roles = {}
    hdr = table.find('tr', id='songlist_header') or table.find('tr')
    if not hdr:
        return roles
    cells = [c.get_text(' ', strip=True).strip().lower() for c in hdr.find_all(['th', 'td'])]
    shift = 0
    for i, label in enumerate(cells):
        idx = i + shift
        if label == '#':
            roles[idx] = 'num'
        elif label == 'cd':
            roles[idx] = 'disc'
        elif label in ('song name', 'song title', 'title'):
            roles[idx] = 'title'
            roles[idx + 1] = 'duration'
            shift = 1
        elif label in CONTENT_TYPES:
            roles[idx] = 'size_' + label
        elif label.replace(' ', '') in CONTENT_TYPES:
            roles[idx] = 'size_' + label.replace(' ', '')
    return roles


def _parse_songlist(table, slug):
    """Parse table#songlist into track dicts (header aware + regex fallback)."""
    roles = _songlist_roles(table)
    prefix = '/game-soundtracks/album/%s/' % slug
    tracks = []
    for tr in table.find_all('tr'):
        if tr.get('id') in ('songlist_header', 'songlist_footer'):
            continue
        cells = tr.find_all('td')
        if not cells:
            continue
        basename = None
        for a in tr.find_all('a', href=True):
            href = urllib.parse.urlparse(a['href']).path
            if prefix in href and AUDIO_EXT_RE.search(href):
                basename = urllib.parse.unquote(href.rsplit('/', 1)[-1])
                break
        if not basename:
            continue
        t = {'basename': basename, 'num': None, 'disc': None, 'title': None,
             'duration': None, 'sizes': {}}
        for idx, cell in enumerate(cells):
            role = roles.get(idx)
            text = cell.get_text(' ', strip=True)
            if role == 'num':
                m = NUM_RE.match(text)
                t['num'] = int(m.group(1)) if m else None
            elif role == 'disc':
                m = NUM_RE.match(text)
                t['disc'] = int(m.group(1)) if m else None
            elif role == 'title':
                t['title'] = text or None
            elif role == 'duration':
                t['duration'] = _duration(text)
            elif role and role.startswith('size_'):
                size = _human2bytes(text)
                if size:
                    t['sizes'][role[5:]] = size
        if t['title'] is None or t['duration'] is None or not t['sizes']:
            # fallback for layouts without a usable header row
            sizes_seen = []
            for cell in cells:
                text = cell.get_text(' ', strip=True)
                if not text:
                    continue
                if t['title'] is None and cell.find('a', href=True) and not AUDIO_EXT_RE.search(text) and not SIZE_RE.match(text) and not DURATION_RE.match(text) and not NUM_RE.match(text):
                    t['title'] = text
                elif t['duration'] is None and DURATION_RE.match(text):
                    t['duration'] = _duration(text)
                elif SIZE_RE.match(text):
                    sizes_seen.append(_human2bytes(text))
                elif t['num'] is None and NUM_RE.match(text):
                    t['num'] = int(NUM_RE.match(text).group(1))
            if not t['sizes'] and sizes_seen:
                for fmt, size in zip(['mp3', 'flac'], sizes_seen):
                    if size:
                        t['sizes'][fmt] = size
        if not t['title']:
            t['title'] = os.path.splitext(basename)[0]
        t['num'] = t['num'] or len(tracks) + 1
        formats = [f for f in ('mp3', 'flac', 'ogg', 'm4a', 'opus', 'wma', 'wav') if f in t['sizes']]
        t['formats'] = formats or ['mp3']
        t['size'] = t['sizes'].get(t['formats'][0])
        tracks.append(t)
    return tracks


def load_album(slug):
    """Album page -> {slug,title,cover,covers,info,tracks}, cached for 30 days."""
    cached = _cache_get('albums', slug, max_age=30 * 86400)
    if cached and cached.get('v') == ALBUM_CACHE_VERSION:
        return cached
    try:
        r = sess.get('%s/game-soundtracks/album/%s' % (BASE, slug), timeout=30)
    except Exception as exc:
        print('album fetch failed for %s: %s' % (slug, exc))
        return None
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, 'html.parser')
    h2 = soup.select_one('#pageContent h2')
    title = h2.get_text(' ', strip=True) if h2 else None
    if not title or title.lower().startswith('ooops'):
        return None
    covers = _dedupe([a['href'] for a in soup.select('div.albumImage a[href]')])
    table = soup.select_one('table#songlist')
    tracks = _parse_songlist(table, slug) if table else []
    alt = soup.select_one('p.albuminfoAlternativeTitles')
    album = {
        'v': ALBUM_CACHE_VERSION,
        'slug': slug,
        'title': title,
        'cover': covers[0] if covers else None,
        'covers': covers,
        'alt_titles': alt.get_text(' ', strip=True) if alt else None,
        'info': parse_album_info(soup),
        'tracks': tracks,
    }
    _cache_put('albums', slug, album)
    return album


def resolve_track(slug, basename):
    """Track page -> {'files': {ext: cdn_url}}, cached for 30 days."""
    key = '%s/%s' % (slug, basename)
    cached = _cache_get('tracks', key, max_age=30 * 86400)
    if cached:
        return cached
    url = '%s/game-soundtracks/album/%s/%s' % (BASE, slug, urllib.parse.quote(basename))
    try:
        r = sess.get(url, timeout=30)
    except Exception as exc:
        print('track fetch failed for %s: %s' % (key, exc))
        return None
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, 'html.parser')
    files = {}
    for span in soup.select('span.songDownloadLink'):
        a = span.find_parent('a')
        if not a or not a.get('href'):
            continue
        m = AUDIO_EXT_RE.search(urllib.parse.urlparse(a['href']).path)
        if m:
            files.setdefault(m.group(1).lower(), a['href'])
    if not files:
        return None
    data = {'files': files}
    _cache_put('tracks', key, data)
    return data

# ---------------- subsonic models ----------------


def album_meta(slug, album=None):
    """Metadata for an album: live page info wins, library.json fills gaps."""
    lib = ALBUMS.get(slug) or {'title': slug, 'letter': _derive_letter(slug)}
    info = (album or {}).get('info') or {}
    meta = dict(lib)
    if album and album.get('title'):
        meta['title'] = album['title']
    for key in ('year', 'album_type', 'date_added', 'catalog_number', 'file_count', 'total_filesize'):
        if info.get(key):
            meta[key] = info[key]
    for key in ('publishers', 'developers', 'platforms', 'uploaders'):
        if info.get(key):
            meta[key] = info[key]
        meta.setdefault(key, [])
    if album and album.get('tracks'):
        meta['track_count'] = len(album['tracks'])
    return meta


def apply_meta(d, meta):
    """release date -> year, publisher -> album artist, platform/type -> genre."""
    artist = artist_name(meta)
    d['artist'] = artist
    d['albumArtist'] = artist
    d['displayArtist'] = artist
    d['displayAlbumArtist'] = artist
    aid = artist_id(meta)
    if aid:
        d['artistId'] = aid
    genres = genre_list(meta)
    if genres:
        d['genre'] = genres[0]
        d['genres'] = [{'name': g} for g in genres]
    year = meta.get('year')
    if year:
        d['year'] = year
        d['originalReleaseDate'] = {'year': year}
        d['releaseDate'] = {'year': year}
    created = _iso_created(meta.get('date_added'))
    if created:
        d['created'] = created
    return d


def album_child(slug, parent=None, album=None):
    """Album as a directory child / album list entry."""
    meta = album_meta(slug, album)
    d = {
        'id': 'album/' + slug,
        'parent': parent or ('letter/' + meta.get('letter', '0-9')),
        'isDir': True,
        'title': meta['title'],
        'name': meta['title'],
        'album': meta['title'],
        'coverArt': 'album/' + slug,
    }
    apply_meta(d, meta)
    if meta.get('track_count'):
        d['songCount'] = meta['track_count']
    return d


def _sanitize(name):
    return re.sub(r'[\\/:*?"<>|]+', '_', name).strip() or 'unknown'


def song_child(slug, album_title, meta, t, idx, cover=None):
    suffix = (t.get('formats') or ['mp3'])[0]
    size = (t.get('sizes') or {}).get(suffix) or t.get('size')
    duration = t.get('duration')
    d = {
        'id': 'track/%s/%d' % (slug, idx),
        'parent': 'album/' + slug,
        'isDir': False,
        'title': t.get('title') or os.path.splitext(t['basename'])[0],
        'album': album_title,
        'albumId': 'album/' + slug,
        'coverArt': cover or ('album/' + slug),
        'type': 'music',
        'suffix': suffix,
        'contentType': CONTENT_TYPES.get(suffix, 'audio/mpeg'),
        'track': t.get('num') or idx,
        'mediaType': 'song',
    }
    if t.get('disc'):
        d['discNumber'] = t['disc']
    if duration:
        d['duration'] = duration
    if size:
        d['size'] = size
        if duration:
            d['bitRate'] = max(1, round(size * 8 / duration / 1000))
    apply_meta(d, meta)
    d['path'] = '%s/%s/%s' % (_sanitize(d['artist']), _sanitize(album_title), _sanitize(t['basename']))
    return d

# ---------------- derived orderings ----------------

ALPHA_SLUGS = [slug for L in LETTERS for slug, _ in LETTER_ALBUMS[L]]
YEAR_SLUGS = sorted((s for s, m in ALBUMS.items() if m.get('year')),
                    key=lambda s: (ALBUMS[s]['year'], ALBUMS[s]['title'].lower()))
NEWEST_SLUGS = sorted((s for s, m in ALBUMS.items() if m.get('date_added')),
                      key=lambda s: ALBUMS[s]['date_added'], reverse=True)

app = FastAPI()


def _albums_for_artist(artist_id_):
    if artist_id_.startswith('letter/'):
        return LETTER_ALBUMS.get(artist_id_[len('letter/'):], [])
    if artist_id_.startswith('pub/'):
        return PUB_ALBUMS.get(urllib.parse.unquote(artist_id_[len('pub/'):]), [])
    return []


def _artist_entries():
    """(id, name, albumCount) for every browsable artist."""
    if USE_PUB_ARTISTS:
        return [(pub_id(p), p, len(PUB_ALBUMS[p])) for p in PUBLISHERS]
    return [('letter/' + L, L, len(LETTER_ALBUMS[L])) for L in LETTERS]


def _int(q, key, default=0):
    try:
        return int(q.get(key, default) or default)
    except (TypeError, ValueError):
        return default


@app.get('/rest/{endpoint}')
@app.post('/rest/{endpoint}')
async def subsonic(endpoint: str, request: Request):
    q = dict(request.query_params)
    if request.method == 'POST':
        try:
            form = await request.form()
            for k, v in form.items():
                q.setdefault(k, str(v))
        except Exception:
            pass
    fmt = 'json' if q.get('f') in ('json', 'jsonp') else 'xml'
    ep = endpoint[:-5] if endpoint.endswith('.view') else endpoint

    if ep not in ('ping', 'getLicense') and not check_auth(q):
        return sub_error(fmt, 40, 'Wrong username or password.')

    if ep == 'ping':
        return respond({}, fmt)

    if ep == 'getLicense':
        return respond({'license': {'valid': True, 'email': 'nobody@example.com'}}, fmt)

    if ep == 'getUser':
        return respond({'user': {
            'username': USERNAME, 'email': 'nobody@example.com', 'scrobblingEnabled': False,
            'adminRole': False, 'settingsRole': False, 'downloadRole': True,
            'uploadRole': False, 'playlistRole': True, 'coverArtRole': True,
            'commentRole': False, 'podcastRole': False, 'streamRole': True,
            'jukeboxRole': False, 'shareRole': False, 'folder': [0],
        }}, fmt)

    if ep == 'getOpenSubsonicExtensions':
        return respond({'openSubsonicExtensions': []}, fmt)

    if ep == 'getMusicFolders':
        return respond({'musicFolders': {'musicFolder': [{'id': 0, 'name': 'KHInsider'}]}}, fmt)

    if ep == 'getIndexes':
        # file/folder view: sections stay alphabetical
        return respond({'indexes': {
            'lastModified': int(time.time() * 1000),
            'ignoredArticles': 'The El La Los Las Le Les',
            'index': [{'name': L, 'artist': [{'id': 'letter/' + L, 'name': L,
                                              'albumCount': len(LETTER_ALBUMS[L])}]} for L in LETTERS],
        }}, fmt)

    if ep == 'getArtists':
        # ID3 view: publishers become album artists when the library has them
        buckets = {}
        for aid, name, count in _artist_entries():
            buckets.setdefault(_derive_letter(name), []).append(
                {'id': aid, 'name': name, 'albumCount': count})
        index = [{'name': L, 'artist': buckets[L]} for L in sorted(buckets.keys())]
        return respond({'artists': {
            'ignoredArticles': 'The El La Los Las Le Les', 'index': index,
        }}, fmt)

    if ep == 'getArtist':
        aid = q.get('id', '')
        rows = _albums_for_artist(aid)
        if not rows:
            return sub_error(fmt, 70, 'Artist not found.')
        name = aid[len('pub/'):] if aid.startswith('pub/') else aid[len('letter/'):]
        name = urllib.parse.unquote(name)
        return respond({'artist': {
            'id': aid, 'name': name, 'albumCount': len(rows),
            'album': [album_child(slug, parent=aid) for slug, _ in rows],
        }}, fmt)

    if ep == 'getArtistInfo' or ep == 'getArtistInfo2':
        key = 'artistInfo' if ep == 'getArtistInfo' else 'artistInfo2'
        return respond({key: {}}, fmt)

    if ep == 'getMusicDirectory':
        did = q.get('id', '')
        if did.startswith('letter/') or did.startswith('pub/'):
            rows = _albums_for_artist(did)
            if not rows:
                return sub_error(fmt, 70, 'Directory not found.')
            name = urllib.parse.unquote(did.split('/', 1)[1])
            return respond({'directory': {
                'id': did, 'parent': '0', 'name': name,
                'child': [album_child(slug, parent=did) for slug, _ in rows],
            }}, fmt)
        if did.startswith('album/'):
            slug = did[len('album/'):]
            album = load_album(slug)
            if not album:
                return sub_error(fmt, 70, 'Album not found.')
            meta = album_meta(slug, album)
            return respond({'directory': {
                'id': did, 'parent': 'letter/' + meta.get('letter', '0-9'),
                'name': album['title'],
                'child': [song_child(slug, album['title'], meta, t, i + 1)
                          for i, t in enumerate(album['tracks'])],
            }}, fmt)
        return sub_error(fmt, 70, 'Directory not found.')

    if ep in ('getAlbum', 'getAlbumInfo', 'getAlbumInfo2'):
        aid = q.get('id', '')
        slug = aid[len('album/'):] if aid.startswith('album/') else aid
        album = load_album(slug)
        if not album:
            return sub_error(fmt, 70, 'Album not found.')
        meta = album_meta(slug, album)
        songs = [song_child(slug, album['title'], meta, t, i + 1)
                 for i, t in enumerate(album['tracks'])]
        if ep != 'getAlbum':
            key = 'albumInfo' if ep == 'getAlbumInfo' else 'albumInfo2'
            notes = []
            if meta.get('platforms'):
                notes.append('Platforms: ' + ', '.join(meta['platforms']))
            if meta.get('album_type'):
                notes.append('Album type: ' + meta['album_type'])
            if meta.get('developers'):
                notes.append('Developed by: ' + ', '.join(meta['developers']))
            if meta.get('publishers'):
                notes.append('Published by: ' + ', '.join(meta['publishers']))
            if meta.get('catalog_number'):
                notes.append('Catalog number: ' + meta['catalog_number'])
            info = {}
            if notes:
                info['notes'] = ' | '.join(notes)
            if album.get('cover'):
                info['largeImageUrl'] = album['cover']
            return respond({key: info}, fmt)
        out = album_child(slug, album=album)
        out['songCount'] = len(songs)
        out['duration'] = sum(s.get('duration') or 0 for s in songs)
        out['song'] = songs
        if meta.get('catalog_number'):
            out['musicBrainzId'] = None
        return respond({'album': out}, fmt)

    if ep in ('getAlbumList', 'getAlbumList2'):
        kind = q.get('type', 'alphabeticalByName')
        size = max(1, min(_int(q, 'size', 10), 500))
        offset = max(0, _int(q, 'offset', 0))
        slugs = None
        if kind == 'byGenre':
            slugs = GENRE_ALBUMS.get(q.get('genre', ''), [])
        elif kind == 'byYear':
            fy, ty = _int(q, 'fromYear', 0), _int(q, 'toYear', 9999)
            lo, hi = min(fy, ty), max(fy, ty)
            slugs = [s for s in YEAR_SLUGS if lo <= ALBUMS[s]['year'] <= hi]
            if fy > ty:
                slugs = list(reversed(slugs))
        elif kind == 'newest':
            slugs = NEWEST_SLUGS or None
        elif kind == 'random':
            pool = ALPHA_SLUGS
            slugs = random.sample(pool, min(size, len(pool)))
            offset = 0
        elif kind == 'starred':
            slugs = []
        if slugs is None:
            slugs = ALPHA_SLUGS
        page = slugs[offset:offset + size]
        key = 'albumList' if ep == 'getAlbumList' else 'albumList2'
        return respond({key: {'album': [album_child(s) for s in page]}}, fmt)

    if ep == 'getGenres':
        if GENRES:
            genres = [{'_text': g, 'value': g, 'name': g,
                       'albumCount': len(GENRE_ALBUMS[g]), 'songCount': 0} for g in GENRES]
        else:
            genres = [{'_text': 'Video Game Music', 'value': 'Video Game Music',
                       'name': 'Video Game Music', 'albumCount': len(ALBUMS), 'songCount': 0}]
        return respond({'genres': {'genre': genres}}, fmt)

    if ep == 'getSongsByGenre':
        # song-level genre browsing would need one album page fetch per album
        return respond({'songsByGenre': {'song': []}}, fmt)

    if ep in ('search2', 'search3'):
        query = (q.get('query') or '').strip().strip('"').lower()
        acount = max(0, _int(q, 'artistCount', 20))
        alcount = max(0, _int(q, 'albumCount', 20))
        aoffset = max(0, _int(q, 'albumOffset', 0))
        artists, albums = [], []
        if query:
            hits = [slug for title, slug in SEARCH if query in title]
            albums = [album_child(s) for s in hits[aoffset:aoffset + alcount]]
            if acount:
                for aid, name, count in _artist_entries():
                    if query in name.lower():
                        artists.append({'id': aid, 'name': name, 'albumCount': count})
                        if len(artists) >= acount:
                            break
        key = 'searchResult2' if ep == 'search2' else 'searchResult3'
        return respond({key: {'artist': artists, 'album': albums, 'song': []}}, fmt)

    if ep == 'getSong':
        sid = q.get('id', '')
        m = re.match(r'^track/(.+)/(\d+)$', sid)
        if not m:
            return sub_error(fmt, 70, 'Song not found.')
        slug, idx = m.group(1), int(m.group(2))
        album = load_album(slug)
        if not album or idx < 1 or idx > len(album['tracks']):
            return sub_error(fmt, 70, 'Song not found.')
        meta = album_meta(slug, album)
        return respond({'song': song_child(slug, album['title'], meta,
                                          album['tracks'][idx - 1], idx)}, fmt)

    if ep == 'getCoverArt':
        cid = q.get('id', '')
        slug = None
        if cid.startswith('album/'):
            slug = cid[len('album/'):]
        else:
            m = re.match(r'^track/(.+)/\d+$', cid)
            if m:
                slug = m.group(1)
        album = load_album(slug) if slug else None
        if not album or not album.get('cover'):
            return sub_error(fmt, 70, 'Cover art not found.')
        try:
            r = sess.get(album['cover'], timeout=30)
        except Exception:
            return sub_error(fmt, 0, 'Cover art fetch failed.')
        if r.status_code != 200:
            return sub_error(fmt, 70, 'Cover art not found.')
        return Response(content=r.content,
                        media_type=r.headers.get('content-type', 'image/jpeg'),
                        headers={'Cache-Control': 'public, max-age=604800'})

    if ep in ('stream', 'download'):
        sid = q.get('id', '')
        m = re.match(r'^track/(.+)/(\d+)$', sid)
        if not m:
            return sub_error(fmt, 70, 'Song not found.')
        slug, idx = m.group(1), int(m.group(2))
        album = load_album(slug)
        if not album or idx < 1 or idx > len(album['tracks']):
            return sub_error(fmt, 70, 'Song not found.')
        track = album['tracks'][idx - 1]
        resolved = resolve_track(slug, track['basename'])
        if not resolved:
            return sub_error(fmt, 70, 'Song not found.')
        files = resolved['files']
        want = (q.get('format') or '').lower()
        url = (files.get(want) or files.get((track.get('formats') or ['mp3'])[0])
               or files.get('mp3') or next(iter(files.values())))
        if not (PROXY_STREAM or q.get('proxy') == '1'):
            return RedirectResponse(url, status_code=302)
        suffix = (AUDIO_EXT_RE.search(urllib.parse.urlparse(url).path).group(1) or 'mp3').lower()
        upstream = sess.get(url, stream=True, timeout=60)

        def body():
            for chunk in upstream.iter_content(chunk_size=65536):
                yield chunk

        return StreamingResponse(body(), media_type=CONTENT_TYPES.get(suffix, 'audio/mpeg'),
                                 headers={'Content-Length': upstream.headers['Content-Length']}
                                 if upstream.headers.get('Content-Length') else None)

    if ep in ('scrobble', 'star', 'unstar', 'setRating', 'savePlayQueue'):
        return respond({}, fmt)

    if ep in ('getStarred', 'getStarred2'):
        key = 'starred' if ep == 'getStarred' else 'starred2'
        return respond({key: {'artist': [], 'album': [], 'song': []}}, fmt)

    if ep == 'getPlaylists':
        return respond({'playlists': {'playlist': []}}, fmt)

    if ep == 'getScanStatus':
        return respond({'scanStatus': {'scanning': False, 'count': len(ALBUMS)}}, fmt)

    return sub_error(fmt, 30, 'Endpoint %s is not implemented by this relay.' % ep)


@app.get('/')
def root():
    return {
        'server': SERVER_TYPE,
        'version': SERVER_VERSION,
        'apiVersion': API_VERSION,
        'albums': len(ALBUMS),
        'albumsWithMetadata': LIB_META_ALBUMS,
        'publishers': len(PUBLISHERS),
        'genres': len(GENRES),
        'artistMode': 'publisher' if USE_PUB_ARTISTS else 'letter',
        'genreSources': GENRE_SOURCES,
    }


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', '8080')))
