# Incremental cumulative live-data compatibility contract

## Data lineage

A certified full catalogue seeds discovery once. The serving producer retains
and extends it with its own recent-discovery history and complete album records
written by the `khinsider-live-v2` crawler generation. It does not read
legacy `index.json`, old album JSON caches, cached song titles, archival
snapshots or published libraries to supply missing records.

This generation's own validated checkpoints are deliberately accumulated. The
first serving cutover checks only the previous song manifest's row count as a
shrinkage guard; it never imports the previous index rows. Thereafter, the saved
modern publication inventory protects against lost albums. Existing serving data
remains available until a validated replacement is ready.

## Catalogue and complete metadata

`catalogue.json` has `schema_version: 2`, `data_source: khinsider-live-v2`,
`complete: true`, a semantic `catalogue_id`, observation timestamps, the full
listing page set and advertised count, and unique canonical album slugs.
Every row has a valid page and timestamp. Resume preserves the earliest staged
observation. The certified unique count cannot be below the site-advertised
count. Partial or incoherent listing generations are not promoted.

A successful album record has `status: ok`, `http_status: 200`, the same
`data_source`, timezone-aware `crawled_at`, a title, `tracks_complete: true`,
and `track_count == len(tracks) > 0`. Every track has a real displayed title and
basename; present disc numbers are nonnegative integers (including real disc 0),
and present track numbers are positive integers. Booleans are rejected. Song identities
must be unique. The parser checks all DOM song rows, even without player URLs.

HTTP 404 records use `status: gone`, `http_status: 404` and an observation time.
They are listed explicitly as unavailable, not counted as fetched albums. New
update events can requeue them. Empty songlists, invalid JSON, transient
HTTP failures and unmarked/legacy metadata do not count as complete.

Slugs have one canonical percent-encoding layer: decode once, then quote the
single path segment. Album and song filename encoding layers are distinct.
Unicode and combining marks must survive producer/consumer joins.

The crawler records the trusted final album URL as optional `resolved_slug`,
retaining the request slug for acknowledgment. Snapshots deduplicate aliases;
newer terminal observations supersede stale redirects. Homepage/foreign-site
redirects are not album records and do not become invented HTTP 404s.
The relay preserves requested client IDs while using confirmed canonical album
and song-page paths. Non-MP3 resolution does not add an extra album request.

A same-album song-page link in a row with a numeric song ID remains an observed
basename even when the site has truncated or omitted its extension. Preserve it
exactly; do not append an invented extension or silently omit the track.

## Shared player decoder

`index/scripts/khinsider_player.py` and the relay's `khinsider_player.py` remain
byte-identical. `extract_player_urls(html, slug)` statically maps song IDs to
observed HTTPS MP3 URLs. JavaScript packer word boundaries use ASCII semantics,
matching JavaScript rather than Python's default Unicode boundaries.

Unsupported/malformed player code can yield no URLs without losing a valid
complete DOM track list. `mp3_url` is optional. Never execute JavaScript, derive
one track's CDN hash from another, or guess FLAC URLs. Missing MP3 URLs and other
formats use real song pages at resolution time. Discovery does not fetch audio
bodies. Relay album caches are versioned and re-parsed lazily after parser fixes.

## Generated serving artifacts

`library.json` preserves the album-focused API and omits full track arrays.
`library.json.gz` is the compressed equivalent. Both it and `songs-index.json`
declare `dataset_schema_version: 2`, `data_source: khinsider-live-v2`,
`complete: true`, `completeness_scope: cumulative_snapshot`, `catalogue_id`, and
`legacy_inputs: []`. The serving catalogue identity describes the accumulated
registry, not just the unchanged historical seed.

The song TSV remains schema **1**, with four UTF-8, LF-separated columns:

```text
album-slug<TAB>disc<TAB>track-number<TAB>actual-displayed-title
```

No retired-cache union, filename-title reconstruction, partial album track-list
override, or same-title track collapsing is allowed. Disc 0 is serialized as `0`. Latest complete canonical observations
replace the album's entire previous track set. Tabs/newlines are sanitized.
Gzip has `mtime=0` and an empty filename header.

