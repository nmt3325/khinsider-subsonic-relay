import gzip
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SONGS_PATH = ROOT / 'songs.py'
SERVER_PATH = ROOT / 'server.py'
DEFAULT_LIBRARY_URL = (
    'https://github.com/nmt3325/khinsider-index/releases/latest/download/'
    'library.json')

_LOAD_COUNT = 0


class FakeResponse:
    def __init__(self, data=b'', headers=None):
        self._data = data
        self.headers = headers or {}

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._data)
        chunk = self._data[:size]
        self._data = self._data[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RequestRecorder:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, req, timeout=0):
        url = req.full_url
        headers = {k.lower(): v for k, v in req.header_items()}
        self.calls.append((url, headers))
        assert self.steps, f'unexpected request for {url}'
        step = self.steps.pop(0)
        assert url == step['url']
        if 'assert_headers' in step:
            step['assert_headers'](headers)
        if 'exc' in step:
            raise step['exc']
        return FakeResponse(step.get('data', b''), step.get('headers'))


def http_error(url, code):
    return urllib.error.HTTPError(url, code, f'HTTP {code}', hdrs={}, fp=io.BytesIO())


def gzip_bytes(text, mtime=0):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', mtime=mtime, filename='') as gz:
        gz.write(text.encode('utf-8'))
    return buf.getvalue()


def manifest_bytes(tsv_text, gz_bytes=None, schema=1):
    raw = tsv_text.encode('utf-8')
    if gz_bytes is None:
        gz_bytes = gzip_bytes(tsv_text)
    return json.dumps({
        'schema_version': schema,
        'content_sha256': hashlib.sha256(raw).hexdigest(),
        'sha256': hashlib.sha256(gz_bytes).hexdigest(),
        'songs': tsv_text.count('\n'),
        'albums': 1,
        'bytes_raw': len(raw),
        'bytes_gzip': len(gz_bytes),
    }).encode('utf-8')


def assert_unconditional(headers):
    assert 'if-none-match' not in headers
    assert 'if-modified-since' not in headers


def assert_conditional(headers, etag='v1'):
    assert headers.get('if-none-match') == etag
    assert 'if-modified-since' in headers


@pytest.fixture
def songs_tsv():
    return 'oldslug\t1\t1\tOpening Old\n'


@pytest.fixture
def songs_tsv_new():
    return 'newslug\t1\t1\tOpening New\n'


