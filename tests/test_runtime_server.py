import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
import urllib.parse
import uuid
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
VA_HTML = (FIXTURES / 'va-2024.html').read_text(encoding='utf-8')
DEFAULT_LIBRARY_URL = (
    'https://github.com/nmt3325/khinsider-index/releases/latest/download/'
    'library.json')


class DummyResponse:
    def __init__(self, *, text='', status_code=200, headers=None, content=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content if content is not None else text.encode('utf-8')

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


class DownloadResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}
        self.offset = 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f'unexpected GET {url}')
        route = self.routes[url]
        if callable(route):
            route = route(url=url, kwargs=kwargs)
        elif isinstance(route, list):
            if not route:
                raise AssertionError(f'no more responses for {url}')
            route = route.pop(0)
        if isinstance(route, Exception):
            raise route
        return route


def player_markup(songid, file_url):
    rows = json.dumps([{'songid': songid, 'file': file_url}], ensure_ascii=False)
    return f'<script>var mediaPath="https://",extension="",tracks={rows};</script>'


class RuntimeServerTests(unittest.TestCase):
    def load_server(self, library=None):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        cache_dir = Path(tempdir.name) / 'cache'
        library_path = Path(tempdir.name) / 'library.json'
        payload = library if library is not None else {'albums': []}
        library_path.write_text(json.dumps(payload), encoding='utf-8')
        http_meta_path = Path(str(library_path) + '.http.json')
        http_meta_path.write_text(json.dumps({
            'source': DEFAULT_LIBRARY_URL,
            'digest': hashlib.sha256(library_path.read_bytes()).hexdigest(),
            'etag': 'seed',
            'last_modified': 'seed',
        }), encoding='utf-8')
        os.environ['CACHE_DIR'] = str(cache_dir)
        os.environ['LIBRARY_PATH'] = str(library_path)
        os.environ['LIBRARY_URL'] = DEFAULT_LIBRARY_URL
        os.environ['LIBRARY_REFRESH_HOURS'] = '0'
        os.environ['LIBRARY_MAX_AGE_HOURS'] = '0'
        os.environ['PROXY_STREAM'] = '0'
        os.environ['SUBSONIC_USER'] = 'admin'
        os.environ['SUBSONIC_PASSWORD'] = 'admin'
        stub = types.SimpleNamespace(
            start=lambda: None,
            status=lambda: {'state': 'stub'},
            search=lambda *args, **kwargs: [],
        )
        old_songs = sys.modules.get('songs')
        sys.modules['songs'] = stub
        def cleanup_songs():
            if old_songs is None:
                sys.modules.pop('songs', None)
            else:
                sys.modules['songs'] = old_songs
        self.addCleanup(cleanup_songs)
        module_name = f'tested_server_{uuid.uuid4().hex}'
        spec = importlib.util.spec_from_file_location(module_name, ROOT / 'server.py')
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        root_str = str(ROOT)
        inserted = False
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
            inserted = True
        try:
            spec.loader.exec_module(module)
        finally:
            if inserted:
                sys.path.remove(root_str)
        return module

    def album_html(self, slug='demo', basename='01. Track.mp3', title='Track', songid='1', formats=('mp3',), script=''):
        headers = ''.join(f'<th>{fmt.upper()}</th>' for fmt in formats)
        sizes = ''.join('<td>1 MB</td>' for _ in formats)
        href = urllib.parse.quote(basename)
        return f'''<html><div id="pageContent"><h2>Demo Album</h2></div>{script}
<table id="songlist">
<tr id="songlist_header"><th>#</th><th>Song Name</th>{headers}</tr>
<tr>
  <td>1</td>
  <td><a href="/game-soundtracks/album/{slug}/{href}">{title}</a><div class="playlistAddTo" songid="{songid}"></div></td>
  <td>0:30</td>{sizes}
</tr>
</table></html>'''

    def song_page_html(self, files):
        parts = []
        for ext, url in files.items():
            parts.append(f'<a href="{url}"><span class="songDownloadLink">{ext}</span></a>')
        return '<html>' + ''.join(parts) + '</html>'

    def test_stream_mp3_fast_path_uses_one_album_get_for_two_tracks(self):
        module = self.load_server()
        album_url = f'{module.BASE}/game-soundtracks/album/va-2024'
        module.sess = FakeSession({album_url: DummyResponse(text=VA_HTML)})
        client = TestClient(module.app)

        for idx in (1, 2):
            resp = client.get(
                '/rest/stream',
                params={'u': 'admin', 'p': 'admin', 'id': f'track/va-2024/{idx}'},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 302)
            self.assertIn('/soundtracks/va-2024/', resp.headers['location'])

        self.assertEqual(module.sess.calls, [album_url])

    def test_unknown_player_falls_back_to_song_page_for_mp3(self):
        module = self.load_server()
        basename = '01. Track.mp3'
        album_url = f'{module.BASE}/game-soundtracks/album/demo'
        track_url = f'{module.BASE}/game-soundtracks/album/demo/{urllib.parse.quote(basename)}'
        mp3_url = 'https://cdn.example.test/demo/01-track.mp3'
        module.sess = FakeSession({
            album_url: DummyResponse(text=self.album_html()),
            track_url: DummyResponse(text=self.song_page_html({'mp3': mp3_url})),
        })

        resolved = module.resolve_track('demo', basename, requested_format='mp3')
        self.assertEqual(resolved, {'files': {'mp3': mp3_url}})
        self.assertEqual(module.sess.calls, [album_url, track_url])

    def test_flac_request_fetches_song_page_after_mp3_fast_path(self):
        module = self.load_server()
        basename = '01. Track.mp3'
        album_url = f'{module.BASE}/game-soundtracks/album/demo'
        track_url = f'{module.BASE}/game-soundtracks/album/demo/{urllib.parse.quote(basename)}'
        direct_mp3 = 'https://nu.vgmtreasurechest.com/soundtracks/demo/hash-fast/01.%20Track.mp3'
        page_mp3 = 'https://cdn.example.test/demo/full/01-track.mp3'
        flac_url = 'https://cdn.example.test/demo/full/01-track.flac'
        script = player_markup('1', direct_mp3)
        module.sess = FakeSession({
            album_url: DummyResponse(text=self.album_html(formats=('mp3', 'flac'), script=script)),
            track_url: DummyResponse(text=self.song_page_html({'mp3': page_mp3, 'flac': flac_url})),
        })

        fast = module.resolve_track('demo', basename, requested_format='mp3')
        full = module.resolve_track('demo', basename, requested_format='flac')

        self.assertEqual(fast, {'files': {'mp3': direct_mp3}})
        self.assertEqual(full, {'files': {'mp3': page_mp3, 'flac': flac_url}})
        self.assertEqual(module.sess.calls, [album_url, track_url])

    def test_two_arg_resolve_track_keeps_full_files_compatibility(self):
        module = self.load_server()
        basename = '01. Track.mp3'
        album_url = f'{module.BASE}/game-soundtracks/album/demo'
        track_url = f'{module.BASE}/game-soundtracks/album/demo/{urllib.parse.quote(basename)}'
        script = player_markup('1', 'https://nu.vgmtreasurechest.com/soundtracks/demo/hash-fast/01.%20Track.mp3')
        files = {
            'mp3': 'https://cdn.example.test/demo/full/01-track.mp3',
            'flac': 'https://cdn.example.test/demo/full/01-track.flac',
        }
        module.sess = FakeSession({
            album_url: DummyResponse(text=self.album_html(formats=('mp3', 'flac'), script=script)),
            track_url: DummyResponse(text=self.song_page_html(files)),
        })

        resolved = module.resolve_track('demo', basename)
        self.assertEqual(resolved, {'files': files})
        self.assertEqual(module.sess.calls, [track_url])

    def test_singleflight_allows_only_one_album_fetch(self):
        module = self.load_server()
        album_url = f'{module.BASE}/game-soundtracks/album/demo'
        started = threading.Event()
        release = threading.Event()
        calls = []

        def route(**_kwargs):
            calls.append('hit')
            started.set()
            release.wait(1)
            return DummyResponse(text=self.album_html())

        module.sess = FakeSession({album_url: route})
        results = []

        def worker():
            results.append(module.load_album('demo'))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        started.wait(1)
        t2.start()
        release.set()
        t1.join()
        t2.join()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['slug'], 'demo')
        self.assertEqual(results[1]['slug'], 'demo')

    def test_exception_releases_singleflight_lock_for_retry(self):
        module = self.load_server()
        basename = '01. Track.mp3'
        album_url = f'{module.BASE}/game-soundtracks/album/demo'
        track_url = f'{module.BASE}/game-soundtracks/album/demo/{urllib.parse.quote(basename)}'
        module.sess = FakeSession({
            album_url: DummyResponse(text=self.album_html(formats=('mp3', 'flac'))),
            track_url: [RuntimeError('boom'), DummyResponse(text=self.song_page_html({'flac': 'https://cdn.example.test/demo/full/01-track.flac'}))],
        })

        first = module.resolve_track('demo', basename, requested_format='flac')
        second = module.resolve_track('demo', basename, requested_format='flac')

        self.assertIsNone(first)
        self.assertEqual(second, {'files': {'flac': 'https://cdn.example.test/demo/full/01-track.flac'}})
        self.assertFalse(module._flights)

    def test_invalid_library_download_keeps_last_good_disk_and_memory(self):
        good = {'albums': [{'slug': 'good', 'title': 'Good Album'}]}
        module = self.load_server(library=good)
        before_disk = Path(module.LIBRARY_PATH).read_text(encoding='utf-8')
        self.assertIn('good', module.ALBUMS)

        bad_payload = b'{"albums": [{"title": "Missing slug"}]}'
        response = DownloadResponse(bad_payload, headers={'ETag': 'new', 'Last-Modified': 'later'})
        with mock.patch.object(module.urllib.request, 'urlopen', return_value=response):
            with self.assertRaises(ValueError):
                module.refresh_library()

        self.assertEqual(Path(module.LIBRARY_PATH).read_text(encoding='utf-8'), before_disk)
        self.assertIn('good', module.ALBUMS)
        self.assertEqual(len(module.ALBUMS), 1)


if __name__ == '__main__':
    unittest.main()
