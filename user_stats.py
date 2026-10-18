"""Persistent single-account ratings and playback history for the Subsonic relay.

Track IDs are the stable Subsonic IDs (track/<album-slug>/<one-based-index>).
Only scrobble submissions count as completed plays.  A "now playing" ping
(submission=false) does not affect the library's listening statistics.
"""
import datetime
import os
import sqlite3
import threading
import time
from contextlib import contextmanager


def _iso_time(milliseconds):
    """Subsonic's ISO-8601 UTC datetime for a playback timestamp."""
    return datetime.datetime.fromtimestamp(
        milliseconds / 1000, datetime.timezone.utc
    ).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


class UserStatsStore:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._conn() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS track_stats (
                    song_id TEXT PRIMARY KEY,
                    rating INTEGER CHECK (rating BETWEEN 1 AND 5),
                    play_count INTEGER NOT NULL DEFAULT 0,
                    last_played_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS play_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    song_id TEXT NOT NULL,
                    played_at_ms INTEGER NOT NULL,
                    recorded_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_play_history_time
                    ON play_history(played_at_ms DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_play_history_song
                    ON play_history(song_id, played_at_ms DESC);
                CREATE TABLE IF NOT EXISTS favorites (
                    kind TEXT NOT NULL CHECK(kind IN ('song', 'album', 'artist')),
                    item_id TEXT NOT NULL,
                    starred_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (kind, item_id)
                );
                CREATE INDEX IF NOT EXISTS idx_favorites_time
                    ON favorites(starred_at_ms DESC);
            ''')

    @contextmanager
    def _conn(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute('PRAGMA busy_timeout=15000')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def set_rating(self, song_id, rating):
        if isinstance(rating, bool) or not isinstance(rating, int) or not (0 <= rating <= 5):
            raise ValueError('Rating must be an integer from 0 to 5.')
        with self.lock, self._conn() as db:
            db.execute('''
                INSERT INTO track_stats (song_id, rating)
                VALUES (?, ?)
                ON CONFLICT(song_id) DO UPDATE SET rating=excluded.rating
            ''', (song_id, rating or None))

    def set_favorites(self, entries, starred):
        """Atomically star/unstar (kind, item_id) pairs, preserving first-star time.

        This is distinct from ratings: removing a favorite must never erase
        its 1-5-star score, scrobbles, or play history.
        """
        entries = list(dict.fromkeys(entries))
        if not entries:
            raise ValueError('At least one favorite id must be provided.')
        for kind, item_id in entries:
            if kind not in ('song', 'album', 'artist') or not item_id:
                raise ValueError('Invalid favorite id.')
        now = int(time.time() * 1000)
        with self.lock, self._conn() as db:
            if starred:
                db.executemany('''
                    INSERT OR IGNORE INTO favorites(kind, item_id, starred_at_ms)
                    VALUES (?, ?, ?)
                ''', [(kind, item_id, now) for kind, item_id in entries])
            else:
                db.executemany('''
                    DELETE FROM favorites WHERE kind = ? AND item_id = ?
                ''', entries)

    def starred_at(self, kind, item_id):
        with self.lock, self._conn() as db:
            row = db.execute('''
                SELECT starred_at_ms FROM favorites
                WHERE kind = ? AND item_id = ?
            ''', (kind, item_id)).fetchone()
        return _iso_time(row[0]) if row else None

    def favorites(self, kind):
        if kind not in ('song', 'album', 'artist'):
            raise ValueError('Invalid favorite kind.')
        with self.lock, self._conn() as db:
            rows = db.execute('''
                SELECT item_id, starred_at_ms FROM favorites WHERE kind = ?
                ORDER BY starred_at_ms DESC, item_id ASC
            ''', (kind,)).fetchall()
        return [(item_id, _iso_time(at_ms)) for item_id, at_ms in rows]

    def record_plays(self, entries):
        """Persist a batch atomically as (song_id, epoch_milliseconds) pairs."""
        if not entries:
            return
        now = int(time.time() * 1000)
        with self.lock, self._conn() as db:
            for song_id, timestamp_ms in entries:
                db.execute('''
                    INSERT INTO play_history (song_id, played_at_ms, recorded_at_ms)
                    VALUES (?, ?, ?)
                ''', (song_id, timestamp_ms, now))
                db.execute('''
                    INSERT INTO track_stats (song_id, play_count, last_played_ms)
                    VALUES (?, 1, ?)
                    ON CONFLICT(song_id) DO UPDATE SET
                        play_count=track_stats.play_count + 1,
                        last_played_ms=MAX(
                            COALESCE(track_stats.last_played_ms, 0),
                            excluded.last_played_ms
                        )
                ''', (song_id, timestamp_ms))

    def get(self, song_id):
        with self.lock, self._conn() as db:
            row = db.execute('''
                SELECT t.rating, t.play_count, t.last_played_ms, f.starred_at_ms
                FROM (SELECT ? AS id) AS target
                LEFT JOIN track_stats AS t ON t.song_id = target.id
                LEFT JOIN favorites AS f ON f.kind = 'song' AND f.item_id = target.id
            ''', (song_id,)).fetchone()
        result = {}
        if row[0] is not None:
            result['userRating'] = row[0]
        if row[1]:
            result['playCount'] = row[1]
        if row[2] is not None:
            result['played'] = _iso_time(row[2])
        if row[3] is not None:
            result['starred'] = _iso_time(row[3])
        return result

    def recent(self, limit=100, offset=0):
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))
        with self.lock, self._conn() as db:
            rows = db.execute('''
                SELECT id, song_id, played_at_ms
                FROM play_history
                ORDER BY played_at_ms DESC, id DESC
                LIMIT ? OFFSET ?
            ''', (limit, offset)).fetchall()
        return [
            {'id': row[0], 'song_id': row[1],
             'playedAt': _iso_time(row[2]), 'playedAtMs': row[2]}
            for row in rows
        ]
