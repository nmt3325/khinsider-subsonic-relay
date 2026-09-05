# khinsider-subsonic-relay

Subsonic API-compatible relay server that publishes all of downloads.khinsider.com as a music library — without hosting a single audio file.

## 概要

downloads.khinsider.com の全アルバム（104,607件 / 2026-09-04時点）を Subsonic 互換ライブラリとして公開するリレーサーバーです。音声ファイルは一切保持せず、クライアントからの再生リクエスト時に khinsider の CDN（vgmtreasurechest.com）上の実URLへ **302リダイレクト** します。サーバーには帯域もストレージもほぼ不要です。

## 動作の仕組み

1. アルバム一覧は [khinsider-index](https://github.com/nmt3325/khinsider-index) のライブクロール成果（library.json）を初回起動時に自動ダウンロード
2. アルバム詳細・トラック一覧・メタデータ・実ファイルURLはクライアントからのアクセス時にオンデマンドで実サイトから取得してキャッシュ（Cloudflare対策として curl_cffi の Chrome TLS フィンガープリントを使用）
3. `/rest/stream` は実ファイルの CDN URL へ 302 リダイレクト。リダイレクト非対応クライアント向けに `PROXY_STREAM=1` でサーバー中継モードも可
4. 曲名検索用に、325万曲の曲名インデックスを別途ダウンロードしてローカルの SQLite FTS5 に展開（[曲名検索](#曲名検索)）

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

## 曲名検索

`search2` / `search3` はアルバム名・publisher 名だけでなく **曲名**でも検索できます。曲名インデックス（`songs.tsv.gz` / 約33MB / 325万曲・10.5万アルバム）を khinsider-index の `song-index` リリースから初回起動時にバックグラウンドでダウンロードし、SQLite の FTS5（trigram トークナイザ）でローカルにビルドします。実装は `songs.py`。

- ビルドが終わるまで曲検索は空を返します（アルバム・アーティスト検索は起動直後から使えます）。ビルドは4コア程度で30秒前後、DB は約600MB
- trigram なので **部分一致と日本語**が効きます（`ノクターン` で `風のノクターン` もヒットする）。逆に3文字未満のクエリは扱えないため無視します
- インデックスは候補アルバムを絞るためだけに使い、ヒットした曲は必ず実際のアルバムページ（30日キャッシュ）と突き合わせてから返します。曲名が一致しなければ曲番号で解決し、それでも合わなければその候補は捨てます。返る `id` は常に `getSong` / `stream` がそのまま扱えるものになります
- 実サイト40アルバムでの実測では、インデックスの曲名は実際の表示名と95%前後（アルバム平均）一致し、曲番号フォールバックを含めて97%（2026年クロール分）/ 93%（2023年キャッシュ分）を解決できました
- 1クエリで実ページを取得するアルバム数は `SONG_SEARCH_ALBUM_LIMIT`（既定12）まで。初回は1秒程度、ページがキャッシュ済みなら数十ms
- 曲検索が不要なら `SONG_SEARCH=off` でダウンロードもビルドも行いません

## 起動

Docker Compose:

```sh
docker compose up -d
```

同梱の `docker-compose.yml` は `khinsider-data` という named volume を `/data` にマウントし、library.json（約24MB）・曲名DB（約600MB）・ページキャッシュをまとめてそこに置きます。コンテナを作り直しても再ダウンロードや再ビルドは走りません。インデックス更新中は新旧2世代が一時的に並ぶので、1.5GB ほど空きを見てください。ホストから直接見えるディレクトリに置きたい場合は `volumes:` の指定を `- ./data:/data` に差し替えます。

ベアメタル:

```sh
pip install -r requirements.txt
SUBSONIC_USER=myuser SUBSONIC_PASSWORD=secret uvicorn server:app --host 0.0.0.0 --port 8080
```

`GET /`（認証なし）でライブラリ件数・メタデータ付き件数・publisher 数・ジャンル数・現在のモードに加え、library と曲名インデックスの取得状況（`library` / `songIndex`）を確認できます。

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
| `LIBRARY_PATH` | `./library.json` | ライブラリデータのパス（`.gz` も可）。Docker では `/data/library.json` |
| `LIBRARY_URL` | `.../releases/latest/download/library.json` | library.json の取得元。常に最新のリリースを指す |
| `LIBRARY_REFRESH_HOURS` | `24` | 稼働中に library.json を再チェックする間隔（時間）。`0` で無効。ETag / Last-Modified 付きの条件付き GET なので、更新が無ければ 304 が返るだけで再構築もしません |
| `LIBRARY_MAX_AGE_HOURS` | `LIBRARY_REFRESH_HOURS` | 起動時にキャッシュを再取得する古さのしきい値。旧来の名前で、通常は指定不要 |
| `CACHE_DIR` | `./cache` | ページキャッシュ先（アルバム30日）。Docker では `/data/cache` |
| `PROXY_STREAM` | - | `1` で302ではなくサーバー中継 |
| `GENRE_SOURCES` | `platform,album_type` | ジャンルに使う項目と順序。`album_type,platform` や `album_type` 単独も可 |
| `ARTIST_MODE` | `auto` | `auto`（メタデータがあれば publisher）/ `publisher` / `letter` |
| `FALLBACK_ARTIST` | `KHInsider` | publisher も developer も無い場合のアーティスト名 |
| `SONG_SEARCH` | `auto` | `off` で曲名検索を無効化（インデックスのDLもビルドもしない） |
| `SONGS_URL` | `.../releases/download/song-index/songs.tsv.gz` | 曲名インデックスの取得元 |
| `SONGS_DB` | `./songs.sqlite` | ビルドした曲名DBの置き場所。約600MB。Docker では `/data/songs.sqlite` |
| `SONGS_REFRESH_HOURS` | `24` | 稼働中に曲名インデックスを再チェックする間隔（時間）。`0` で無効。中身が同じなら再ビルドせず、新しければ裏でビルドしてから原子的に差し替え |
| `SONGS_MAX_AGE_DAYS` | `0` | 内容が変わっていなくても、DBがこの日数より古ければ作り直す（`0` は作り直さない） |
| `SONG_SEARCH_ALBUM_LIMIT` | `12` | 1クエリで実ページを取得する候補アルバムの上限 |
| `SONG_SEARCH_CANDIDATES` | `600` | インデックスから取り出す候補行の上限 |

## ライブラリデータ

`library.json` と `songs.tsv.gz` は khinsider-index 側で生成します。通常は同リポジトリの GitHub Actions（`album-meta.yaml` / `album-meta-residual.yaml` / `song-index.yaml`）が自動で回すので、手で叩く必要はありません。

```sh
# khinsider-index リポジトリで
python3 scripts/crawl_index_pages.py    # 一覧ページ210枚から全アルバムとメタデータを収集
python3 scripts/crawl_facets.py         # publisher / developer / year の逆引きを収集
python3 scripts/build_library.py --gzip # マージして library.json を生成
python3 scripts/build_song_index.py     # 曲名インデックス songs.tsv.gz を生成
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

曲名インデックスは `album<TAB>disc<TAB>track<TAB>title` の gzip TSV です。曲名以外の情報（長さ・サイズ・実URL）は持たず、それらは常にアルバムページから取得します。

## 対応エンドポイント

ping / getLicense / getUser / getMusicFolders / getOpenSubsonicExtensions / getIndexes / getArtists / getArtist / getArtistInfo / getArtistInfo2 / getMusicDirectory / getAlbum / getAlbumInfo / getAlbumInfo2 / getAlbumList / getAlbumList2 / getGenres / getSongsByGenre / search2 / search3 / getSong / getCoverArt / stream / download / scrobble / star / unstar / setRating / savePlayQueue / getStarred / getStarred2 / getPlaylists / getScanStatus

- `search2` / `search3` はアルバム名・publisher 名・曲名を検索します。曲名側の仕組みと制限は「[曲名検索](#曲名検索)」を参照。
- `getSongsByGenre` は空を返します。曲単位のジャンル閲覧には該当ジャンルの全アルバムページを取得する必要があるためです。ジャンルはアルバム単位で `getAlbumList2?type=byGenre` を使ってください。

## 注意点

- library.json と曲名インデックスは稼働中も自動で更新されます。`LIBRARY_REFRESH_HOURS` / `SONGS_REFRESH_HOURS`（既定24時間）ごとに条件付き GET で確認し、中身が変わっていた時だけメモリ上のインデックスと曲名DBを差し替えるので、再起動もダウンタイムも不要です。index 側は library を日次、曲名インデックスを週次で更新します
- アルバム初回アクセス時は実ページ取得のため数百ms〜1秒程度の遅延あり（以後はキャッシュ）
- 曲名DBのビルド中（初回起動から30秒前後）は曲検索だけが空を返します。ディスクは約600MB、インデックス更新中は新旧2世代が並ぶため一時的に約1.2GB必要です
- 外部公開する場合は HTTPS 化（Caddy等）と強いパスワードを推奨。Subsonic の legacy `p=` 認証は平文
