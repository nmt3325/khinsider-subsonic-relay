import io
import json
import os
import time
import threading
from types import SimpleNamespace
from unittest import mock

import pytest
from khinsider_http import PageFetcher, UpstreamUnavailable

URL = 'https://downloads.khinsider.com/game-soundtracks/album/demo'


def response(code=200, text='album page', headers=None):
    return SimpleNamespace(status_code=code, text=text, headers=headers or {}, url=URL)


def solution():
    return {'status':200, 'url':URL, 'response':'<html>actual album</html>',
            'headers':{}, 'userAgent':'Mozilla/5.0 test-browser', 'cookies':[
                {'name':'cf_clearance','value':'private-cookie','domain':'.khinsider.com',
                 'path':'/', 'expires':time.time()+3600},
                {'name':'analytics','value':'do-not-save','domain':'.khinsider.com',
                 'path':'/', 'expires':time.time()+3600}]}


def session(*replies):
    return SimpleNamespace(get=mock.Mock(side_effect=replies), headers={}, cookies=mock.Mock())


def test_normal_pages_do_not_call_browser():
    s=session(response())
    with mock.patch('khinsider_http.urlopen') as browser:
        assert PageFetcher('http://browser:8191').get(URL,s).status_code==200
    browser.assert_not_called()


def test_challenge_recovers_and_restores_private_host_scoped_clearance(tmp_path):
    sol=solution();path=tmp_path/'clearance.json'
    s=session(response(403,headers={'cf-mitigated':'challenge'}))
    body=json.dumps({'status':'ok','solution':sol}).encode()
    with mock.patch('khinsider_http.urlopen',return_value=io.BytesIO(body)) as browser:
        r=PageFetcher('http://browser:8191',path).get(URL,s)
    assert r.text=='<html>actual album</html>'
    assert s.headers['User-Agent']==sol['userAgent']
    s.cookies.set.assert_called_once_with('cf_clearance','private-cookie',domain='.khinsider.com',path='/')
    assert os.stat(path).st_mode & 0o777 == 0o600
    stored=json.loads(path.read_text())
    assert [c['name'] for c in stored['cookies']]==['cf_clearance']
    request=browser.call_args.args[0]
    assert json.loads(request.data)=={'cmd':'request.get','url':URL,'maxTimeout':60000}
    new_session=session(response())
    PageFetcher('http://browser:8191',path).get(URL,new_session)
    assert new_session.headers['User-Agent']==sol['userAgent']
    new_session.cookies.set.assert_called_once()


def test_failed_solver_does_not_overwrite_last_good_clearance(tmp_path):
    path=tmp_path/'clearance.json';path.write_text(json.dumps(solution()));before=path.read_bytes()
    s=session(response(403,headers={'cf-mitigated':'challenge'}))
    with mock.patch('khinsider_http.urlopen',return_value=io.BytesIO(b'{"status":"error"}')):
        with pytest.raises(UpstreamUnavailable):PageFetcher('http://browser:8191',path).get(URL,s)
    assert path.read_bytes()==before


def test_browser_redirect_outside_khinsider_is_rejected():
    sol=solution();sol['url']='http://169.254.169.254/'
    with mock.patch('khinsider_http.urlopen',return_value=io.BytesIO(json.dumps({'status':'ok','solution':sol}).encode())):
        with pytest.raises(UpstreamUnavailable):
            PageFetcher('http://browser:8191').get(URL,session(response(403,headers={'cf-mitigated':'challenge'})))


def test_expired_or_wrong_domain_cookies_are_not_loaded(tmp_path):
    sol=solution();sol['cookies'][0]['domain']='.example.com'
    sol['cookies'][1].update(name='cf_clearance',expires=time.time()-100)
    path=tmp_path/'clearance.json';path.write_text(json.dumps(sol));s=session(response())
    PageFetcher(clearance_path=path).get(URL,s)
    s.cookies.set.assert_not_called()
    assert not s.headers


@pytest.mark.parametrize('code',[403,429,500,503])
def test_temporary_status_is_not_missing_song(code):
    with pytest.raises(UpstreamUnavailable):PageFetcher().get(URL,session(response(code)))


def test_real_404_is_preserved():
    assert PageFetcher().get(URL,session(response(404))).status_code==404


def test_challenge_html_with_200_is_not_an_album():
    html='<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/test"></script>'
    with pytest.raises(UpstreamUnavailable):PageFetcher().get(URL,session(response(200,html)))


def test_concurrent_challenges_share_one_browser_recovery():
    fetcher=PageFetcher('http://browser:8191');gate=threading.Barrier(2);counter=iter(range(10));guard=threading.Lock()
    def get(*args,**kwargs):
        with guard:n=next(counter)
        if n<2:
            gate.wait(timeout=3)
            return response(403,headers={'cf-mitigated':'challenge'})
        return response()
    s=SimpleNamespace(get=get,headers={},cookies=mock.Mock());results=[];failures=[]
    def work():
        try:results.append(fetcher.get(URL,s))
        except Exception as e:failures.append(e)
    with mock.patch.object(fetcher,'_solve',return_value=(response(),solution())) as solve:
        threads=[threading.Thread(target=work) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=5)
    assert not failures
    assert len(results)==2
    solve.assert_called_once_with(URL)


def test_selenium_expiry_field_is_normalized_and_persisted(tmp_path):
    sol=solution();cookie=sol['cookies'][0];cookie['expiry']=cookie.pop('expires')
    path=tmp_path/'clearance.json';s=session(response(403,headers={'cf-mitigated':'challenge'}))
    with mock.patch('khinsider_http.urlopen',return_value=io.BytesIO(json.dumps({'status':'ok','solution':sol}).encode())):
        PageFetcher('http://browser:8191',path).get(URL,s)
    stored=json.loads(path.read_text())
    assert stored['cookies'][0]['expires']==cookie['expiry']
    s.cookies.set.assert_called_once()