def load_songs(monkeypatch, tmp_path, **env):
    global _LOAD_COUNT
    _LOAD_COUNT += 1
    monkeypatch.setenv('SONGS_DB', str(tmp_path / 'songs.sqlite'))
    monkeypatch.setenv('SONGS_REFRESH_HOURS', env.pop('SONGS_REFRESH_HOURS', '0'))
    monkeypatch.setenv('SONG_SEARCH', env.pop('SONG_SEARCH', 'auto'))
    for key in ('SONGS_URL', 'SONGS_MANIFEST_URL', 'SONGS_MAX_AGE_DAYS'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    name = f'test_source_binding_songs_{_LOAD_COUNT}'
    spec = importlib.util.spec_from_file_location(name, SONGS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_song_meta(db_path):
    con = sqlite3.connect(db_path)
    try:
        return dict(con.execute('SELECT k, v FROM meta').fetchall())
    finally:
        con.close()


def build_song_db(monkeypatch, mod, tsv_text, *, source_headers=None):
    if source_headers is None:
        source_headers = {'ETag': 'v1', 'Last-Modified': 'Mon, 01 Jan 2024 00:00:00 GMT'}
    net = RequestRecorder([
        {'url': mod.SONGS_MANIFEST_URL, 'data': manifest_bytes(tsv_text)},
        {'url': mod.SONGS_URL, 'data': gzip_bytes(tsv_text), 'headers': source_headers},
    ])
    monkeypatch.setattr(mod.urllib.request, 'urlopen', net)
    assert mod._ensure() is not None
    return net


def load_server(monkeypatch, tmp_path, *, library, library_url=DEFAULT_LIBRARY_URL,
                http_meta=None, urlopen=None):
    library_path = tmp_path / 'library.json'
    library_path.write_text(json.dumps(library), encoding='utf-8')
    if http_meta is not None:
        (tmp_path / 'library.json.http.json').write_text(
            json.dumps(http_meta), encoding='utf-8')
    monkeypatch.setenv('CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setenv('LIBRARY_PATH', str(library_path))
    monkeypatch.setenv('LIBRARY_URL', library_url)
    monkeypatch.setenv('LIBRARY_REFRESH_HOURS', '0')
    monkeypatch.setenv('LIBRARY_MAX_AGE_HOURS', '0')
    monkeypatch.setenv('PROXY_STREAM', '0')
    monkeypatch.setenv('SUBSONIC_USER', 'admin')
    monkeypatch.setenv('SUBSONIC_PASSWORD', 'admin')
    if urlopen is not None:
        monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
    stub = types.SimpleNamespace(
        start=lambda: None,
        status=lambda: {'state': 'stub'},
        search=lambda *args, **kwargs: [],
    )
    old_songs = sys.modules.get('songs')
    sys.modules['songs'] = stub
    try:
        name = f'test_source_binding_server_{hash((str(tmp_path), library_url, json.dumps(library, sort_keys=True)))}'
        spec = importlib.util.spec_from_file_location(name, SERVER_PATH)
        mod = importlib.util.module_from_spec(spec)
        root_str = str(ROOT)
        inserted = False
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
            inserted = True
        try:
            spec.loader.exec_module(mod)
        finally:
            if inserted:
                sys.path.remove(root_str)
        return mod
    finally:
        if old_songs is None:
            sys.modules.pop('songs', None)
        else:
            sys.modules['songs'] = old_songs


@pytest.mark.parametrize('runtime_change', ['source', 'schema'])
def test_song_startup_does_not_publish_foreign_db_when_refresh_fails(
        monkeypatch, tmp_path, songs_tsv, runtime_change):
    mod = load_songs(monkeypatch, tmp_path)
    build_song_db(monkeypatch, mod, songs_tsv)

    if runtime_change == 'source':
        mod2 = load_songs(
            monkeypatch,
            tmp_path,
            SONGS_URL='https://example.test/new.tsv.gz',
            SONGS_MANIFEST_URL='https://example.test/new.json',
        )
    else:
        mod2 = load_songs(monkeypatch, tmp_path)
        monkeypatch.setattr(mod2, 'SCHEMA', 2)
    net = RequestRecorder([
        {'url': mod2.SONGS_MANIFEST_URL, 'data': manifest_bytes(songs_tsv)},
        {'url': mod2.SONGS_URL,
         'exc': urllib.error.URLError('offline'),
         'assert_headers': assert_unconditional},
        {'url': mod2.SONGS_MANIFEST_URL, 'data': manifest_bytes(songs_tsv)},
        {'url': mod2.SONGS_URL,
         'exc': urllib.error.URLError('offline'),
         'assert_headers': assert_unconditional},
    ])
    monkeypatch.setattr(mod2.urllib.request, 'urlopen', net)

    assert mod2._ensure() is None
    assert mod2.candidates('Opening') == []
    meta = read_song_meta(mod2.SONGS_DB)
    assert meta['source'] != mod2.SONGS_URL or meta['schema'] != str(mod2.SCHEMA)
    assert mod2.status()['url'] == mod2.SONGS_URL
    assert 'offline' in (mod2.status()['error'] or '')
    assert len(net.calls) == 4


def test_song_refresh_keeps_last_good_same_source_on_failure(monkeypatch, tmp_path, songs_tsv):
    mod = load_songs(monkeypatch, tmp_path)
    build_song_db(monkeypatch, mod, songs_tsv)
    assert mod.candidates('Opening') == [('oldslug', 1, 1, 'Opening Old')]

    net = RequestRecorder([
        {'url': mod.SONGS_MANIFEST_URL, 'data': manifest_bytes('newslug\t1\t1\tOpening New\n')},
        {'url': mod.SONGS_URL,
         'exc': urllib.error.URLError('same-source-down'),
         'assert_headers': assert_conditional},
    ])
    monkeypatch.setattr(mod.urllib.request, 'urlopen', net)

    assert mod.refresh() is False
    assert mod.candidates('Opening') == [('oldslug', 1, 1, 'Opening Old')]
    assert 'same-source-down' in (mod.status()['error'] or '')


def test_library_source_change_triggers_unconditional_get_at_startup(monkeypatch, tmp_path):
    old_library = {'albums': [{'slug': 'oldslug', 'title': 'Old Album'}]}
    new_library = {'albums': [{'slug': 'newslug', 'title': 'New Album'}]}
    new_url = 'https://example.test/new-library.json'
    net = RequestRecorder([
        {'url': new_url,
         'data': json.dumps(new_library).encode('utf-8'),
         'headers': {'ETag': 'new-etag', 'Last-Modified': 'Tue, 02 Jan 2024 00:00:00 GMT'},
         'assert_headers': assert_unconditional},
    ])
    mod = load_server(
        monkeypatch,
        tmp_path,
        library=old_library,
        library_url=new_url,
        http_meta={
            'source': DEFAULT_LIBRARY_URL,
            'digest': hashlib.sha256(json.dumps(old_library).encode('utf-8')).hexdigest(),
            'etag': 'old-etag',
            'last_modified': 'Mon, 01 Jan 2024 00:00:00 GMT',
        },
        urlopen=net,
    )

    assert 'newslug' in mod.ALBUMS
    assert 'oldslug' not in mod.ALBUMS
    http_meta = json.loads((tmp_path / 'library.json.http.json').read_text(encoding='utf-8'))
    assert http_meta['source'] == new_url
    assert len(net.calls) == 1


def test_library_source_change_failure_keeps_last_good_and_old_binding(monkeypatch, tmp_path):
    old_library = {'albums': [{'slug': 'oldslug', 'title': 'Old Album'}]}
    new_url = 'https://example.test/new-library.json'
    original_meta = {
        'source': DEFAULT_LIBRARY_URL,
        'digest': hashlib.sha256(json.dumps(old_library).encode('utf-8')).hexdigest(),
        'etag': 'old-etag',
        'last_modified': 'Mon, 01 Jan 2024 00:00:00 GMT',
    }
    net = RequestRecorder([
        {'url': new_url, 'exc': urllib.error.URLError('library offline'), 'assert_headers': assert_unconditional},
    ])
    mod = load_server(
        monkeypatch,
        tmp_path,
        library=old_library,
        library_url=new_url,
        http_meta=original_meta,
        urlopen=net,
    )

    assert 'oldslug' in mod.ALBUMS
    assert mod.LIBRARY_URL == new_url
    current_meta = json.loads((tmp_path / 'library.json.http.json').read_text(encoding='utf-8'))
    assert current_meta == original_meta
    assert len(net.calls) == 1


def test_library_source_change_rejects_foreign_304(monkeypatch, tmp_path):
    old_library = {'albums': [{'slug': 'oldslug', 'title': 'Old Album'}]}
    new_url = 'https://example.test/new-library.json'
    original_meta = {
        'source': DEFAULT_LIBRARY_URL,
        'digest': hashlib.sha256(json.dumps(old_library).encode('utf-8')).hexdigest(),
        'etag': 'old-etag',
        'last_modified': 'Mon, 01 Jan 2024 00:00:00 GMT',
    }
    net = RequestRecorder([
        {'url': new_url, 'exc': http_error(new_url, 304), 'assert_headers': assert_unconditional},
    ])
    mod = load_server(
        monkeypatch,
        tmp_path,
        library=old_library,
        library_url=new_url,
        http_meta=original_meta,
        urlopen=net,
    )

    assert 'oldslug' in mod.ALBUMS
    current_meta = json.loads((tmp_path / 'library.json.http.json').read_text(encoding='utf-8'))
    assert current_meta == original_meta
    assert len(net.calls) == 1
