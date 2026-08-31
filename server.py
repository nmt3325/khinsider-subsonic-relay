#!/usr/bin/env python3
"""
khinsider-subsonic-relay: a Subsonic API-compatible relay server for
downloads.khinsider.com.

No audio files are hosted. Album/track pages are fetched on demand from the
live site (Cloudflare bypass via curl_cffi TLS impersonation) and cached.
/rest/stream resolves the track's real CDN URL and returns a 302 redirect,
so clients download directly from khinsider's CDN (vgmtreasurechest.com).
Set PROXY_STREAM=1 or pass &proxy=1 to stream bytes through this server.

Config (env):
  SUBSONIC_USER / SUBSONIC_PASSWORD  - login (default: admin / admin)
  PORT                               - listen port (default: 8080)
  LIBRARY_PATH                       - library.json (auto-downloaded if missing)
  LIBRARY_URL                        - override download URL for library.json
  CACHE_DIR                          - page cache dir (default: ./cache)
  PROXY_STREAM                       - '1' to always proxy instead of 302
"""
import hashlib
import html as htmllib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from curl_cffi import requests as creq
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

BASE = 'https://downloads.khinsider.com'
API_VERSION = '1.16.1'
SERVER_TYPE = 'khinsider-relay'
USERNAME = os.environ.get('SUBSONIC_USER', 'admin')
PASSWORD = os.environ.get('SUBSONIC_PASSWORD', 'admin')
CACHE_DIR = os.environ.get('CACHE_DIR', './cache')
LIBRARY_PATH = os.environ.get('LIBRARY_PATH', './library.json')
LIBRARY_URL = os.environ.get(
    'LIBRARY_URL',
    'https://github.com/nmt3325/khinsider-index/releases/download/v2026.09.01/library.json',
)
PROXY_STREAM = os.environ.get('PROXY_STREAM', '') == '1'

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(os.path.join(CACHE_DIR, 'albums'), exist_ok=True)
os.makedirs(os.path.join(CACHE_DIR, 'tracks'), exist_ok=True)

sess = creq.Session(impersonate='chrome')

# ---------------- library ----------------

def ensure_library():
    if not os.path.exists(LIBRARY_PATH):
        print(f'downloading library from {LIBRARY_URL} ...')
        urllib.request.urlretrieve(LIBRARY_URL, LIBRARY_PATH)
    with open(LIBRARY_PATH) as f:
        data = json.load(f)
    return data

_lib = ensure_library()
ALBUMS = {}          # slug -> {'title', 'letter'}
LETTER_ALBUMS = {}   # letter -> [(slug, title)] sorted
SEARCH = []          # (lower_title, slug)
for a in _lib['albums']:
    ALBUMS[a['slug']] = {'title': a['title'], 'letter': a['letter']}
    LETTER_ALBUMS.setdefault(a['letter'], []).append((a['slug'], a['title']))
    SEARCH.append((a['title'].lower(), a['slug']))
for L in LETTER_ALBUMS:
    LETTER_ALBUMS[L].sort(key=lambda x: x[1].lower())
LETTERS = ['0-9'] + [chr(c) for c in range(ord('A'), ord('Z') + 1)]
LETTERS = [L for L in LETTERS if L in LETTER_ALBUMS]
print(f'library loaded: {len(ALBUMS)} albums in {len(LETTERS)} sections')

# ---------------- subsonic plumbing ----------------

app = FastAPI(docs_url=None, redoc_url=None)


def check_auth(q):
    if not PASSWORD:
        return True
    u = q.get('u', '')
    if u != USERNAME:
        return False
    t, s = q.get('t'), q.get('s')
    if t and s:
        return hashlib.md5((PASSWORD + s).encode()).hexdigest() == t.lower()
    p = q.get('p', '')
    if p.startswith('enc:'):
        try:
            p = bytes.fromhex(p[4:]).decode()
        except Exception:
            return False
    return p == PASSWORD


def _fill(el, d):
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, dict):
            child = ET.SubElement(el, k)
            _fill(child, v)
        elif isinstance(v, list):
            for item in v:
                child = ET.SubElement(el, k)
                _fill(child, item)
        elif isinstance(v, bool):
            el.set(k, 'true' if v else 'false')
        else:
            el.set(k, str(v))


