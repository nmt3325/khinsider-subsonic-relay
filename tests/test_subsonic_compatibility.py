"""Protocol regressions reproduced by the 2026-11-12 compatibility audit.

All writes use temporary databases. No KHInsider or production service is used.
"""
import io
import json
import urllib.parse
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
from PIL import Image
import pytest

import test_runtime_server as runtime
from user_stats import UserStatsStore


AUTH = {'u': 'admin', 'p': 'admin', 'v': '1.16.1', 'c': 'compat-tests', 'f': 'json'}


@pytest.fixture
def relay(monkeypatch):
    case = runtime.RuntimeServerTests(methodName='runTest')
    rows = [
        {'slug': 'aaa', 'title': 'A Album', 'publishers': ['Zeta'],
         'platforms': ['SNES'], 'track_count': 2},
        {'slug': 'bbb', 'title': 'B Album', 'publishers': ['Middle'],
         'platforms': ['PS2'], 'track_count': 1, 'date_added': '2024-01-02'},
        {'slug': 'zzz', 'title': 'Z Album', 'publishers': ['Alpha'],
         'platforms': ['SNES'], 'track_count': 2, 'date_added': '2024-01-03'},
    ]
    payload = {'data_source': 'khinsider-live-v2', 'dataset_schema_version': 2,
               'complete': True, 'legacy_inputs': [], 'albums': rows}
    mod = case.load_server(payload)
    albums = {
        row['slug']: {
            'v': mod.ALBUM_CACHE_VERSION, 'slug': row['slug'],
            'title': row['title'], 'info': row, 'cover': 'https://example.invalid/cover.png',
            'tracks': [{'title': 'Theme %d' % i, 'basename': '%02d. Theme.mp3' % i,
                        'duration': 60 + i, 'formats': ['mp3'], 'num': i}
                       for i in range(1, row['track_count'] + 1)],
        }
        for row in rows
    }
    calls = []

    def load(slug):
        calls.append(slug)
        return albums.get(slug)

    monkeypatch.setattr(mod, 'load_album', load)
    mod.sess = runtime.FakeSession({})
    with TestClient(mod.app) as client:
        def api(endpoint, **params):
            response = client.get('/rest/' + endpoint + '.view', params=dict(AUTH, **params))
            assert response.status_code == 200
            return response.json()['subsonic-response']

        def ok(endpoint, **params):
            body = api(endpoint, **params)
            assert body['status'] == 'ok', body
            return body

        try:
            yield mod, client, api, ok, albums, calls
        finally:
            case.doCleanups()


def test_id3_album_required_fields_in_all_listing_paths(relay):
    mod, client, api, ok, albums, calls = relay
    ok('star', id='album/aaa')
    groups = [
        ok('getAlbumList2', type='alphabeticalByName')['albumList2']['album'],
        ok('getArtist', id='pub/Zeta')['artist']['album'],
        ok('search3', query='', songCount=0)['searchResult3']['album'],
        ok('getStarred2')['starred2']['album'],
    ]
    for group in groups:
        for album in group:
            for field in ('id', 'name', 'songCount', 'duration', 'created'):
                assert field in album
            assert isinstance(album['songCount'], int)
            assert isinstance(album['duration'], int)
            assert album['duration'] >= 0
            assert 'T' in album['created']
    assert calls == [], 'Listing must never fetch every album page'
    mod._cache_put('albums', 'aaa', albums['aaa'])
    assert mod.album_child('aaa')['duration'] == 123
    assert mod.album_child('aaa')['songCount'] == 2
    assert ok('getAlbum', id='album/aaa')['album']['duration'] == 123


