# khinsider-subsonic-relay

Subsonic API-compatible relay server that publishes all of downloads.khinsider.com as a music library — without hosting a single audio file.

## 概要

downloads.khinsider.com の全アルバム（104,431件 / 2026-09-01時点）を Subsonic 互換ライブラリとして公開するリレーサーバーです。音声ファイルは一切保持せず、クライアントからの再生リクエスト時に khinsider の CDN（vgmtreasurechest.com）上の実URLへ **302リダイレクト** します。サーバーには帯域もストレージもほぼ不要です。

## 動作の仕組み

1. アルバム一覧は [khinsider-index](https://github.com/nmt3325/khinsider-index) のライブクロール成果（library.json）を初回起動時に自動ダウンロード
2. アルバム詳細・トラック一覧・メタデータ・実ファイルURLはクライアントからのアクセス時にオンデマンドで実サイトから取得してキャッシュ（Cloudflare対策として curl_cffi の Chrome TLS フィンガープリントを使用）
3. `/rest/stream` は実ファイルの CDN URL へ 302 リダイレクト。リダイレクト非対応クライアント向けに `PROXY_STREAM=1` でサーバー中継モードも可

## メタデータのマッピング

khinsider のアルバムページにある情報ブロック（`Platforms:` / `Year:` / `Published by:` …）を Subsonic のタグへ変換します。

| khinsider | Subsonic フィールド | 例 |
|---|---|---|
| `Year` | `year`, `releaseDate`, `originalReleaseDate` | 2011 |
| `Published by` | `artist`, `albumArtist`, `displayArtist`, `displayAlbumArtist`, `artistId` | Nintendo |
| `Platforms` | `genre`, `genres[]` | 3DS |
| `Album type` | `genre`, `genres[]` | Gamerip |
| `Date Added` | `created` | 2026-04-07T00:00:00.000Z |
| `Developed by` | publisher が無い場合の albumArtist フォールバック | |
| `Catalog number` | `getAlbumInfo` の notes | |
| MP3 / FLAC 列 | `size`, `bitRate`, `suffix`, `contentType` | 4812963 / 301kbps / mp3 |
| `CD` 列 | `discNumber` | 1 |
| 曲の長さ列 | `duration`（曲・アルバム合計とも） | 128 |

- Subsonic の `genre` は1つしか持てないので先頭値（既定では platform の1件目）を入れ、OpenSubsonic の `genres[]` に platform と album type を全部並べます。順序は `GENRE_SOURCES` で変更可能。
- publisher が複数のときは先頭をアルバムアーティストにし、全件は `getAlbumInfo` / `getAlbumInfo2` の notes に出します。
- publisher も developer も無いアルバムは `FALLBACK_ARTIST`（既定 `KHInsider`）になります。
- `getArtists`（ID3ビュー）は **publisher をアーティストとして** 一覧します。`getIndexes`（フォルダビュー）は従来どおり頭文字 `0-9` / `A`-`Z` なので、クライアントの「アーティスト」タブと「フォルダ」タブで別々のブラウズができます。
- `getGenres` と `getAlbumList2?type=byGenre` で platform / album type 横断のジャンル閲覧、`type=byYear` でリリース年順、`type=newest` で Date Added 順。これらは library.json にメタデータが入っている場合のみ全件対象になります（未クロールのアルバムは個別アクセス時にライブ取得）。

### ID3 タグを読まない理由

このリレーは音声ファイル内の ID3/Vorbis タグを **読みません**。設計判断として意図的です。

- 一般的な Subsonic クライアントは、曲名・アーティスト・アルバム・ジャンル等を `getAlbum` / `getSong` / `getMusicDirectory` のレスポンスから表示します。サーバーが返した値が正であり、ストリーム本体のタグは参照しません（ローカルに保存して別プレイヤーで開く場合を除く）。つまり「再生開始後に ID3 を取り直す」処理は不要です。
- ID3 を読むには曲ごとに CDN への追加リクエスト（先頭数百KB〜数MBのレンジ取得）が必要です。100曲のアルバムを開くたびに100リクエストが増え、遅延・Cloudflare 負荷・CDN 転送量のいずれも見合いません。
- khinsider の rip はタグが空、あるいは `Track 01` のままのものが珍しくなく、アルバムページの情報より信頼できるとは限りません。
- `/rest/download` はファイルをそのまま返すので、タグを見たい場合はダウンロード後にクライアント側で扱えます。

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

`GET /`（認証なし）でライブラリ件数・メタデータ付き件数・publisher 数・ジャンル数・現在のモードを確認できます。

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
| `LIBRARY_PATH` | `./library.json` | ライブラリデータのパス（`.gz` も可） |
| `LIBRARY_URL` | index リリースのURL | library.json の取得元 |
| `CACHE_DIR` | `./cache` | ページキャッシュ先（アルバム30日） |
| `PROXY_STREAM` | - | `1` で302ではなくサーバー中継 |
| `GENRE_SOURCES` | `platform,album_type` | ジャンルに使う項目と順序。`album_type,platform` や `album_type` 単独も可 |
| `ARTIST_MODE` | `auto` | `auto`（メタデータがあれば publisher）/ `publisher` / `letter` |
| `FALLBACK_ARTIST` | `KHInsider` | publisher も developer も無い場合のアーティスト名 |

## ライブラリデータ

`library.json` は khinsider-index 側で生成します。

```sh
# khinsider-index リポジトリで
python3 scripts/crawl_album_meta.py          # アルバムページのメタデータを収集（レジューム可）
python3 scripts/build_library.py --gzip      # index.json とマージして library.json を生成
```

スキーマ（メタデータ項目は無くても動作します）:

```json
{
  "albums": [
    {"slug": "nintendo-3ds-background-music", "title": "3DS Background Music", "letter": "0-9",
     "year": 2011, "publishers": ["Nintendo"], "platforms": ["3DS"], "album_type": "Gamerip",
     "date_added": "2026-04-07", "track_count": 106, "duration": 9786}
  ]
}
```

メタデータが無い（旧形式の）library.json でも動きます。その場合はアルバムを開いた時点でページを取得して year / publisher / platform / album type を埋めるので、個々のアルバム表示は同じ結果になります。ただし `getAlbumList2?type=byGenre` / `byYear` / `newest` のような一覧系はライブラリ側の値を使うため、メタデータ入りの library.json が必要です。

## 対応エンドポイント

ping / getLicense / getUser / getMusicFolders / getOpenSubsonicExtensions / getIndexes / getArtists / getArtist / getArtistInfo / getArtistInfo2 / getMusicDirectory / getAlbum / getAlbumInfo / getAlbumInfo2 / getAlbumList / getAlbumList2 / getGenres / getSongsByGenre / search2 / search3 / getSong / getCoverArt / stream / download / scrobble / star / unstar / setRating / savePlayQueue / getStarred / getStarred2 / getPlaylists / getScanStatus

- `search2` / `search3` の `song` は常に空です。曲名の全文検索には10万アルバム分のトラック一覧が必要で、index には入っていません（アルバム名・publisher名は検索できます）。
- `getSongsByGenre` も同じ理由で空を返します。ジャンルはアルバム単位で `getAlbumList2?type=byGenre` を使ってください。

## 注意点

- ライブラリは 2026-09-01 時点のスナップショット。更新する場合は khinsider-index 側の `scripts/crawl_live.py`（アルバム一覧）と `scripts/crawl_album_meta.py`（メタデータ）で再クロールして library.json を差し替える
- アルバム初回アクセス時は実ページ取得のため数百ms〜1秒程度の遅延あり（以後はキャッシュ）
- 外部公開する場合は HTTPS 化（Caddy等）と強いパスワードを推奨。Subsonic の legacy `p=` 認証は平文
