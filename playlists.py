"""Persistent single-user Subsonic playlist store.

The relay has one configured Subsonic account, so ownership is deliberately
simple. Playlist song metadata is stored only to make list responses cheap;
getPlaylist always resolves fresh song objects through the relay.
"""
import copy
import json
import os
import threading
import time


SCHEMA = 1


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())


class PlaylistStore:
    def __init__(self, path, owner):
        self.path = path
        self.owner = owner
        self.lock = threading.RLock()
        self.data = self._load()

    def _empty(self):
        return {'schema': SCHEMA, 'nextId': 1, 'playlists': {}}

    def _load(self):
        try:
            with open(self.path, encoding='utf-8') as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return self._empty()
        except Exception as exc:
            print('playlist store: failed to read %s (%s); starting empty' %
                  (self.path, exc))
            return self._empty()

        if not isinstance(data, dict) or data.get('schema') != SCHEMA:
            print('playlist store: unsupported data in %s; starting empty' % self.path)
            return self._empty()
        if not isinstance(data.get('playlists'), dict):
            return self._empty()
        try:
            next_id = max(1, int(data.get('nextId') or 1))
        except (TypeError, ValueError):
            next_id = 1
        data['nextId'] = next_id
        cleaned = {}
        for playlist_id, row in data['playlists'].items():
            if not isinstance(row, dict) or not row.get('name'):
                continue
            row = dict(row)
            row['id'] = str(row.get('id') or playlist_id)
            # The relay exposes one configured account. If its username changes,
            # existing local playlists still belong to that account.
            row['owner'] = self.owner
            row['public'] = bool(row.get('public'))
            row['comment'] = str(row.get('comment') or '')
            row['songs'] = [
                song for song in (row.get('songs') or [])
                if isinstance(song, dict) and song.get('id')
            ]
            cleaned[row['id']] = row
        data['playlists'] = cleaned
        return data

    def _save(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        tmp = '%s.%d.%d.tmp' % (self.path, os.getpid(), time.time_ns())
        try:
            with open(tmp, 'w', encoding='utf-8') as stream:
                json.dump(self.data, stream, ensure_ascii=False, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    @staticmethod
    def _copy(value):
        return copy.deepcopy(value)

    def list(self):
        with self.lock:
            rows = [self._copy(row) for row in self.data['playlists'].values()]
        return sorted(rows, key=lambda row: (row.get('name', '').casefold(), row['id']))

    def get(self, playlist_id):
        with self.lock:
            row = self.data['playlists'].get(str(playlist_id))
            return self._copy(row) if row is not None else None

    def create(self, name, songs):
        name = str(name or '').strip()
        if not name:
            raise ValueError('Playlist name is required.')
        now = _now()
        with self.lock:
            playlist_id = str(self.data['nextId'])
            while playlist_id in self.data['playlists']:
                self.data['nextId'] += 1
                playlist_id = str(self.data['nextId'])
            self.data['nextId'] += 1
            row = {
                'id': playlist_id,
                'name': name,
                'comment': '',
                'owner': self.owner,
                'public': False,
                'created': now,
                'changed': now,
                'songs': self._copy(songs),
            }
            self.data['playlists'][playlist_id] = row
            self._save()
            return self._copy(row)

    def replace(self, playlist_id, *, name=None, songs=None):
        with self.lock:
            row = self.data['playlists'].get(str(playlist_id))
            if row is None:
                raise KeyError(playlist_id)
            if name is not None:
                clean = str(name).strip()
                if not clean:
                    raise ValueError('Playlist name must not be empty.')
                row['name'] = clean
            if songs is not None:
                row['songs'] = self._copy(songs)
            row['changed'] = _now()
            self._save()
            return self._copy(row)

    def update(self, playlist_id, *, name=None, comment=None, public=None,
               add_songs=None, remove_indexes=None):
        with self.lock:
            row = self.data['playlists'].get(str(playlist_id))
            if row is None:
                raise KeyError(playlist_id)
            songs = list(row.get('songs') or [])
            indexes = sorted(set(remove_indexes or []), reverse=True)
            if any(index < 0 or index >= len(songs) for index in indexes):
                raise IndexError('songIndexToRemove is outside the playlist.')
            for index in indexes:
                del songs[index]
            songs.extend(self._copy(add_songs or []))

            changed = bool(indexes or add_songs)
            if name is not None:
                clean = str(name).strip()
                if not clean:
                    raise ValueError('Playlist name must not be empty.')
                if clean != row.get('name'):
                    changed = True
                row['name'] = clean
            if comment is not None:
                comment = str(comment)
                if comment != row.get('comment', ''):
                    changed = True
                row['comment'] = comment
            if public is not None:
                public = bool(public)
                if public != bool(row.get('public')):
                    changed = True
                row['public'] = public
            row['songs'] = songs
            if changed:
                row['changed'] = _now()
                self._save()
            return self._copy(row)

    def delete(self, playlist_id):
        with self.lock:
            row = self.data['playlists'].pop(str(playlist_id), None)
            if row is None:
                raise KeyError(playlist_id)
            self._save()