def test_album_and_artist_ratings_favorites_and_directory_state(relay):
    mod, client, api, ok, albums, calls = relay
    for item_id, kind, rating in [('album/aaa', 'album', 5), ('pub/Zeta', 'artist', 4)]:
        ok('setRating', id=item_id, rating=rating)
        ok('star', id=item_id)
        directory = ok('getMusicDirectory', id=item_id)['directory']
        assert directory['userRating'] == rating
        assert directory['starred'] == mod.user_stats.starred_at(kind, item_id)
        reopened = UserStatsStore(mod.USER_STATS_PATH)
        assert reopened.get(item_id, kind)['userRating'] == rating
        ok('unstar', id=item_id)
        assert 'starred' not in ok('getMusicDirectory', id=item_id)['directory']
        assert reopened.get(item_id, kind)['userRating'] == rating
        ok('setRating', id=item_id, rating=0)
        assert 'userRating' not in reopened.get(item_id, kind)
    ok('setRating', id='pub/zeta', rating=3)
    assert ok('getArtist', id='pub/Zeta')['artist']['userRating'] == 3
    assert api('setRating', id='album/missing', rating=4)['error']['code'] == 70


def test_play_queue_post_preserves_order_duplicates_position_and_restart(relay):
    mod, client, api, ok, albums, calls = relay
    ids = ['track/aaa/1', 'track/zzz/2', 'track/aaa/1']
    form = list(AUTH.items()) + [('id', sid) for sid in ids]
    form += [('current', ids[1]), ('position', '12345')]
    response = client.post('/rest/savePlayQueue.view',
                           content=urllib.parse.urlencode(form),
                           headers={'Content-Type': 'application/x-www-form-urlencoded'})
    assert response.json()['subsonic-response']['status'] == 'ok'
    queue = ok('getPlayQueue')['playQueue']
    assert [entry['id'] for entry in queue['entry']] == ids
    assert queue['current'] == ids[1]
    assert queue['position'] == 12345
    assert queue['username'] == 'admin'
    assert queue['changedBy'] == AUTH['c']
    persisted = UserStatsStore(mod.USER_STATS_PATH).get_queue()
    assert persisted['current'] == queue['current']
    assert persisted['entry'] == queue['entry']
    # The read must not re-fetch upstream or disappear during an outage.
    calls.clear()
    mod.load_album = lambda slug: pytest.fail('Queue restoration must work offline')
    assert ok('getPlayQueue')['playQueue'] == queue
    assert calls == []
    assert mod.user_stats.recent() == []


def test_invalid_queue_save_is_atomic_and_empty_queue_can_clear(relay):
    mod, client, api, ok, albums, calls = relay
    ok('savePlayQueue', id='track/aaa/1', current='track/aaa/1', position=400)
    before = ok('getPlayQueue')['playQueue']
    for params in [
        {'id': 'track/missing/1'},
        {'id': 'track/aaa/1', 'current': 'track/aaa/2'},
        {'id': 'track/aaa/1', 'current': 'track/aaa/1', 'position': -1},
        {'id': 'track/aaa/1', 'current': 'track/aaa/1', 'position': 'abc'},
        {'id': 'track/aaa/1', 'position': 1},
    ]:
        assert api('savePlayQueue', **params)['status'] == 'failed'
        assert ok('getPlayQueue')['playQueue'] == before
    ok('savePlayQueue')
    assert ok('getPlayQueue')['playQueue']['entry'] == []
    assert 'current' not in ok('getPlayQueue')['playQueue']


def test_album_info2_key_and_legacy_song_id(relay):
    mod, client, api, ok, albums, calls = relay
    body = ok('getAlbumInfo2', id='album/aaa')
    assert 'albumInfo' in body and 'albumInfo2' not in body
    assert ok('getAlbumInfo', id='track/aaa/2')['albumInfo'] == body['albumInfo']
    assert api('getAlbumInfo', id='track/aaa/999')['error']['code'] == 70


@pytest.mark.parametrize('endpoint,key,item_id,text_field', [
    ('getAlbumInfo', 'albumInfo', 'album/aaa', 'notes'),
    ('getAlbumInfo2', 'albumInfo', 'album/aaa', 'notes'),
    ('getArtistInfo', 'artistInfo', 'pub/Zeta', 'biography'),
    ('getArtistInfo2', 'artistInfo2', 'pub/Zeta', 'biography'),
])
def test_info_xml_uses_child_elements(relay, endpoint, key, item_id, text_field):
    mod, client, api, ok, albums, calls = relay
    response = client.get('/rest/' + endpoint, params=dict(AUTH, f='xml', id=item_id))
    root = ET.fromstring(response.content)
    info = root.find('{*}' + key)
    assert info is not None
    assert text_field not in info.attrib
    assert info.find('{*}' + text_field) is not None
    assert info.find('{*}largeImageUrl') is not None


