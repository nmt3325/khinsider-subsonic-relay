import gzip
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import types
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'songs.py'
DEFAULT_SONGS_URL = (
    'https://github.com/nmt3325/khinsider-index/releases/download/'
    'song-index/songs.tsv.gz')
DEFAULT_MANIFEST_URL = (
    'https://github.com/nmt3325/khinsider-index/releases/download/'
    'song-index/songs-index.json')
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


class FakeNet:
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


def manifest_bytes(tsv_text, gz_bytes=None, schema=1, sha256=None, content_sha256=None):
    raw = tsv_text.encode('utf-8')
    if gz_bytes is None:
        gz_bytes = gzip_bytes(tsv_text)
    return json.dumps({
        'schema_version': schema,
        'content_sha256': content_sha256 or hashlib.sha256(raw).hexdigest(),
        'sha256': sha256 or hashlib.sha256(gz_bytes).hexdigest(),
        'songs': tsv_text.count('\n'),
        'albums': 1,
        'bytes_raw': len(raw),
        'bytes_gzip': len(gz_bytes),
    }).encode('utf-8')


@pytest.fixture
def tsv_text():
    return 'demo\t1\t1\tOpening\ndemo\t2\t1\tOpening\n'


@pytest.fixture
def tsv_text_new():
    return 'demo\t1\t1\tOpening Again\ndemo\t2\t1\tOpening Again\n'


