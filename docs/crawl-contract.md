# Crawl efficiency compatibility contract

## Scope and safety
This migration preserves library.json, four-column song TSV, Subsonic IDs,
existing retry/deadline/checkpoint behavior, and legacy fallback inputs.
No song/album-wide CDN hash guessing, JS execution, RSS-only discovery,
unbounded parallelism, or automatic deletion from one missing sweep.

## Shared player module
`khinsider_player.py` is identical in index/scripts and relay root.
`extract_player_urls(html: str, slug: str) -> dict[str, str]` maps page songid
to the observed HTTPS MP3 URL. Empty mapping means unsupported/malformed
player, requiring the existing real song-page resolver. `valid_mp3_url(url,
slug) -> bool` validates candidate cached URLs. Callers join songid from the
same album's songlist DOM, never positional/hash guessing. Keep FLAC and
other formats on real per-song-page extraction.

## Canonical album metadata
Keep all existing album-meta.ndjson fields. Add `tracks_complete: true` and
`tracks: [...]` only for a validated, nonempty songlist. Each track keeps
`basename`, `num`, `disc`, `title`, `duration`, `sizes`, `formats`, and adds
`songid` (string when present), `mp3_url` (observed URL when available).
`crawled_at` remains UTC ISO8601 seconds. An HTTP 200 with absent/malformed
songlist is a retryable parse error, not a successful zero-track record.
Old records without tracks remain compatible; no immediate refetch of all
previous successes. New records supersede old album track sets atomically
for index generation; don't keep removed/renamed old tracks for that album.
The existing newline slug file plus `--refresh --retry-failures --order file`
CLI is the integration surface for a recent-update queue. Preserve caller
argument compatibility. Provide optional `--refresh-days N` (default 0,
disabled): permit bounded recrawl of records older than N days; workflow
owner can use a capped separate queue to avoid interfering with backfill.

## Song artifact manifest
Keep four TSV columns: album, disc, track number, actual displayed title.
`build_song_index.py --metadata PATH` is additive and optional. Prefer latest
complete canonical tracks over cached 2023 titles and filename reconstruction;
retain legacy sources for albums without complete current records.
CLI defaults and legacy inputs remain usable. Deterministic sorted rows,
UTF-8 LF, tabs/newlines in fields sanitized, gzip mtime=0/filename=''.
Manifest `songs-index.json` retains existing fields and adds:
- `schema_version`: 1 (TSV compatibility version, not build time)
- `content_sha256`: SHA256 hex of exact decompressed TSV bytes
- `sha256`: SHA256 hex of gzip bytes
Existing `songs`, `albums`, `bytes_raw`, `bytes_gzip` keep their meanings.
Generation time/timing are diagnostics, excluded from content equality.
An explicitly requested missing input is an error; preserve last-good output
on failure. Avoid silently publishing a partial legacy half; add explicit
`--allow-partial` override if needed for controlled bootstrap tests.
Relay may skip the big download only for a valid manifest matching current
source URL + local schema + stored content hash. Manifest absent/invalid or
custom URL without manifest falls back to conditional gzip download.
Verify newly downloaded TSV hash against manifest before replacement. On
mismatch/transient failure, keep the serving last-good DB. No zero-row DB.

## Discovery and publication
Parse every homepage date table, with date watermark and overlap. Persist
seen versions AND pending failures separately; observing an event is not
completion. Acknowledge only a corresponding successful `crawled_at` after
its discovery. Carry pending work across runs. A known/replaced slug must
bypass permanent legacy skip flags. Do not classify all homepage events as
new album creations. Use full sweeps for reconciliation; page-only changes
still need capped TTL recrawls. Stage full-list generations separately;
missing album table is not an empty success, and incomplete sweeps cannot
replace last-good list. Keep reusable checkpoints of unfinished generations.
Daily workflow prefers incremental discovery, with weekly/manual full
reconciliation and bootstrap when no valid list/facet snapshot exists.
Keep existing backfill, resource limits, concurrency group and publication
coverage gates. Include album-list.pages and pending discovery state in
checkpoint assets. Restore failure must not reset to empty as if bootstrap.
Do not change library album API or publish full per-track data in library.json.
Library content identity excludes generation/version/progress diagnostics;
when unchanged, skip release creation. Use deterministic compression.
Weekly song workflow restores canonical metadata as an additional input,
compares content_sha256 with published song manifest, skips unchanged upload.
Wayback workflow needs scripts directory/dependencies for canonical reuse.
Old manual-only workflows stay manual; don't add expensive live PR triggers.

## Incremental discovery CLI

```sh
python scripts/crawl_recent.py --state recent-state.json \
  --out recent-albums.ndjson --queue recent-slugs.txt \
  --metadata album-meta.ndjson --overlap-days 3 --max-pages 10 \
  --deadline-minutes 5
```

These are the default paths and limits. `--ack-only` performs no network
requests: it only acknowledges metadata successfully crawled after discovery
and rewrites the pending queue. Missing metadata cannot acknowledge pending
work. Recent NDJSON rows keep the listing schema plus `listed_at` and
`discovered_at`. `build_library.py --recent PATH` merges these rows into the
baseline without exporting track arrays.

Full sweeps keep unfinished generations separate from live outputs. A retry
of `--fresh` resumes the unfinished generation instead of discarding it.
Listing generations are bound to their source path. Missing, inconsistent or
foreign staging checkpoints fail closed and are preserved: move the whole
generation aside, or choose a different `--out`, to start again. Completed
staging release assets are removed only after the replacement checkpoint is
uploaded, so the next restore cannot resurrect a finished generation.
Restoration errors are not initial bootstrap, and checkpoint publication must
not replace remote state after a failed restore.

## Wayback reuse and relay migration

Wayback's `METADATA_FILE` defaults to `album-meta.ndjson`; it can reuse complete
canonical data instead of repeating an album request. Only observed per-track
URLs are accepted. Old guessed direct-link records do not count as successful
v2 resolution. Audio bodies are not downloaded by these discovery routines.

Relay's `SONGS_MANIFEST_URL` defaults to the paired `songs-index.json` only
when `SONGS_URL` uses the default distribution. A custom songs URL needs an
explicit matching manifest URL; setting the manifest URL to an empty string
disables manifest lookup. Missing or invalid manifests fall back to gzip
validation. Legacy SQLite metadata may require a one-time index re-download
or rebuild; deleting the persisted volume is not required. Album caches
upgrade lazily when accessed. Existing Subsonic IDs and four-column TSV
readers remain supported.
A songs database is not served under a different source/schema, or while its
legacy source binding remains unverified. During a failed first migration,
song search can remain unavailable until validation succeeds; the database
file is retained. A failed library-source refresh retains the last-good
library and retries without forwarding the previous source's validators.

## Validation

Run offline pytest suites and fatal Ruff checks in each repository. Fixtures
are stored inside the repositories; no external sandbox paths or live audio
requests are required. Producer/consumer changes must preserve the manifest
contract above and keep the two shared player-decoder copies identical.