def test_album_list_orderings_use_artist_ratings_and_completed_plays(relay):
    mod, client, api, ok, albums, calls = relay
    mod.user_stats.record_plays([
        ('track/aaa/1', 1000), ('track/zzz/1', 2000),
        ('track/zzz/1', 3000), ('track/zzz/2', 4000),
    ])
    ok('setRating', id='album/zzz', rating=5)
    ok('setRating', id='album/aaa', rating=3)

    def ids(kind, **params):
        return [a['id'] for a in ok('getAlbumList', type=kind, **params)['albumList']['album']]

    assert ids('alphabeticalByArtist') == ['album/zzz', 'album/bbb', 'album/aaa']
    assert ids('alphabeticalByArtist', size=1, offset=1) == ['album/bbb']
    for kind in ('recent', 'frequent', 'highest'):
        assert ids(kind) == ['album/zzz', 'album/aaa']
    assert api('getAlbumList', type='not-a-type')['error']['code'] == 10


def test_genre_song_counts_and_paging(relay):
    mod, client, api, ok, albums, calls = relay
    genres = {g['value']: g for g in ok('getGenres')['genres']['genre']}
    assert genres['SNES']['songCount'] == 4
    assert genres['SNES']['albumCount'] == 2
    songs = ok('getSongsByGenre', genre=' snes ', count=2, offset=1)['songsByGenre']['song']
    assert [s['id'] for s in songs] == ['track/aaa/2', 'track/zzz/1']
    assert ok('getSongsByGenre', genre='Unknown')['songsByGenre']['song'] == []
    assert ok('getSongsByGenre', genre='SNES', count=0)['songsByGenre']['song'] == []


def test_empty_query_sync_can_page_all_songs_without_search_index(relay):
    mod, client, api, ok, albums, calls = relay
    mod.song_index = None
    found = []
    for offset in (0, 2, 4):
        page = ok('search3', query='', artistCount=0, albumCount=0,
                  songCount=2, songOffset=offset)['searchResult3']['song']
        found.extend(song['id'] for song in page)
    assert found == ['track/aaa/1', 'track/aaa/2', 'track/bbb/1',
                     'track/zzz/1', 'track/zzz/2']
    assert len(set(found)) == len(found)
    calls.clear()
    assert ok('search3', query='', songCount=2, songOffset=999)['searchResult3']['song'] == []
    assert calls == [], 'Offset skipping should use local counts, not re-crawl the catalogue'


@pytest.mark.parametrize('endpoint', ['ping', 'getLicense', 'getMusicFolders'])
def test_connection_tests_and_protected_api_reject_wrong_auth(relay, endpoint):
    mod, client, api, ok, albums, calls = relay
    assert api(endpoint, p='wrong')['error']['code'] == 40
    token = dict(AUTH, t='not-a-token', s='salt')
    del token['p']
    response = client.get('/rest/' + endpoint, params=token)
    assert response.json()['subsonic-response']['error']['code'] == 40


@pytest.mark.parametrize('missing', ['u', 'p', 'v', 'c'])
def test_common_required_parameters(relay, missing):
    mod, client, api, ok, albums, calls = relay
    params = dict(AUTH)
    del params[missing]
    response = client.get('/rest/getMusicFolders', params=params)
    assert response.json()['subsonic-response']['error']['code'] == 10


@pytest.mark.parametrize('version,code', [
    ('0.16.1', 20), ('2.0.0', 30), ('1.17.0', 30), ('garbage', 10),
])
def test_incompatible_api_versions(relay, version, code):
    mod, client, api, ok, albums, calls = relay
    assert api('getMusicFolders', v=version)['error']['code'] == code
    assert ok('getMusicFolders', v='1.16.999')['status'] == 'ok'
    assert ok('getMusicFolders', v='1.8.0')['status'] == 'ok'