The manifest retains `songs`, `albums`, `bytes_raw`, `bytes_gzip`, `sha256` and
`content_sha256`. It also records the metadata hash, catalogue identity,
unavailable albums, complete coverage and library content hash. Generation
and timing diagnostics do not by themselves trigger a new release.

The shared engine uses cumulative builders: each output includes every valid
accumulated canonical album, never only the current run's delta. Failed refreshes
retain last-good complete records. Missing new albums stay queued without
blocking other updates. Invalid records, empty snapshots, inconsistent outputs,
and unexplained loss of previously published albums still fail closed. Standalone
builders without `--cumulative` retain strict certified-catalogue checks.
Optional metadata fields may be empty; full coverage is not a promise that all
publishers/developers/dates exist, or that field richness exceeds old datasets.

## Discovery, checkpoints and publication

A full listing is needed only at cold bootstrap. Daily/backfill runs discover
homepage changes with a three-day overlap and immediately queue new slugs, even
outside the seed. Recent scan caps/deadlines retain a page cursor; the watermark
only moves forward on a completed scan. Acknowledgment requires a later validated
observation. Failed requests back off exponentially from 1 to 24 hours; newer
events override the delay. Unstarted requests are not failures. The weekly
rebuild consumes the full accumulated snapshot without website crawling.

`complete: true` in a serving artifact means the full accumulated snapshot,
not complete website collection. `crawl_complete` describes the known metadata
queue; job `discovery_complete` separately describes the recent scan window.
Pending requests can include refreshes whose last-good records remain served.
A capped discovery window does not block an otherwise valid snapshot. Coverage
of the released library is not a promise of 100% website coverage or instant
detection of unannounced edits.

The single shared engine serializes all serving writers. `live-crawl-v2` is a
prerelease state tag, not a library release. Its `checkpoint.tar.gz` has a
source/schema marker and per-file size/hash descriptor, and permits only known
regular files. Restore stages and validates before promotion. Only a genuinely
missing release is a bootstrap; missing assets and transport/auth failures fail
closed. Checkpoint save never runs after unsuccessful restore.

A bounded run may publish the entire valid accumulated snapshot while still
retaining unresolved requests. Retry and publication inventory state are saved
in the existing checkpoint. Equal serving contents skip publication; remembered
metadata/registry inputs also avoid unnecessary repeated full rebuilds.
Both serving outputs and their stored artifact files must validate before any
release write. All four artifacts are uploaded to a draft library release;
the compatible `song-index` payload is updated before its manifest, and the
library release is then promoted to latest. GitHub provides no global
multi-release transaction. Existing releases are not deleted by this engine.

## Relay transition and archival separation

Existing default library/song/manifest URLs and Subsonic IDs remain supported.
Custom song URLs do not silently inherit the default manifest URL. Explicitly
empty manifest configuration retains compatibility with standalone TSV sources.
Existing legacy serving formats remain readable during initial collection;
they are never consulted by the new producer to fill holes.

A declared live-v2 library or manifest must be complete and declare no legacy
inputs. A rejected live-v2 manifest cannot be swallowed into the optional
legacy-manifest fallback: retain the last-good SQLite database instead. Verify
payload hashes before replacing the database; never replace it with zero rows.
Keep source-bound cache and conditional-request behavior for compatible inputs.

Wayback direct-link discovery requires the complete modern catalogue/metadata
and streams selected track records. It does not use the old root index to
choose albums. Existing archival queue/submission history is retained separately
and is never an input to either serving builder. Archive submission is not part
of the serving dataset's completeness requirement.

## Validation and completion claims

Run offline regression suites, fatal lint and workflow validation. Also verify
modern builders against the actual relay reader/search path. Small fixture
catalogues are testing scopes, not publishable full-site acquisition evidence.
Report these separately: code tested/landed; full album/track acquisition;
complete release publication; uptake by the running relay.