def load_module(monkeypatch, tmp_path, **env):
    global _LOAD_COUNT
    _LOAD_COUNT += 1
    monkeypatch.setenv('SONGS_DB', str(tmp_path / 'songs.sqlite'))
    monkeypatch.setenv('SONGS_REFRESH_HOURS', env.pop('SONGS_REFRESH_HOURS', '0'))
    monkeypatch.setenv('SONG_SEARCH', env.pop('SONG_SEARCH', 'auto'))
    for key in ('SONGS_URL', 'SONGS_MANIFEST_URL', 'SONGS_MAX_AGE_DAYS'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    name = f'tested_songs_{_LOAD_COUNT}'
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.pop('server', None)
    spec.loader.exec_module(mod)
    return mod


def read_meta(db_path):
    con = sqlite3.connect(db_path)
    try:
        return dict(con.execute('SELECT k, v FROM meta').fetchall())
    finally:
        con.close()


def install_net(monkeypatch, mod, steps):
    net = FakeNet(steps)
    monkeypatch.setattr(mod.urllib.request, 'urlopen', net)
    return net


def assert_conditional(headers, etag):
    assert headers.get('if-none-match') == etag
    assert 'if-modified-since' in headers


def assert_unconditional(headers):
    assert 'if-none-match' not in headers
    assert 'if-modified-since' not in headers


def build_good_db(monkeypatch, mod, tsv_text, *, manifest=True, gz_data=None,
                  etag='v1', last_modified='Mon, 01 Jan 2024 00:00:00 GMT'):
    if gz_data is None:
        gz_data = gzip_bytes(tsv_text)
    steps = []
    if manifest:
        assert mod.SONGS_MANIFEST_URL
        steps.append({'url': mod.SONGS_MANIFEST_URL,
                      'data': manifest_bytes(tsv_text, gz_data)})
    steps.append({'url': mod.SONGS_URL,
                  'data': gz_data,
                  'headers': {'ETag': etag, 'Last-Modified': last_modified}})
    net = install_net(monkeypatch, mod, steps)
    assert mod._ensure() is not None
    return net, read_meta(mod.SONGS_DB)


def missing_manifest_step(mod):
    return {'url': mod.SONGS_MANIFEST_URL, 'exc': http_error(mod.SONGS_MANIFEST_URL, 404)}


def invalid_manifest_step(mod):
    return {'url': mod.SONGS_MANIFEST_URL, 'data': b'not json'}


def wrong_schema_manifest_step(mod):
    return {'url': mod.SONGS_MANIFEST_URL, 'data': manifest_bytes('demo\t1\t1\tOpening\n', schema=2)}


def missing_hash_manifest_step(mod):
    return {'url': mod.SONGS_MANIFEST_URL,
            'data': json.dumps({'schema_version': 1}).encode('utf-8')}


@pytest.mark.parametrize(
    'manifest_step',
    [missing_manifest_step, invalid_manifest_step,
     wrong_schema_manifest_step, missing_hash_manifest_step],
)
def test_invalid_or_missing_manifest_falls_back_to_conditional_gzip(
        monkeypatch, tmp_path, tsv_text, manifest_step):
    mod = load_module(monkeypatch, tmp_path)
    build_good_db(monkeypatch, mod, tsv_text)
    net = install_net(monkeypatch, mod, [
        manifest_step(mod),
        {'url': mod.SONGS_URL,
         'exc': http_error(mod.SONGS_URL, 304),
         'assert_headers': lambda headers: assert_conditional(headers, 'v1')},
    ])
    assert mod.refresh() is False
    assert len(net.calls) == 2


def test_default_and_custom_manifest_selection(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)
    assert mod.SONGS_URL == DEFAULT_SONGS_URL
    assert mod.SONGS_MANIFEST_URL == DEFAULT_MANIFEST_URL

    mod = load_module(monkeypatch, tmp_path, SONGS_URL='https://example.test/songs.tsv.gz')
    assert mod.SONGS_MANIFEST_URL == ''

    mod = load_module(
        monkeypatch,
        tmp_path,
        SONGS_URL='https://example.test/songs.tsv.gz',
        SONGS_MANIFEST_URL='https://example.test/manifest.json',
    )
    assert mod.SONGS_MANIFEST_URL == 'https://example.test/manifest.json'


def test_first_build_then_manifest_skip_without_gzip(monkeypatch, tmp_path, tsv_text):
    mod = load_module(monkeypatch, tmp_path)
    net, meta = build_good_db(monkeypatch, mod, tsv_text)
    assert meta['content_digest'] == hashlib.sha256(tsv_text.encode('utf-8')).hexdigest()
    assert len(net.calls) == 2

    build_calls = []
    monkeypatch.setattr(mod, '_build', lambda *args, **kwargs: build_calls.append(args))
    net = install_net(monkeypatch, mod, [
        {'url': mod.SONGS_MANIFEST_URL,
         'data': manifest_bytes(tsv_text, gzip_bytes(tsv_text))},
    ])
    assert mod.refresh() is False
    assert len(net.calls) == 1
    assert build_calls == []


def test_same_content_different_gzip_mtime_skips_rebuild(monkeypatch, tmp_path, tsv_text):
    mod = load_module(monkeypatch, tmp_path, SONGS_MANIFEST_URL='')
    build_good_db(monkeypatch, mod, tsv_text, manifest=False, gz_data=gzip_bytes(tsv_text, mtime=0))
    before = read_meta(mod.SONGS_DB)

    build_calls = []
    monkeypatch.setattr(mod, '_build', lambda *args, **kwargs: build_calls.append(args))
    net = install_net(monkeypatch, mod, [
        {'url': mod.SONGS_URL,
         'data': gzip_bytes(tsv_text, mtime=123456789),
         'headers': {'ETag': 'v2', 'Last-Modified': 'Tue, 02 Jan 2024 00:00:00 GMT'},
         'assert_headers': lambda headers: assert_conditional(headers, 'v1')},
    ])
    assert mod.refresh() is False
    after = read_meta(mod.SONGS_DB)
    assert build_calls == []
    assert before['content_digest'] == after['content_digest']
    assert before['built'] == after['built']
    assert after['etag'] == 'v2'
    assert len(net.calls) == 1


def test_http_304_keeps_existing_db(monkeypatch, tmp_path, tsv_text):
    mod = load_module(monkeypatch, tmp_path, SONGS_MANIFEST_URL='')
    build_good_db(monkeypatch, mod, tsv_text, manifest=False)
    before = read_meta(mod.SONGS_DB)
    net = install_net(monkeypatch, mod, [
        {'url': mod.SONGS_URL,
         'exc': http_error(mod.SONGS_URL, 304),
         'assert_headers': lambda headers: assert_conditional(headers, 'v1')},
    ])
    assert mod.refresh() is False
    after = read_meta(mod.SONGS_DB)
    assert after['content_digest'] == before['content_digest']
    assert after['built'] == before['built']
    assert len(net.calls) == 1


@pytest.mark.parametrize(
    'case_name,payload,manifest_payload',
    [
        (
            'gzip_mismatch',
            lambda text: gzip_bytes(text, mtime=123),
            lambda text: manifest_bytes(text, gzip_bytes(text, mtime=0)),
        ),
        (
            'content_mismatch',
            lambda text: gzip_bytes(text + 'extra\t3\t1\tDifferent\n'),
            lambda text: manifest_bytes(text),
        ),
        (
            'corrupt_gzip',
            lambda text: b'not-a-gzip',
            lambda text: manifest_bytes(text),
        ),
        (
            'empty_tsv',
            lambda text: gzip_bytes(''),
            lambda text: manifest_bytes(text),
        ),
        (
            'mixed_bad_rows',
            lambda text: gzip_bytes('demo\t1\t1\tOpening Again\ndemo\tx\t2\tBroken\n'),
            lambda text: manifest_bytes(text),
        ),
    ],
)
def test_bad_downloads_keep_last_good_db(
        monkeypatch, tmp_path, tsv_text, tsv_text_new,
        case_name, payload, manifest_payload):
    mod = load_module(monkeypatch, tmp_path)
    build_good_db(monkeypatch, mod, tsv_text)
    before = read_meta(mod.SONGS_DB)
    target_text = tsv_text_new
    net = install_net(monkeypatch, mod, [
        {'url': mod.SONGS_MANIFEST_URL, 'data': manifest_payload(target_text)},
        {'url': mod.SONGS_URL,
         'data': payload(target_text),
         'headers': {'ETag': f'{case_name}-etag', 'Last-Modified': 'Wed, 03 Jan 2024 00:00:00 GMT'},
         'assert_headers': assert_unconditional},
    ])
    assert mod.refresh() is False
    after = read_meta(mod.SONGS_DB)
    assert after == before
    assert len(net.calls) == 2


def test_source_change_forces_download_instead_of_manifest_skip(monkeypatch, tmp_path, tsv_text):
    mod = load_module(monkeypatch, tmp_path)
    build_good_db(monkeypatch, mod, tsv_text)

    mod2 = load_module(
        monkeypatch,
        tmp_path,
        SONGS_URL='https://example.test/songs.tsv.gz',
        SONGS_MANIFEST_URL='https://example.test/songs-index.json',
    )
    net = install_net(monkeypatch, mod2, [
        {'url': mod2.SONGS_MANIFEST_URL, 'data': manifest_bytes(tsv_text, gzip_bytes(tsv_text))},
        {'url': mod2.SONGS_URL,
         'data': gzip_bytes(tsv_text),
         'headers': {'ETag': 'custom-v1', 'Last-Modified': 'Thu, 04 Jan 2024 00:00:00 GMT'},
         'assert_headers': assert_unconditional},
    ])
    assert mod2._ensure() is not None
    meta = read_meta(mod2.SONGS_DB)
    assert meta['source'] == 'https://example.test/songs.tsv.gz'
    assert meta['etag'] == 'custom-v1'
    assert len(net.calls) == 2


def test_schema_change_forces_download_instead_of_manifest_skip(monkeypatch, tmp_path, tsv_text):
    mod = load_module(monkeypatch, tmp_path)
    build_good_db(monkeypatch, mod, tsv_text)

    mod2 = load_module(monkeypatch, tmp_path)
    monkeypatch.setattr(mod2, 'SCHEMA', 2)
    net = install_net(monkeypatch, mod2, [
        {'url': mod2.SONGS_MANIFEST_URL, 'data': manifest_bytes(tsv_text, gzip_bytes(tsv_text))},
        {'url': mod2.SONGS_URL,
         'data': gzip_bytes(tsv_text),
         'headers': {'ETag': 'schema-v2', 'Last-Modified': 'Fri, 05 Jan 2024 00:00:00 GMT'},
         'assert_headers': assert_unconditional},
    ])
    assert mod2._ensure() is not None
    meta = read_meta(mod2.SONGS_DB)
    assert meta['schema'] == '2'
    assert meta['etag'] == 'schema-v2'
    assert len(net.calls) == 2


def test_legacy_db_without_content_or_source_meta_migrates_safely(monkeypatch, tmp_path, tsv_text):
    mod = load_module(monkeypatch, tmp_path)
    build_good_db(monkeypatch, mod, tsv_text)
    con = sqlite3.connect(mod.SONGS_DB)
    try:
        con.execute("DELETE FROM meta WHERE k IN ('source', 'content_digest')")
        con.commit()
    finally:
        con.close()

    net = install_net(monkeypatch, mod, [
        {'url': mod.SONGS_MANIFEST_URL, 'data': manifest_bytes(tsv_text, gzip_bytes(tsv_text))},
        {'url': mod.SONGS_URL,
         'exc': http_error(mod.SONGS_URL, 304),
         'assert_headers': assert_unconditional},
    ])
    assert mod.refresh() is False
    meta = read_meta(mod.SONGS_DB)
    assert 'content_digest' not in meta
    assert len(net.calls) == 2


def test_missing_db_rebuilds_without_conditionals(monkeypatch, tmp_path, tsv_text):
    mod = load_module(monkeypatch, tmp_path)
    build_good_db(monkeypatch, mod, tsv_text)
    Path(mod.SONGS_DB).unlink()

    mod2 = load_module(monkeypatch, tmp_path)
    net = install_net(monkeypatch, mod2, [
        {'url': mod2.SONGS_MANIFEST_URL, 'data': manifest_bytes(tsv_text, gzip_bytes(tsv_text))},
        {'url': mod2.SONGS_URL,
         'data': gzip_bytes(tsv_text),
         'headers': {'ETag': 'rebuilt', 'Last-Modified': 'Sat, 06 Jan 2024 00:00:00 GMT'},
         'assert_headers': assert_unconditional},
    ])
    assert mod2._ensure() is not None
    meta = read_meta(mod2.SONGS_DB)
    assert meta['etag'] == 'rebuilt'
    assert len(net.calls) == 2


def test_search_resolves_disc_specific_track_numbers(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path, SONG_SEARCH='auto')
    monkeypatch.setattr(
        mod,
        'candidates',
        lambda query: [('album-slug', 1, 1, 'Opening'), ('album-slug', 2, 1, 'Opening')],
    )
    album = {
        'title': 'Album',
        'tracks': [
            {'title': 'Opening', 'num': 1, 'disc': 1},
            {'title': 'Opening', 'num': 1, 'disc': 2},
        ],
    }
    server = types.SimpleNamespace(
        load_album=lambda slug: album,
        album_meta=lambda slug, loaded: {'title': 'Album'},
        song_child=lambda slug, album_title, meta, track, idx: {
            'id': f'track/{slug}/{idx}',
            'disc': track['disc'],
            'track': track['num'],
        },
    )
    sys.modules['server'] = server
    try:
        songs = mod.search('Opening', count=10, offset=0)
    finally:
        sys.modules.pop('server', None)
    assert [song['id'] for song in songs] == ['track/album-slug/1', 'track/album-slug/2']


@pytest.mark.parametrize('invalid', [
    {'complete': False}, {'legacy_inputs': ['songs_cached.jsonl.gz']},
    {'dataset_schema_version': 1}, {'schema_version': 2}, {'sha256': ''},
])
def test_declared_live_manifest_cannot_fall_back_to_unverified_gzip(monkeypatch, tmp_path, invalid):
    mod = load_module(monkeypatch, tmp_path)
    text = 'demo\t1\t1\tOpening\n'
    build_good_db(monkeypatch, mod, text)
    before = Path(mod.SONGS_DB).read_bytes()
    value = json.loads(manifest_bytes(text))
    value.update(data_source='khinsider-live-v2', dataset_schema_version=2,
                 complete=True, legacy_inputs=[])
    value.update(invalid)
    net = install_net(monkeypatch, mod, [{'url': mod.SONGS_MANIFEST_URL,
                                        'data': json.dumps(value).encode()}])
    assert mod._sync(force=True) is False
    assert len(net.calls) == 1 and not net.steps
    assert Path(mod.SONGS_DB).read_bytes() == before


def test_complete_live_manifest_builds_a_new_database(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)
    text = 'caf%C3%A9\t1\t1\tCafé opening\n'
    compressed = gzip_bytes(text)
    value = json.loads(manifest_bytes(text, compressed))
    value.update(data_source='khinsider-live-v2', dataset_schema_version=2,
                 complete=True, legacy_inputs=[])
    net = install_net(monkeypatch, mod, [
        {'url': mod.SONGS_MANIFEST_URL, 'data': json.dumps(value).encode()},
        {'url': mod.SONGS_URL, 'data': compressed},
    ])
    assert mod._sync(force=True) is True
    assert not net.steps
    assert mod.candidates('opening')[0][0] == 'caf%C3%A9'