def test_endpoint_required_parameters_and_user_authorization(relay):
    mod, client, api, ok, albums, calls = relay
    for endpoint in ('getSong', 'getAlbum', 'getArtist', 'getUser', 'getAlbumList2', 'search3'):
        assert api(endpoint)['error']['code'] == 10
    assert api('getAlbumList2', type='byGenre')['error']['code'] == 10
    assert api('getAlbumList2', type='byYear', fromYear=2000)['error']['code'] == 10
    assert api('getUser', username='somebody-else')['error']['code'] == 50
    assert ok('getUser', username='admin')['user']['username'] == 'admin'


def test_jsonp_is_wrapped_and_rejects_invalid_callbacks(relay):
    mod, client, api, ok, albums, calls = relay
    response = client.get('/rest/ping', params=dict(AUTH, f='jsonp', callback='app.callback'))
    assert response.headers['content-type'].startswith('application/javascript')
    assert response.text.startswith('app.callback(') and response.text.endswith(');')
    data = json.loads(response.text[len('app.callback('):-2])
    assert data['subsonic-response']['status'] == 'ok'
    for callback in ('', 'bad);alert(1)//', 'x\nattack'):
        response = client.get('/rest/ping', params=dict(AUTH, f='jsonp', callback=callback))
        assert response.json()['subsonic-response']['error']['code'] == 10


@pytest.mark.parametrize('endpoint', ['stream', 'download', 'getCoverArt'])
def test_binary_endpoint_errors_are_xml_even_with_json_requested(relay, endpoint):
    mod, client, api, ok, albums, calls = relay
    response = client.get('/rest/' + endpoint, params=dict(AUTH, id='not-a-song'))
    assert response.headers['content-type'].startswith('text/xml')
    root = ET.fromstring(response.content)
    assert root.attrib['status'] == 'failed'
    assert root.find('{*}error').attrib['code'] == '70'


def test_cover_resize_and_song_album_artist_ids(relay):
    mod, client, api, ok, albums, calls = relay
    output = io.BytesIO()
    Image.new('RGB', (600, 400), '#24506a').save(output, 'PNG')
    original = output.getvalue()
    mod.sess = runtime.FakeSession({
        'https://example.invalid/cover.png': runtime.DummyResponse(
            content=original, headers={'content-type': 'image/png'}),
    })
    for item_id in ('album/aaa', 'track/aaa/1', 'pub/Zeta'):
        response = client.get('/rest/getCoverArt', params=dict(AUTH, id=item_id, size=64))
        assert response.headers['content-type'] == 'image/png'
        with Image.open(io.BytesIO(response.content)) as image:
            assert image.size == (64, 43)
    response = client.get('/rest/getCoverArt', params=dict(AUTH, id='album/aaa'))
    assert response.content == original


def test_folder_filters_and_incremental_indexes(relay):
    mod, client, api, ok, albums, calls = relay
    first = ok('getIndexes')['indexes']
    assert ok('getIndexes')['indexes']['lastModified'] == first['lastModified']
    assert 'indexes' not in ok('getIndexes', ifModifiedSince=first['lastModified'])
    assert 'indexes' not in ok('getIndexes', ifModifiedSince=first['lastModified'] + 100000)
    assert ok('getIndexes', musicFolderId='999')['indexes']['index'] == []
    assert ok('getArtists', musicFolderId='999')['artists']['index'] == []
    assert ok('getAlbumList2', type='alphabeticalByName', musicFolderId='999')['albumList2']['album'] == []
    result = ok('search3', query='', musicFolderId='999')['searchResult3']
    assert result == {'artist': [], 'album': [], 'song': []}
    assert ok('getSongsByGenre', genre='SNES', musicFolderId='999')['songsByGenre']['song'] == []
    assert calls == []


def test_now_playing_is_visible_without_incrementing_play_history(relay):
    mod, client, api, ok, albums, calls = relay
    ok('scrobble', id='track/aaa/1', submission='false')
    assert mod.user_stats.get('track/aaa/1') == {}
    assert mod.user_stats.recent() == []
    items = ok('getNowPlaying')['nowPlaying']['entry']
    assert len(items) == 1
    assert items[0]['id'] == 'track/aaa/1'
    assert items[0]['username'] == 'admin'
    assert items[0]['playerName'] == AUTH['c']
    assert isinstance(items[0]['playerId'], int)
    assert items[0]['minutesAgo'] == 0
    assert api('scrobble', id='track/missing/1', submission='false')['error']['code'] == 70