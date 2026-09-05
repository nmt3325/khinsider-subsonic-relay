"""Offline tests for the shared, non-executing KHInsider player decoder."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent
MODULE = ROOT / 'scripts' / 'khinsider_player.py'
if not MODULE.exists():
    MODULE = ROOT / 'khinsider_player.py'
spec = importlib.util.spec_from_file_location('tested_player', MODULE)
player = importlib.util.module_from_spec(spec)
spec.loader.exec_module(player)

URL1 = 'https://nu.vgmtreasurechest.com/soundtracks/demo/hash-one/01.%20A.mp3'
URL2 = 'https://nu.vgmtreasurechest.com/soundtracks/demo/hash-two/02.%20B.mp3'


def markup(rows, trailing=False):
    raw = json.dumps(rows, ensure_ascii=False)
    if trailing:
        raw = raw[:-1] + ',]'
    return '<script>var mediaPath="https://",extension="",tracks=' + raw + ';</script>'


class SharedPlayerTests(unittest.TestCase):
    def test_real_frozen_packed_player_has_all_tracks_and_different_hashes(self):
        html = (ROOT / '_testdata_player_va.html').read_text(encoding='utf-8')
        found = player.extract_player_urls(html, 'va-2024')
        self.assertEqual(len(found), 4)
        self.assertIn('/rdpqittl/', found['3497281'])
        self.assertIn('/bylmcynt/', found['3497282'])

    def test_independent_track_hashes(self):
        html = markup([{'songid':'1','file':URL1}, {'songid':2,'file':URL2}])
        self.assertEqual(player.extract_player_urls(html, 'demo'), {'1':URL1,'2':URL2})

    def test_relative_host_and_trailing_comma(self):
        html = markup([{'songid':'1','file':URL1[len('https://'):]}], trailing=True)
        self.assertEqual(player.extract_player_urls(html, 'demo'), {'1':URL1})

    def test_comma_and_brackets_inside_title_not_rewritten(self):
        raw = '[{"title":"A,] B,} \\\"C","rows":[1,2,],},]'
        self.assertEqual(player._array_literal(raw), [{'title':'A,] B,} "C','rows':[1,2]}])

    def test_escaped_javascript_strings(self):
        self.assertEqual(player._string(r"'https:\/\/example.test\x2f\u65e5'"),
                         'https://example.test/日')

    def test_duplicate_or_invalid_ids_require_fallback(self):
        for rows in ([{'songid':'1','file':URL1},{'songid':'1','file':URL2}],
                     [{'songid':'１２','file':URL1}], [{'songid':None,'file':URL1}]):
            with self.subTest(rows=rows):
                self.assertEqual(player.extract_player_urls(markup(rows), 'demo'), {})

    def test_invalid_hosts_paths_schemes_and_formats_require_fallback(self):
        bad = [URL1.replace('https:', 'http:'),
               URL1.replace('nu.vgmtreasurechest.com', 'nu.vgmtreasurechest.com.evil.test'),
               URL1.replace('/demo/', '/different-album/'),
               URL1.replace('mp3', 'flac'),
               URL1.replace('/hash-one/', '/%2e%2e/'),
               URL1.replace('https://', 'https://user:pass@'),
               URL1.replace('.com/', '.com:8080/')]
        for url in bad:
            with self.subTest(url=url):
                self.assertFalse(player.valid_mp3_url(url, 'demo'))
                self.assertEqual(player.extract_player_urls(markup([{'songid':'1','file':url}]), 'demo'), {})

    def test_javascript_is_never_executed(self):
        html = '<script>var tracks=__import__("os").system("false");</script>'
        self.assertEqual(player.extract_player_urls(html, 'demo'), {})

    def test_invalid_packer_and_truncated_array_require_fallback(self):
        for html in ('<script>eval(function(){}(\'a\',99,100000000,\'a\'.split(\'|\')))</script>',
                     '<script>var tracks=[{"songid":"1"};</script>', '<html>challenge</html>'):
            self.assertEqual(player.extract_player_urls(html, 'demo'), {})

    def test_bad_packed_candidate_does_not_hide_valid_plain_player(self):
        prelude = "eval(function(){}('a',99,100000000,'a'.split('|')))"
        html = markup([{'songid':'1','file':URL1}]).replace('<script>', '<script>' + prelude)
        self.assertEqual(player.extract_player_urls(html, 'demo'), {'1': URL1})

    def test_limit_and_wrong_album(self):
        self.assertEqual(player.extract_player_urls(' ' * (player.MAX_HTML + 1), 'demo'), {})
        self.assertEqual(player.extract_player_urls(markup([{'songid':'1','file':URL1}]), 'wrong'), {})


if __name__ == '__main__':
    unittest.main()
