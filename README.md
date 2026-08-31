# khinsider-subsonic-relay

Subsonic API-compatible relay server that publishes all of downloads.khinsider.com as a music library — without hosting a single audio file.

## 概要

downloads.khinsider.com の全アルバム（104,431件 / 2026-09-01時点）を Subsonic 互換ライブラリとして公開するリレーサーバーです。音声ファイルは一切保持せず、クライアントからの再生リクエスト時に khinsider の CDN（vgmtreasurechest.com）上の実URLへ **302リダイレクト** します。サーバーには帯域もストレージもほぼ不要です。

## 動作の仕組み

1. アルバム一覧は [khinsider-index v2026.09.01](https://github.com/nmt3325/khinsider-index/releases/tag/v2026.09.01) のライブクロール成果（library.json）を初回起動時に自動ダウンロード
2. アルバム詳細・トラック一覧・実ファイルURLはクライアントからのアクセス時にオンデマンドで実サイトから取得してキャッシュ（Cloudflare対策として curl_cffi の Chrome TLS フィンガープリントを使用）
3. `/rest/stream` は実ファイルの CDN URL へ 302 リダイレクト。リダイレクト非対応クライアント向けに `PROXY_STREAM=1` でサーバー中継モードも可

## 起動

Docker Compose:

```sh
docker compose up -d
```

ベアメタル:

```sh
pip install -r requirements.txt
SUBSONIC_USER=myuser SUBSONIC_PASSWORD=secret uvicorn server:app --host 0.0.0.0 --port 8080
```

## クライアント設定

Subsonic API 互換クライアント（Symfonium / Tempo / DSub / Substreamer / play:Sub / Ultrasonic など）で:

- サーバーURL: `http://<host>:8080`
- ユーザー名 / パスワード: 上記環境変数で設定したもの

## 環境変数

| 変数 | 既定値 | 説明 |
|---|---|---|
| `SUBSONIC_USER` | `admin` | ログインのユーザー名 |
| `SUBSONIC_PASSWORD` | `admin` | パスワード（必ず変更すること） |
| `PORT` | `8080` | 待受ポート |
| `LIBRARY_PATH` | `./library.json` | ライブラリデータのパス |
| `LIBRARY_URL` | index リリースのURL | library.json の取得元 |
| `CACHE_DIR` | `./cache` | ページキャッシュ先 |
| `PROXY_STREAM` | - | `1` で302ではなくサーバー中継 |

## 対応エンドポイント

ping / getLicense / getUser / getMusicFolders / getIndexes / getArtists / getArtist / getMusicDirectory / getAlbum / getAlbumList / getAlbumList2 / search2 / search3 / getSong / getCoverArt / stream / download / scrobble / star / unstar / getStarred / getStarred2 / getGenres

## 注意点

- ライブラリは 2026-09-01 時点のスナップショット。更新する場合は khinsider-index 側の `scripts/crawl_live.py` で再クロールして library.json を差し替える
- アルバム初回アクセス時は実ページ取得のため数百ms〜1秒程度の遅延あり（以後はキャッシュ）
- 外部公開する場合は HTTPS 化（Caddy等）と強いパスワードを推奨。Subsonic の legacy `p=` 認証は平文
- 楽曲データの権利は khinsider および各権利者に帰属。このサーバーは索引とリダイレクトのみを行う