def respond(payload, fmt, status='ok', error=None):
    body = {'status': status, 'version': API_VERSION}
    if error:
        body['error'] = error
    else:
        body.update(payload)
    if fmt == 'json':
        return JSONResponse({'subsonic-response': body})
    root = ET.Element('subsonic-response', {
        'xmlns': 'http://subsonic.org/restapi',
        'status': body['status'],
        'version': body['version'],
        'type': SERVER_TYPE,
        'serverVersion': '0.1.0',
    })
    for k, v in body.items():
        if k in ('status', 'version'):
            continue
        child = ET.SubElement(root, k)
        _fill(child, v)
    return Response(ET.tostring(root, encoding='unicode'), media_type='application/xml')


def sub_error(fmt, code, msg):
    return respond({}, fmt, status='failed', error={'code': code, 'message': msg})

# ---------------- khinsider fetching/parsing ----------------


def _cache_path(kind, key):
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', key)
    return os.path.join(CACHE_DIR, kind, safe + '.json')


def _cache_get(kind, key, max_age):
    p = _cache_path(kind, key)
    if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < max_age:
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _cache_put(kind, key, data):
    with open(_cache_path(kind, key), 'w') as f:
        json.dump(data, f, ensure_ascii=False)


def _human2bytes(s):
    m = re.match(r'([\d.]+)\s*(KB|MB|GB|B)', s.strip(), re.I)
    if not m:
        return None
    n = float(m.group(1))
    mult = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}[m.group(2).upper()]
    return int(n * mult)


def _duration(s):
    m = re.match(r'^(\d+):(\d\d)(?::(\d\d))?$', s.strip())
    if not m:
        return None
    if m.group(3) is not None:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return int(m.group(1)) * 60 + int(m.group(2))


def load_album(slug):
    cached = _cache_get('albums', slug, max_age=30 * 86400)
    if cached:
        return cached
    r = sess.get(f'{BASE}/game-soundtracks/album/{slug}', timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f'album page HTTP {r.status_code}')
    soup = BeautifulSoup(r.text, 'html.parser')
    title_el = soup.select_one('#pageContent h2')
    title = title_el.get_text(strip=True) if title_el else slug
    cover = None
    img_div = soup.find('div', class_='albumImage')
    if img_div:
        a = img_div.find('a')
        if a and a.get('href'):
            cover = urllib.parse.urljoin(BASE, a['href'])
    tracks = []
    table = soup.find('table', id='songlist')
    if table:
        for row in table.find_all('tr')[1:]:
            link = None
            for a in row.find_all('a'):
                href = a.get('href', '')
                if re.search(r'\.(mp3|flac|ogg|m4a|opus|wma|wav)$', href, re.I):
                    link = a
                    break
            if link is None:
                continue
            href = link['href']
            basename = href.rsplit('/', 1)[-1]
            cells = [td.get_text(strip=True) for td in row.find_all('td')]
            num = None
            dur = None
            sizes = []
            for c in cells:
                if num is None and re.match(r'^\d+\.$', c):
                    num = int(c[:-1])
                elif dur is None and re.match(r'^\d+:\d\d(?::\d\d)?$', c):
                    dur = _duration(c)
                elif re.match(r'^[\d.]+\s*(KB|MB|GB)$', c, re.I):
                    sizes.append(_human2bytes(c))
            tracks.append({
                'num': num,
                'title': link.get_text(strip=True),
                'duration': dur,
                'size': sizes[0] if sizes else None,
                'basename': basename,
            })
    album = {'slug': slug, 'title': title, 'cover': cover, 'tracks': tracks}
    _cache_put('albums', slug, album)
    return album


def resolve_track(slug, basename):
    key = f'{slug}__{basename}'
    cached = _cache_get('tracks', key, max_age=30 * 86400)
    if cached:
        return cached
    r = sess.get(f'{BASE}/game-soundtracks/album/{slug}/{basename}', timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f'track page HTTP {r.status_code}')
    soup = BeautifulSoup(r.text, 'html.parser')
    files = {}
    for span in soup.find_all('span', class_='songDownloadLink'):
        p = span.parent
        if p and p.name == 'a' and p.get('href'):
            url = p['href']
            if url.startswith('//'):
                url = 'https:' + url
            ext = url.rsplit('.', 1)[-1].lower()
            files[ext] = url
    if not files:
        raise RuntimeError('no download links on track page')
    _cache_put('tracks', key, files)
    return files

