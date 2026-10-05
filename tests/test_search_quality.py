import gzip
import importlib.util
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]


def load_songs(tmp_path, monkeypatch):
    monkeypatch.setenv('SONGS_DB', str(tmp_path / 'songs.sqlite'))
    monkeypatch.setenv('SONGS_REFRESH_HOURS', '0')
    monkeypatch.setenv('SONG_SEARCH', 'auto')
    name = 'test_search_quality_songs'
    spec = importlib.util.spec_from_file_location(name, ROOT / 'songs.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_fixture_db(mod, tmp_path):
    rows = [
        ('mario', 1, 1, 'Jump Up, Super Star!'),
        ('mario', 1, 2, 'Super Mario Odyssey'),
        ('ski', 1, 1, 'Ski Safari Theme'),
        ('other', 1, 1, 'Jumping Superstar'),
        ('other', 1, 2, 'Jump up Superstar'),
        ('jp', 1, 1, '風のノクターン ～夜想曲～'),
        ('width', 1, 1, 'ＳＵＰＥＲ　ＭＡＲＩＯ'),
    ]
    archive = tmp_path / 'songs.tsv.gz'
    with gzip.open(archive, 'wt', encoding='utf-8') as stream:
        for album, disc, number, title in rows:
            stream.write(f'{album}\t{disc}\t{number}\t{title}\n')
    meta = {
        'schema': mod.SCHEMA,
        'tsv_schema': mod.TSV_SCHEMA,
        'source': mod.SONGS_URL,
        'built': int(time.time()),
        'checked': int(time.time()),
        'rows': len(rows),
        'content_digest': 'fixture',
    }
    mod._build(str(archive), mod.SONGS_DB, meta)
    assert mod._swap_in(mod.SONGS_DB)
    return mod


def titles(mod, query):
    return [row[3] for row in mod.candidates(query, 20)]


def test_song_search_handles_gaps_joined_words_unicode_and_typo(monkeypatch, tmp_path):
    mod = build_fixture_db(load_songs(tmp_path, monkeypatch), tmp_path)

    assert titles(mod, 'Jump up star')[0] == 'Jump Up, Super Star!'
    joined = titles(mod, 'supermario')
    assert joined[0] == 'ＳＵＰＥＲ　ＭＡＲＩＯ'
    assert 'Super Mario Odyssey' in joined
    spaced = titles(mod, 'super mario')
    assert spaced[0] == 'ＳＵＰＥＲ　ＭＡＲＩＯ'
    assert 'Super Mario Odyssey' in spaced
    width = titles(mod, 'ＳＵＰＥＲ ｍａｒｉｏ')
    assert width[0] == 'ＳＵＰＥＲ　ＭＡＲＩＯ'
    assert 'Super Mario Odyssey' in width
    assert titles(mod, '風のノクターン')[0] == '風のノクターン ～夜想曲～'
    assert titles(mod, 'Aki safari')[0] == 'Ski Safari Theme'


def test_short_words_are_checked_as_words_not_inside_other_words(monkeypatch, tmp_path):
    mod = build_fixture_db(load_songs(tmp_path, monkeypatch), tmp_path)
    found = titles(mod, 'jump up star')
    assert found[0] == 'Jump Up, Super Star!'
    assert 'Jump up Superstar' in found
    assert found.index('Jump Up, Super Star!') < found.index('Jump up Superstar')
    assert 'Jumping Superstar' not in found


def test_rank_prefers_exact_then_phrase_then_word_match():
    from search_utils import rank_text
    assert rank_text('Mario Odyssey', 'Mario Odyssey') < rank_text(
        'Mario Odyssey', 'Super Mario Odyssey')
    assert rank_text('Jump up star', 'Jump Up, Super Star!') is not None
    assert rank_text('Aki safari', 'Ski Safari Theme') is not None
    assert rank_text('Aki safari', 'Unrelated Safari Theme') is None
