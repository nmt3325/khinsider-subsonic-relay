"""Statically read KHInsider player URLs; never execute website JavaScript.

Vendored identically in khinsider-index/scripts and khinsider-subsonic-relay.
The index copy is the source of truth. Unknown player layouts return {} so
callers can use their existing per-song-page resolver. MP3 only: other
formats MUST be read from real download links, never guessed from a hash.
"""
from itertools import islice
import json
import re
from urllib.parse import unquote, urlsplit

MAX_HTML = 16 * 1024 * 1024
MAX_WORDS = 100000
MAX_TRACKS = 20000
_STRING = r"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
_PACKED = re.compile(
    r"\}\s*\(\s*(" + _STRING + r")\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*("
    + _STRING + r")\s*\.split\(\s*['\"]\|['\"]\s*\)", re.S)
_SCRIPT = re.compile(r'<script\b[^>]*>(.*?)</script\s*>', re.S | re.I)
# JavaScript packer's word boundaries are ASCII, even beside Japanese text.
_WORD = re.compile(r'\b[0-9A-Za-z]+\b', re.ASCII)
_DIGITS = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'


def _string(literal):
    """Decode just a JS string literal, without eval or a JS interpreter."""
    body, out, i = literal[1:-1], [], 0
    escapes = {'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t',
               'v': '\v', '0': '\0'}
    while i < len(body):
        char = body[i]
        i += 1
        if char != '\\':
            out.append(char)
            continue
        if i >= len(body):
            raise ValueError('truncated escape')
        char = body[i]
        i += 1
        if char in ('x', 'u'):
            size = 2 if char == 'x' else 4
            token = body[i:i + size]
            if len(token) != size or not re.fullmatch(r'[0-9a-fA-F]+', token):
                raise ValueError('invalid unicode escape')
            out.append(chr(int(token, 16)))
            i += size
        elif char in ('\r', '\n'):
            if char == '\r' and i < len(body) and body[i] == '\n':
                i += 1
        else:
            out.append(escapes.get(char, char))
    return ''.join(out)


def _unpack(match):
    payload, radix, count, words = match.groups()
    radix, count = int(radix), int(count)
    if not 2 <= radix <= 62 or not 0 <= count <= MAX_WORDS:
        raise ValueError('unsupported packer parameters')
    payload, words = _string(payload), _string(words).split('|')
    if count > len(words):
        raise ValueError('truncated packer dictionary')

    def replace(token):
        word, value = token.group(), 0
        # Large unencoded identifiers are not packer dictionary indexes.
        if len(word) > 8:
            return word
        for char in word:
            digit = _DIGITS.find(char)
            if digit < 0 or digit >= radix:
                return word
            value = value * radix + digit
            if value >= count:
                return word
        return words[value] or word

    return _WORD.sub(replace, payload)


def valid_mp3_url(url, slug):
    """Accept only the album's observed HTTPS VGM Treasure Chest MP3 URLs."""
    if not isinstance(url, str) or not isinstance(slug, str):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        pieces = parsed.path.strip('/').split('/')
        return (parsed.scheme == 'https' and parsed.username is None
                and parsed.password is None and parsed.port in (None, 443)
                and (host == 'vgmtreasurechest.com'
                     or host.endswith('.vgmtreasurechest.com'))
                and len(pieces) >= 4 and pieces[0] == 'soundtracks'
                and unquote(pieces[1]) == unquote(slug)
                and all(unquote(p) not in ('.', '..') for p in pieces)
                and unquote(pieces[-1]).lower().endswith('.mp3')
                and not any(ord(c) < 32 for c in url))
    except ValueError:
        return False


def _assignment(script, name):
    match = re.search(r'\b' + name + r'\s*=\s*(' + _STRING + r')', script)
    return _string(match.group(1)) if match else ''


def _array_literal(text):
    """Read a JSON-like array, allowing JS trailing commas outside strings."""
    depth, quoted, escaped, out = 0, False, False, []
    for i, char in enumerate(text):
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == chr(92):
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
        elif char in ']}':
            depth -= 1
        elif char == ',':
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] in ']}':
                continue
        out.append(char)
        if depth == 0:
            return json.loads(''.join(out))
    raise ValueError('unterminated player array')


def _player(script, slug):
    match = re.search(r'\btracks\s*=\s*(\[)', script)
    if match is None:
        return {}
    # KHInsider emits a JSON array; reject expressions/unknown JS layouts.
    tracks = _array_literal(script[match.start(1):])
    if not isinstance(tracks, list) or not tracks or len(tracks) > MAX_TRACKS:
        return {}
    prefix, suffix = _assignment(script, 'mediaPath'), _assignment(script, 'extension')
    found = {}
    for track in tracks:
        if not isinstance(track, dict):
            return {}
        songid, file = str(track.get('songid', '')), track.get('file')
        if not songid.isascii() or not songid.isdecimal() or not isinstance(file, str):
            return {}
        if songid in found:
            return {}
        url = file if file.startswith('https://') else prefix + file + suffix
        if not valid_mp3_url(url, slug):
            return {}
        found[songid] = url
    return found


def extract_player_urls(html, slug):
    """Return {songid-as-string: actual MP3 URL}; {} means fallback required."""
    if not isinstance(html, str) or len(html) > MAX_HTML:
        return {}
    scripts = _SCRIPT.findall(html) if '<script' in html.lower() else [html]
    for script in scripts:
        candidates = []
        for match in islice(_PACKED.finditer(script), 8):
            try:
                candidates.append(_unpack(match))
            except (ValueError, TypeError, KeyError, RecursionError):
                continue
        candidates.append(script)
        for candidate in candidates:
            try:
                found = _player(candidate, slug)
                if found:
                    return found
            except (ValueError, TypeError, KeyError, RecursionError):
                continue
    return {}