# ---------------- model helpers ----------------


def album_child(slug, parent=None):
    meta = ALBUMS.get(slug, {'title': slug, 'letter': None})
    return {
        'id': f'album/{slug}',
        'title': meta['title'],
        'name': meta['title'],
        'isDir': True,
        'parent': parent,
        'coverArt': f'album/{slug}',
    }


def song_child(slug, album_title, t, idx):
    tid = f"track/{slug}/{idx}"
    return {
        'id': tid,
        'title': t['title'],
        'album': album_title,
        'artist': 'KHInsider',
        'track': t['num'],
        'duration': t['duration'],
        'size': t['size'],
        'suffix': 'mp3',
        'contentType': 'audio/mpeg',
        'isDir': False,
        'parent': f'album/{slug}',
        'coverArt': f'album/{slug}',
        'type': 'music',
    }

# ---------------- API ----------------


@app.api_route('/rest/{endpoint}', methods=['GET', 'POST'])
async def subsonic(endpoint: str, request: Request):
    q = dict(request.query_params)
    if request.method == 'POST':
        form = await request.form()
        q.update({k: str(v) for k, v in form.items()})
    fmt = q.get('f', 'xml')
    ep = endpoint.replace('.view', '')

    if not check_auth(q):
        return sub_error(fmt, 40, 'Wrong username or password')

    try:
        if ep in ('ping', 'ping.view'):
            return respond({}, fmt)
        if ep == 'getLicense':
            return respond({'license': {'valid': True}}, fmt)
        if ep == 'getUser':
            return respond({'user': {'username': USERNAME, 'folder': [1], 'scrobblingEnabled': False, 'adminRole': True, 'downloadRole': True, 'streamRole': True}}, fmt)
        if ep == 'getMusicFolders':
            return respond({'musicFolders': {'musicFolder': [{'id': 1, 'name': 'KHInsider'}]}}, fmt)

        if ep == 'getIndexes':
            idx = [{'name': L, 'artist': [{'id': f'letter/{L}', 'name': L}]} for L in LETTERS]
            return respond({'indexes': {'lastModified': int(time.time() * 1000), 'index': idx}}, fmt)

        if ep == 'getArtists':
            idx = [{'name': L, 'artist': [{'id': f'letter/{L}', 'name': L, 'albumCount': len(LETTER_ALBUMS[L])}]} for L in LETTERS]
            return respond({'artists': {'index': idx}}, fmt)

        if ep == 'getArtist':
            aid = q.get('id', '')
            if aid.startswith('letter/'):
                L = aid.split('/', 1)[1]
                albums = [{'id': f'album/{s}', 'name': t, 'artist': L, 'coverArt': f'album/{s}'} for s, t in LETTER_ALBUMS.get(L, [])]
                return respond({'artist': {'id': aid, 'name': L, 'albumCount': len(albums), 'album': albums}}, fmt)
            return sub_error(fmt, 70, 'unknown artist')

        if ep == 'getMusicDirectory':
            did = q.get('id', '')
            if did.startswith('letter/'):
                L = did.split('/', 1)[1]
                children = [album_child(s, parent=did) for s, t in LETTER_ALBUMS.get(L, [])]
                return respond({'directory': {'id': did, 'name': L, 'child': children}}, fmt)
            if did.startswith('album/'):
                slug = did.split('/', 1)[1]
                album = load_album(slug)
                children = [song_child(slug, album['title'], t, i) for i, t in enumerate(album['tracks'])]
                return respond({'directory': {'id': did, 'name': album['title'], 'child': children}}, fmt)
            return sub_error(fmt, 70, 'unknown directory')

        if ep == 'getAlbum':
            aid = q.get('id', '')
            if not aid.startswith('album/'):
                return sub_error(fmt, 70, 'unknown album')
            slug = aid.split('/', 1)[1]
            album = load_album(slug)
            songs = [song_child(slug, album['title'], t, i) for i, t in enumerate(album['tracks'])]
            total = sum(t['duration'] or 0 for t in album['tracks'])
            return respond({'album': {
                'id': aid, 'name': album['title'], 'artist': 'KHInsider',
                'coverArt': aid, 'songCount': len(songs), 'duration': total,
                'song': songs,
            }}, fmt)

        if ep in ('getAlbumList', 'getAlbumList2'):
            size = int(q.get('size', 10))
            offset = int(q.get('offset', 0))
            all_slugs = sorted(ALBUMS.keys(), key=lambda s: ALBUMS[s]['title'].lower())
            page = all_slugs[offset:offset + size]
            key = 'albumList' if ep == 'getAlbumList' else 'albumList2'
            albums = [{'id': f'album/{s}', 'name': ALBUMS[s]['title'], 'artist': 'KHInsider', 'coverArt': f'album/{s}'} for s in page]
            return respond({key: {'album': albums}}, fmt)

        if ep == 'search3' or ep == 'search2':
            query = (q.get('query') or '').strip().lower()
            album_count = int(q.get('albumCount', 20))
            song_count = int(q.get('songCount', 20))
            hits_a, hits_s = [], []
            if query:
                for lt, slug in SEARCH:
                    if query in lt:
                        hits_a.append({'id': f'album/{slug}', 'name': ALBUMS[slug]['title'], 'artist': 'KHInsider', 'coverArt': f'album/{slug}'})
                        if len(hits_a) >= album_count:
                            break
                # song search only inside matched album titles is skipped (tracks need live pages)
            key = 'searchResult3' if ep == 'search3' else 'searchResult2'
            return respond({key: {'album': hits_a, 'song': hits_s, 'artist': []}}, fmt)

        if ep == 'getSong':
            sid = q.get('id', '')
            if sid.startswith('track/'):
                _, slug, idx = sid.split('/', 2)
                album = load_album(slug)
                i = int(idx)
                return respond({'song': song_child(slug, album['title'], album['tracks'][i], i)}, fmt)
            return sub_error(fmt, 70, 'song not found')

        if ep == 'getCoverArt':
            cid = q.get('id', '')
            if cid.startswith('album/'):
                slug = cid.split('/', 1)[1]
                album = load_album(slug)
                if album.get('cover'):
                    return RedirectResponse(album['cover'], status_code=302)
            return sub_error(fmt, 70, 'no cover art')

        if ep in ('stream', 'download'):
            sid = q.get('id', '')
            if not sid.startswith('track/'):
                return sub_error(fmt, 70, 'unknown song id')
            _, slug, idx = sid.split('/', 2)
            album = load_album(slug)
            files = resolve_track(slug, album['tracks'][int(idx)]['basename'])
            want = q.get('format', 'mp3')
            url = files.get(want) or files.get('mp3') or next(iter(files.values()))
            if not PROXY_STREAM and q.get('proxy') != '1':
                return RedirectResponse(url, status_code=302)
            # proxy mode: stream bytes through, with Range passthrough
            upstream_headers = {}
            if 'range' in request.headers:
                upstream_headers['range'] = request.headers['range']
            r = sess.get(url, timeout=60, stream=True, headers=upstream_headers)
            out_headers = {}
            for h in ('content-type', 'content-length', 'content-range', 'accept-ranges'):
                if h in r.headers:
                    out_headers[h] = r.headers[h]
            return StreamingResponse(r.iter_content(65536), status_code=r.status_code, headers=out_headers)

        if ep == 'scrobble':
            return respond({}, fmt)
        if ep in ('star', 'unstar'):
            return respond({}, fmt)
        if ep in ('getStarred', 'getStarred2'):
            return respond({'starred2' if ep == 'getStarred2' else 'starred': {}}, fmt)
        if ep == 'getGenres':
            return respond({'genres': {'genre': [{'value': 'Video Game Music', 'songCount': 0, 'albumCount': len(ALBUMS)}]}}, fmt)

        return sub_error(fmt, 70, f'endpoint not implemented: {ep}')
    except Exception as e:
        return sub_error(fmt, 70, f'{type(e).__name__}: {e}')


@app.get('/')
def root():
    return {'name': SERVER_TYPE, 'albums': len(ALBUMS), 'subsonic': '/rest/ping.view'}
