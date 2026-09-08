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

    def test_live_library_contract_rejects_partial_or_mixed_payloads(self):
        module = self.load_server()
        value = {'data_source': 'khinsider-live-v2', 'complete': True,
                 'dataset_schema_version': 2, 'legacy_inputs': [],
                 'albums': [{'slug': 'alpha', 'title': 'Alpha'}]}
        self.assertEqual(module._validate_library_data(value), value)
        for key, bad in [('complete', False), ('dataset_schema_version', 1),
                         ('legacy_inputs', ['index.json'])]:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    module._validate_library_data(dict(value, **{key: bad}))

    def test_encoded_slug_accepts_decoded_album_song_links(self):
        from bs4 import BeautifulSoup
        module = self.load_server()
        soup = BeautifulSoup(self.album_html(slug='café', title='Café theme'), 'html.parser')
        tracks = module._parse_songlist(soup.find('table'), 'caf%C3%A9')
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]['title'], 'Café theme')

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


    def test_cumulative_snapshot_with_pending_collection_is_served(self):
        payload = {
            'data_source': 'khinsider-live-v2', 'dataset_schema_version': 2,
            'complete': True, 'legacy_inputs': [],
            'completeness_scope': 'cumulative_snapshot', 'crawl_complete': False,
            'coverage': {'pending': 1}, 'album_count': 1,
            'albums': [{'slug': 'demo', 'title': 'Demo'}],
        }
        mod = self.load_server(payload)
        client = TestClient(mod.app)
        response = client.get('/rest/getAlbumList2', params={
            'u': 'admin', 'p': 'admin', 'v': '1.16.1', 'c': 'test', 'f': 'json',
            'type': 'alphabeticalByName', 'size': 10,
        })
        self.assertEqual(response.status_code, 200)
        result = response.json()['subsonic-response']
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['albumList2']['album'][0]['name'], 'Demo')

    def test_song_ids_preserve_observed_extensionless_and_truncated_basenames(self):
        from bs4 import BeautifulSoup
        for basename in ('long-filename.mp', 'long-filename-without-extension', 'a; b.mp3'):
            with self.subTest(basename=basename):
                mod = self.load_server()
                soup = BeautifulSoup(self.album_html(title='Observed Track'), 'html.parser')
                for link in soup.select('#songlist a[href]'):
                    if '/game-soundtracks/album/demo/' in link['href']:
                        link['href'] = '/game-soundtracks/album/demo/' + basename
                fake = FakeSession({
                    mod.BASE + '/game-soundtracks/album/demo': DummyResponse(text=str(soup)),
                })
                mod.sess = fake
                album = mod.load_album('demo')
                self.assertEqual(len(album['tracks']), 1)
                self.assertEqual(album['tracks'][0]['basename'], basename)
                self.assertEqual(album['tracks'][0]['title'], 'Observed Track')
                self.assertEqual(len(fake.calls), 1)


    def test_redirected_album_preserves_old_ids_and_resolves_canonical_media_paths(self):
        mod = self.load_server()
        mp3 = 'https://nu.vgmtreasurechest.com/soundtracks/canonical/hash/song.mp3'
        flac = 'https://nu.vgmtreasurechest.com/soundtracks/canonical/hash/song.flac'
        response = DummyResponse(text=self.album_html(
            slug='canonical', basename='song.mp3', script=player_markup('1', mp3)))
        response.url = mod.BASE + '/game-soundtracks/album/canonical'
        old_url = mod.BASE + '/game-soundtracks/album/old-alias'
        track_url = mod.BASE + '/game-soundtracks/album/canonical/song.mp3'
        fake = FakeSession({old_url: response, track_url: DummyResponse(
            text='<a href="' + flac + '"><span class="songDownloadLink">FLAC</span></a>')})
        mod.sess = fake
        album = mod.load_album('old-alias')
        self.assertEqual(album['slug'], 'old-alias')
        self.assertEqual(album['resolved_slug'], 'canonical')
        self.assertEqual(mod.album_child('old-alias', album=album)['id'], 'album/old-alias')
        self.assertEqual(mod.resolve_track('old-alias', 'song.mp3', requested_format='mp3')['files']['mp3'], mp3)
        self.assertEqual(mod.resolve_track('old-alias', 'song.mp3', requested_format='flac')['files']['flac'], flac)
        self.assertEqual(fake.calls, [old_url, track_url])

    def test_non_album_redirects_are_not_cached_as_albums(self):
        for final in ('https://downloads.khinsider.com/',
                      'https://example.com/game-soundtracks/album/demo'):
            with self.subTest(final=final):
                mod = self.load_server()
                response = DummyResponse(text=self.album_html())
                response.url = final
                mod.sess = FakeSession({mod.BASE + '/game-soundtracks/album/demo': response})
                self.assertIsNone(mod.load_album('demo'))
                self.assertIsNone(mod._cache_get('albums', 'demo', max_age=30 * 86400))

    def test_genre_browsing_accepts_client_recased_genre_names(self):
        payload = {
            'data_source': 'khinsider-live-v2', 'dataset_schema_version': 2,
            'complete': True, 'legacy_inputs': [],
            'albums': [
                {'slug': 'handheld', 'title': 'Handheld OST', 'platforms': ['3DS']},
                {'slug': 'console', 'title': 'Console OST', 'platforms': ['Wii U']},
            ],
        }
        mod = self.load_server(payload)
        client = TestClient(mod.app)
        self.assertIn('3DS', mod.GENRE_ALBUMS)

        def names_for(genre):
            response = client.get('/rest/getAlbumList2', params={
                'u': 'admin', 'p': 'admin', 'v': '1.16.1', 'c': 'test', 'f': 'json',
                'type': 'byGenre', 'genre': genre, 'size': 10,
            })
            self.assertEqual(response.status_code, 200)
            body = response.json()['subsonic-response']
            self.assertEqual(body['status'], 'ok')
            return [a['name'] for a in body.get('albumList2', {}).get('album', [])]

        for spelling in ('3DS', '3Ds', '3ds', ' 3ds '):
            with self.subTest(genre=spelling):
                self.assertEqual(names_for(spelling), ['Handheld OST'])
        self.assertEqual(names_for('wii u'), ['Console OST'])
        self.assertEqual(names_for('Game Boy'), [])

    def test_artist_browsing_is_available_in_both_browse_modes(self):
        payload = {
            'data_source': 'khinsider-live-v2', 'dataset_schema_version': 2,
            'complete': True, 'legacy_inputs': [],
            'albums': [
                {'slug': 'handheld', 'title': 'Handheld OST',
                 'publishers': ['SEGA'], 'platforms': ['3DS']},
                {'slug': 'console', 'title': 'Console OST',
                 'publishers': ['Sunsoft / Tokuma'], 'platforms': ['Wii U']},
            ],
        }
        mod = self.load_server(payload)
        client = TestClient(mod.app)
        creds = {'u': 'admin', 'p': 'admin', 'v': '1.16.1', 'c': 'test', 'f': 'json'}

        def ok(endpoint, **params):
            response = client.get('/rest/' + endpoint, params=dict(creds, **params))
            self.assertEqual(response.status_code, 200)
            body = response.json()['subsonic-response']
            self.assertEqual(body['status'], 'ok')
            return body

        def titles(rows):
            return [row.get('name') or row.get('title') for row in rows]

        self.assertTrue(mod.USE_PUB_ARTISTS)
        groups = ok('getArtists')['artists']['index']
        self.assertEqual(sorted(a['name'] for g in groups for a in g['artist']),
                         ['SEGA', 'Sunsoft / Tokuma'])
        # folder browsing must expose the artists too, not just A-Z buckets
        self.assertEqual(ok('getIndexes')['indexes']['index'], groups)

        for name, aid in {a['name']: a['id'] for g in groups for a in g['artist']}.items():
            with self.subTest(artist=name):
                artist = ok('getArtist', id=aid)['artist']
                self.assertEqual(artist['name'], name)
                self.assertEqual(len(artist['album']), 1)
                directory = ok('getMusicDirectory', id=aid)['directory']
                self.assertEqual(directory['name'], name)
                self.assertEqual(len(directory['child']), 1)

        for spelling in ('pub/SEGA', 'pub/sega', 'pub/Sega', 'pub/ sega '):
            with self.subTest(artist_id=spelling):
                artist = ok('getArtist', id=spelling)['artist']
                self.assertEqual(artist['name'], 'SEGA')
                self.assertEqual(titles(artist['album']), ['Handheld OST'])

        quoted = 'pub/' + urllib.parse.quote('Sunsoft / Tokuma', safe='')
        self.assertEqual(ok('getArtist', id=quoted)['artist']['name'], 'Sunsoft / Tokuma')
        self.assertEqual(ok('getArtist', id='pub/sunsoft / tokuma')['artist']['name'],
                         'Sunsoft / Tokuma')

        missing = client.get('/rest/getArtist', params=dict(creds, id='pub/Nobody'))
        self.assertEqual(missing.json()['subsonic-response']['status'], 'failed')

        letter = ok('getMusicDirectory', id='letter/H')['directory']
        self.assertEqual(titles(letter['child']), ['Handheld OST'])

    def test_api_responses_are_gzipped_when_the_client_asks(self):
        payload = {
            'data_source': 'khinsider-live-v2', 'dataset_schema_version': 2,
            'complete': True, 'legacy_inputs': [],
            'albums': [
                {'slug': 'album-%03d' % i, 'title': 'Soundtrack Number %03d' % i,
                 'publishers': ['SEGA'], 'platforms': ['3DS']}
                for i in range(80)
            ],
        }
        mod = self.load_server(payload)
        self.assertEqual(mod.GZIP_MIN_SIZE, 1024)
        client = TestClient(mod.app)
        creds = {'u': 'admin', 'p': 'admin', 'v': '1.16.1', 'c': 'test', 'f': 'json'}

        def fetch(endpoint, accept_encoding, **params):
            response = client.get('/rest/' + endpoint, params=dict(creds, **params),
                                  headers={'Accept-Encoding': accept_encoding})
            self.assertEqual(response.status_code, 200)
            return response

        compressed = fetch('getAlbumList2', 'gzip', type='alphabeticalByName', size=80)
        plain = fetch('getAlbumList2', 'identity', type='alphabeticalByName', size=80)
        self.assertEqual(compressed.headers.get('content-encoding'), 'gzip')
        self.assertIsNone(plain.headers.get('content-encoding'))

        # identical JSON either way; only the bytes on the wire shrink
        self.assertEqual(compressed.json(), plain.json())
        self.assertEqual(len(plain.json()['subsonic-response']['albumList2']['album']), 80)
        on_the_wire = compressed.headers.get('content-length')
        self.assertIsNotNone(on_the_wire)
        self.assertLess(int(on_the_wire), len(plain.content))

        # small replies are not worth compressing
        small = fetch('ping', 'gzip')
        self.assertLess(len(small.content), mod.GZIP_MIN_SIZE)
        self.assertIsNone(small.headers.get('content-encoding'))

        # media bytes must never be routed through the compressor
        for path in ('/rest/stream', '/rest/stream.view', '/rest/download',
                     '/rest/getCoverArt', '/rest/getCoverArt.view'):
            with self.subTest(exempt=path):
                self.assertTrue(mod.gzip_exempt(path))
        for path in ('/', '/rest/getArtists', '/rest/getIndexes.view', '/rest/search3'):
            with self.subTest(compressed=path):
                self.assertFalse(mod.gzip_exempt(path))


    def test_empty_query_search3_lists_artists_and_albums_in_pages(self):
        """Offline-first clients sync via search3 with an empty query."""
        library = {
            'data_source': 'khinsider-live-v2',
            'dataset_schema_version': 2,
            'complete': True,
            'legacy_inputs': [],
            'albums': [{'slug': 'album-%d' % i,
                        'title': 'Soundtrack %02d' % i,
                        'publishers': ['Publisher %02d' % i],
                        'platforms': ['3DS']} for i in range(6)],
        }
        mod = self.load_server(library=library)
        creds = {'u': 'admin', 'p': 'admin', 'v': '1.16.1', 'c': 'test', 'f': 'json'}
        with TestClient(mod.app) as client:
            def search(**extra):
                params = dict(creds)
                params.update(extra)
                r = client.get('/rest/search3.view', params=params)
                self.assertEqual(r.status_code, 200)
                return r.json()['subsonic-response']['searchResult3']

            first = search(query='', artistCount=4, artistOffset=0,
                           albumCount=0, songCount=0)
            second = search(query='', artistCount=4, artistOffset=4,
                            albumCount=0, songCount=0)
            self.assertEqual(len(first['artist']), 4)
            self.assertEqual(len(second['artist']), 2)
            names = [a['name'] for a in first['artist'] + second['artist']]
            self.assertEqual(len(set(names)), 6)
            self.assertEqual(names, sorted(names, key=str.lower))
            for entry in first['artist']:
                self.assertTrue(entry['id'].startswith('pub/'))
                self.assertEqual(entry['albumCount'], 1)

            detail = client.get('/rest/getArtist.view',
                                params=dict(creds, id=first['artist'][0]['id']))
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()['subsonic-response']['artist']['name'],
                             first['artist'][0]['name'])

            page1 = search(query='', artistCount=0, albumCount=4,
                           albumOffset=0, songCount=0)
            page2 = search(query='', artistCount=0, albumCount=4,
                           albumOffset=4, songCount=0)
            titles = [a['title'] for a in page1['album'] + page2['album']]
            self.assertEqual(len(titles), 6)
            self.assertEqual(len(set(titles)), 6)
            self.assertEqual(titles, sorted(titles, key=str.lower))

            missing = search(artistCount=2, albumCount=2, songCount=0)
            self.assertEqual(len(missing['artist']), 2)
            self.assertEqual(len(missing['album']), 2)

            no_songs = search(query='', artistCount=0, albumCount=0, songCount=50)
            self.assertEqual(no_songs['song'], [])

            filtered = search(query='Soundtrack 03', artistCount=5,
                              albumCount=5, songCount=0)
            self.assertEqual([a['title'] for a in filtered['album']],
                             ['Soundtrack 03'])
            self.assertEqual(filtered['artist'], [])
    def test_artist_entries_carry_artwork_and_every_album_artist_resolves(self):
        """Strict clients need artwork fields and artistIds that really exist."""
        library = {
            'data_source': 'khinsider-live-v2',
            'dataset_schema_version': 2,
            'complete': True,
            'legacy_inputs': [],
            'albums': [
                {'slug': 'album-a', 'title': 'Alpha Soundtrack',
                 'publishers': ['Publisher A'], 'platforms': ['3DS']},
                {'slug': 'album-b', 'title': 'Beta Soundtrack',
                 'publishers': ['Publisher B'], 'platforms': ['3DS']},
                {'slug': 'album-c', 'title': 'Gamma Soundtrack',
                 'publishers': [], 'platforms': ['3DS']},
            ],
        }
        mod = self.load_server(library=library)
        creds = {'u': 'admin', 'p': 'admin', 'v': '1.16.1', 'c': 'test', 'f': 'json'}
        fallback_id = mod.pub_id(mod.FALLBACK_ARTIST)
        with TestClient(mod.app) as client:
            def call(ep, **extra):
                params = dict(creds)
                params.update(extra)
                response = client.get('/rest/%s.view' % ep, params=params)
                self.assertEqual(response.status_code, 200)
                return response.json()['subsonic-response']

            index = call('getArtists')['artists']['index']
            listed = [a for group in index for a in group['artist']]
            result = call('search3', query='', artistCount=50, albumCount=50,
                          songCount=0)['searchResult3']
            self.assertEqual(len(listed), 3)
            self.assertEqual(len(result['artist']), 3)
            for artist in listed + result['artist']:
                with self.subTest(artist=artist['name']):
                    self.assertTrue(artist['coverArt'].startswith('album/'))
                    self.assertTrue(artist['artistImageUrl'].startswith('http'))
                    self.assertIn('id=album%2F', artist['artistImageUrl'])

            artist_ids = {a['id'] for a in listed}
            self.assertIn(fallback_id, artist_ids)
            for album in result['album']:
                with self.subTest(album=album['id']):
                    self.assertIn(album['artistId'], artist_ids)

            fallback = call('getArtist', id=fallback_id)['artist']
            self.assertEqual(fallback['name'], mod.FALLBACK_ARTIST)
            self.assertEqual([a['id'] for a in fallback['album']], ['album/album-c'])
            self.assertTrue(fallback['coverArt'].startswith('album/'))

            info = call('getArtistInfo2', id=fallback_id)['artistInfo2']
            for key in ('smallImageUrl', 'mediumImageUrl', 'largeImageUrl'):
                with self.subTest(key=key):
                    self.assertTrue(info[key].startswith('http'))


if __name__ == '__main__':
    unittest.main()
