"""Recover public KHInsider pages with a private FlareSolverr sidecar.

Only verified page HTML is returned. Clearance cookies are host-scoped, private,
and reused with the browser User-Agent; no Subsonic credentials reach the solver.
"""
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)
MAX_RESPONSE = 32 * 1024 * 1024


class UpstreamUnavailable(RuntimeError):
    """Temporary upstream failure, not a missing song."""


def is_challenge(response):
    headers = {str(k).lower(): str(v).lower() for k, v in response.headers.items()}
    text = response.text[:16000].lower()
    return (headers.get('cf-mitigated') == 'challenge'
            or ('/cdn-cgi/challenge-platform/' in text
                and ('just a moment' in text or response.status_code in (403, 503))))


class PageFetcher:
    def __init__(self, solver_url='', clearance_path=None, timeout_ms=60000):
        self.solver_url = solver_url.rstrip('/')
        if self.solver_url and not self.solver_url.endswith('/v1'):
            self.solver_url += '/v1'
        self.clearance_path = Path(clearance_path) if clearance_path else None
        self.timeout_ms = max(10000, min(int(timeout_ms), 120000))
        self._lock = threading.Lock()
        self._loaded = False
        self._generation = 0

    @staticmethod
    def _safe_url(url):
        p = urlsplit(url)
        return (p.scheme == 'https' and p.hostname == 'downloads.khinsider.com'
                and p.username is None and p.password is None and p.port in (None, 443)
                and p.path.startswith('/game-soundtracks/album/'))

    @staticmethod
    def _clearance(solution):
        ua = solution.get('userAgent', '')
        if not isinstance(ua, str) or not ua or len(ua) > 1024 or any(ord(c) < 32 for c in ua):
            return None
        cookies = []
        for c in solution.get('cookies') or []:
            if not isinstance(c, dict):
                continue
            if (c.get('name') not in ('cf_clearance', '__cf_bm')
                    or c.get('domain', '').lstrip('.') not in ('khinsider.com', 'downloads.khinsider.com')
                    or not isinstance(c.get('value'), str) or c.get('path', '/') != '/'):
                continue
            try:
                expiry = float(c.get('expires') or c.get('expiry', 0))
            except (TypeError, ValueError):
                continue
            if expiry <= time.time():
                continue
            cookies.append({k: c[k] for k in ('name', 'value', 'domain')})
            cookies[-1].update(path='/', expires=expiry)
        return {'userAgent': ua, 'cookies': cookies} if cookies else None

    def _apply(self, session, data):
        data = self._clearance(data)
        if not data:
            return False
        session.headers['User-Agent'] = data['userAgent']
        for c in data['cookies']:
            session.cookies.set(c['name'], c['value'], domain=c['domain'], path='/')
        return True

    def _load(self, session):
        if self._loaded:
            return
        self._loaded = True
        if self.clearance_path:
            try:
                data = json.loads(self.clearance_path.read_text(encoding='utf-8'))
                if self._apply(session, data):
                    LOG.info('Restored private KHInsider browser clearance')
            except (OSError, ValueError, TypeError):
                pass

    def _save(self, solution):
        data = self._clearance(solution)
        if not data or not self.clearance_path:
            return
        tmp = None
        try:
            self.clearance_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix='.clearance-', dir=self.clearance_path.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.clearance_path)
        except OSError:
            LOG.warning('Could not persist KHInsider browser clearance')
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def _solve(self, url):
        payload = json.dumps({'cmd': 'request.get', 'url': url,
                              'maxTimeout': self.timeout_ms}).encode()
        request = Request(self.solver_url, data=payload,
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout_ms / 1000 + 15) as r:
                raw = r.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError('oversized browser response')
            result = json.loads(raw)
            solution = result.get('solution') or {}
            if result.get('status') != 'ok':
                raise ValueError('browser did not solve the challenge')
            response = SimpleNamespace(status_code=int(solution.get('status', 0)),
                                       url=solution.get('url') or url,
                                       text=solution.get('response') or '',
                                       headers=solution.get('headers') or {})
            if (response.status_code != 200 or not self._safe_url(response.url)
                    or not isinstance(response.text, str) or not response.text
                    or is_challenge(response)):
                raise ValueError('browser returned an unverified page')
            return response, solution
        except Exception as exc:
            # Do not log HTML, cookies, or a solver response body.
            raise UpstreamUnavailable('KHInsider browser recovery failed (%s)' % type(exc).__name__) from exc

    @staticmethod
    def _check(response):
        if response.status_code in (403, 429) or response.status_code >= 500:
            raise UpstreamUnavailable('KHInsider returned HTTP %s' % response.status_code)
        return response

    def get(self, url, session, timeout=30):
        if not self._safe_url(url):
            raise ValueError('Only public KHInsider album/track pages are supported')
        with self._lock:
            self._load(session)
            generation = self._generation
        try:
            response = session.get(url, timeout=timeout)
        except Exception as exc:
            raise UpstreamUnavailable('KHInsider page request failed (%s)' % type(exc).__name__) from exc
        if not is_challenge(response):
            return self._check(response)
        LOG.warning('KHInsider Cloudflare challenge: HTTP %s at %s', response.status_code, urlsplit(url).path)
        if not self.solver_url:
            raise UpstreamUnavailable('KHInsider Cloudflare challenge; no browser helper configured')
        with self._lock:
            # Other requests can reuse the clearance acquired while we waited.
            if generation != self._generation:
                try:
                    response = session.get(url, timeout=timeout)
                    if not is_challenge(response):
                        return self._check(response)
                except UpstreamUnavailable:
                    raise
                except Exception:
                    pass
            response, solution = self._solve(url)
            self._apply(session, solution)
            self._save(solution)
            self._generation += 1
            LOG.warning('KHInsider browser recovery succeeded: %s', urlsplit(url).path)
            return response
