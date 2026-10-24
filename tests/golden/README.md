# Golden-output harness for the metadata WRITE stage

Goal: diff a future Python implementation byte-for-byte against the bash implementation
(`write-metadata` in `functions.sh`, driven by the write loop of `animes-renamer.sh` /
`movies-renamer.sh`). Fully offline: no Plex, no AniList, no MAL, no downloads.

## Layout

| path | purpose |
|------|---------|
| `make_fixture.py --out DIR` | builds a fake `SCRIPT_FOLDER` (deterministic, python3 stdlib, mtimes = now so every cache is fresh) |
| `run_bash.sh` | reference: sources the real `functions.sh`, runs only the write loop |
| `compare.sh IMPL...` | runs all scenarios with `IMPL`, diffs against `expected/<scenario>.yml` |
| `scenarios/_base.env`, `scenarios/<name>.env` | scenario = `_base.env` + overrides (+ `GOLDEN_MEDIA`) |
| `expected/*.yml` | golden outputs generated from the CURRENT bash code |
| `fakebin/curl`, `fakebin/sleep` | put first on `PATH`; log + fail. Any log entry fails the scenario |
| `BASELINE.md` | fork count / wall time of the bash implementation |

```
tests/golden/compare.sh -w python3 tools/write_metadata.py          # cold + warm fragment cache (2nd run must hit)
tests/golden/compare.sh tests/golden/run_bash.sh                    # bash vs golden (must pass)
tests/golden/compare.sh python3 tools/write_metadata.py             # the port
tests/golden/compare.sh -s default -s movies python3 tools/...      # subset
tests/golden/compare.sh -u tests/golden/run_bash.sh                 # regenerate golden (bash ONLY)
tests/golden/run_bash.sh --scenario default --output out.yml        # standalone single run
```

`compare.sh` exits 0 only if every scenario is byte-identical and no network/sleep was attempted.
Failures keep `tests/golden/.work/run-PID/<scenario>/{diff.txt,stderr.log,fixture/}`.

## Interface a Python implementation must provide

```
python3 tools/write_metadata.py --script-folder DIR --media animes|movies --output FILE
```

* Run with cwd = a scratch dir, `LC_ALL=C.utf8`, fake `curl`/`sleep` first on `PATH`.
* **All settings come from the process environment** (same names as `config/.env`; see
  `scenarios/_base.env`): `RATING_1_SOURCE RATING_1_TYPE RATING_2_SOURCE RATING_2_TYPE ALLOW_RENAMING
  MAIN_TITLE_ENG SORT_TITLE_ENG ORIGINAL_TITLE_NATIVE RENAME_SEASONS REDUCE_TITLE_CAPS DISABLE_TAGS
  TAG_SOURCE ADD_ANIME_TAG ANILIST_TAGS_P POSTER_* IGNORE_SEASONS IGNORE_SEASONS_RATING SEASON_YEAR
  IGNORE_S1_ONLY_RATING ANIME_AWARDS ANIME_AWARDS_NO_FVA ANILIST_LISTS ANILIST_USERNAME
  ANILIST_LISTS_LEVEL CACHE_TIME_* DATA_CACHE_TIME`.
  Empty-but-set values matter (`RATING_2_SOURCE=` disables rating 2; see quirks).
  `LOG_FOLDER` / `ASSET_FOLDER` are not exported (defaults: `DIR/config/logs`, `DIR/pmm/assets`).
* Inputs are read from `DIR` only (files below); `DIR/VERSION` supplies `version=` for the first line
  `# Romaji-Renamer v<version>`.
* Must write exactly the YAML bash would have produced to `--output`; stdout/stderr ignored, exit 0.
* Whether the port still touches posters is not observable in the output; it just must never download
  (fixture posters all exist and are >= 10000 bytes; caches are fresh).

### Fixture files consumed (`DIR/`)

```
VERSION                                       version=0.0.0-golden
config/ID/animes.tsv   tvdb  anilist  plex_title  asset_name  seasons_list  override_seasons_ignore
config/ID/movies.tsv   imdb  anilist  plex_title  asset_name            (override rows: imdb <empty> anilist title asset)
config/override-ID-animes.tsv  tvdb anilist title studio ignore_seasons notes     (has header row)
config/override-ID-movies.tsv  imdb anilist title studio notes
config/tmp/anime-mapping.tsv   anilist  tvdb  tvdb_season  epoffset("cour order")  mal
config/tmp/anilist-mal.tsv     anilist  mal      (fallback when json idMal is null)
config/data/anilist-<id>.json, MAL-<id>.json, cr-award.json, ongoing.tsv (tvdb ids)
config/data/anilist-<user>-{Completed,Watching,Dropped,Paused,Planning}.tsv   (one comma-separated line)
```

## Scenarios (21)

Each = `_base.env` overridden by the listed vars. Media is `animes` (20 shows, tvdb 1001-1020) unless noted.

| scenario | overrides |
|----------|-----------|
| default | (none) |
| main_title_eng | `MAIN_TITLE_ENG=Yes` |
| mal_tags_rating1 | `TAG_SOURCE=MAL RATING_1_SOURCE=MAL` |
| rating_swap | `RATING_1_SOURCE=MAL RATING_1_TYPE=user RATING_2_SOURCE=ANILIST RATING_2_TYPE=audience` |
| ignore_seasons | `IGNORE_SEASONS=Yes` |
| anilist_lists_both | `ANILIST_LISTS=Yes ANILIST_LISTS_LEVEL=both` |
| anilist_lists_season | `ANILIST_LISTS=Yes ANILIST_LISTS_LEVEL=season SEASON_YEAR=Yes` |
| anilist_lists_show | `ANILIST_LISTS=Yes ANILIST_LISTS_LEVEL=show` |
| season_year | `SEASON_YEAR=Yes` |
| disable_tags | `DISABLE_TAGS=Yes` |
| no_renaming | `ALLOW_RENAMING=No ADD_ANIME_TAG=No` |
| title_variants | `SORT_TITLE_ENG=Yes ORIGINAL_TITLE_NATIVE=No RENAME_SEASONS=No REDUCE_TITLE_CAPS=No ANILIST_TAGS_P=50` |
| rating2_off | `RATING_2_SOURCE= RATING_2_TYPE=` |
| rating2_off_ignore_seasons | `IGNORE_SEASONS=Yes RATING_2_SOURCE= RATING_2_TYPE=` |
| ignore_seasons_rating | `IGNORE_SEASONS_RATING=Yes SEASON_YEAR=Yes` |
| s1_only_rating | `IGNORE_S1_ONLY_RATING=No SEASON_YEAR=Yes` |
| awards_off_fva | `ANIME_AWARDS=No ANIME_AWARDS_NO_FVA=No` |
| awards_with_fva | `ANIME_AWARDS_NO_FVA=No SEASON_YEAR=Yes` |
| poster_mal | `POSTER_SOURCE=MAL` |
| movies | `GOLDEN_MEDIA=movies` (6 movies) |
| movies_mal_lists | movies + `MAIN_TITLE_ENG=Yes TAG_SOURCE=MAL RATING_1_SOURCE=MAL RATING_2_SOURCE=ANILIST ANILIST_LISTS=Yes ANILIST_LISTS_LEVEL=both ANIME_AWARDS_NO_FVA=No` |

To add a scenario: drop `scenarios/<name>.env`, run `compare.sh -u -s <name> tests/golden/run_bash.sh`.

## Fixture coverage (see comments in `make_fixture.py`)

Single / multi-season (`0,1,2,3`) / multi-cour seasons (offsets 0,100,12) / season 0 / only-season-2;
NOT_YET_RELEASED show and cours; ongoing (`ongoing.tsv`); sequel chains of several hops, branching
(multi->single, multi->multi), a 2-node cycle (50-hop cap), non-TV sequel formats; null english/native
titles; ALL-CAPS titles (`86 EIGHTY-SIX`, `POKÉMON`); special characters in titles/studios; score 0;
awards on main id / last cour / foreign-voice-actor only; overrides with/without title/studio,
`ignore_seasons=yes`, override with last season 1 (not in mapping); tvdb absent from mapping; mapping
season -1; no MAL id; idMal via `anilist-mal.tsv`; seasonYear/season/startDate fallbacks; userlists;
movies incl. override rows in the movies-renamer.sh column layout.

NOT covered (would call curl in bash, so impossible offline): `averageScore: null` or MAL `score: null`
(bash deletes the cache file and re-downloads), missing json cache files, stale caches, missing posters.
The port should mimic "refetch" by whatever its design is; it is untested here.

## Bash quirks a byte-exact port must reproduce (all observed in the goldens)

Ratings
1. **`score_1_season` typo** in the RATING_2 branch of `get-season-infos`: when a season's
   `total_2_cours_score == 0`, it sets `score_1_season=0` and `score_1_no_rating_seasons++`
   (not the `_2_` variables). Effects: that season's `user_rating` prints `0`, rating 1 excludes the
   season, and `score_2_season` keeps the value from the previous season (initially 0), which is then
   added again to `total_2_score` while `score_2_no_rating_seasons` is not incremented. (show 1002 S2)
2. Rating averages use awk (`%.6g` output) then `printf '%.*f' 1` from **bash, which uses long double**:
   `6.45->6.4, 7.55->7.6, 7.95->7.9, 0.15->0.2` where Python float formatting gives 6.5/7.5/8.0/0.1.
   Emulate by parsing the decimal string as an x87 80-bit value (64-bit mantissa, round-to-nearest-even)
   then rounding that exact value half-even to 1 decimal. AniList score is `score/10` via awk `%.6g`.
3. Rating `0` (string) means "no rating" and the line is omitted; seasons with cours all
   NOT_YET_RELEASED/0 are excluded from averages by counters. `total_seasons` drops 1 only if the
   *first* listed season is 0.
4. `get-mal-score` / `get-mal-tags` with no MAL id **leave `anime_score` / `anime_tags` from the previous
   show/call** (stale global; logs "Missing MAL ID"). Reproduce: globals persist across shows and
   across Anilist/MAL (shows 1012, tt0000004).
5. `RATING_2_SOURCE=""` : `get-rating-2` (IGNORE_SEASONS / movies path) still prints
   `    _rating: 0.0` (empty type, `score_2` unset -> `printf %.1f ""`). In the seasons path
   `check-rating-2-valid` prints nothing.
6. Without seasons (not in mapping, mapping season -1, `IGNORE_SEASONS`, override `yes`), ratings come
   from the show's own anilist id; `seasons:` block absent.

Titles / tags / misc
7. `less-caps-title`: applies only if after stripping non `[a-zA-Z ]` and spaces the remainder is
   `^[A-Z]+$`; then lowercases + capitalises first letter after a space/start => `86 Eighty-six`
   (NOT `86 Eighty-Six`). Applies to romaji and english (and override titles), not native.
   In `C.utf8`, non-ASCII letters are stripped by the first sed and left untouched by `tr`:
   `POKÉMON -> PokÉmon`. `en_US.utf8` may give different output, so the locale is pinned to `C.utf8`
   (the real scripts use the first `locale -a | grep -i utf`).
8. `null` is compared as a string: english null -> romaji title (after caps reduction); native null ->
   romaji title. Override title replaces both romaji and english (native untouched, original_title
   still native).
9. Tags: genres then tags with `rank >= ANILIST_TAGS_P` (inclusive), joined by `,`; empty ->
   `genre.sync: Anime,` (trailing comma). MAL tags: genres, demographics, themes.
10. Studio: first `isAnimationStudio==true`, else first studio, else no `studio:` line; override
    studio wins only if non-empty.
11. `get-animes-season-year`: `"<seasonYear> <season>"` lowercased then capitalised per word
    (`2020 Fall`). If BOTH are null -> startDate fallback, month via `[[ -le ]]` arithmetic where
    `null` evaluates to 0 => `Winter`; year null prints `null`. If only one is null you get
    `2020 Null`.
12. Multi-cour season labels: cours are processed in `sort -s -k1,1` order of epoffset as **strings**
    (`0,100,12`), per-cour season strings are **prepended** (`2020 Winter,2019 Summer,2019 Spring`),
    NOT_YET_RELEASED cours `continue` but still advance `cours_count`, so a skipped first cour leaves a
    trailing comma (`2023 Fall,`). `sort -s` stable for equal offsets; locale irrelevant only for
    digit strings (pinned to C.utf8 anyway).

Labels / lists / awards
13. Userlist accumulation bug: `userlist_type_count > 1` is never true for a single prior entry
    (`awk NF` of `Completed` is 1), so each further matching list **replaces** the previous one;
    effectively the last matching list in order Completed, Watching, Dropped, Paused, Planning wins,
    while the *remove* list drops every matching type. Removal is `sed s/X//; s/^,//; s/,,/,/g` which
    leaves a **trailing comma** if the removed item was last (`Completed,Watching,Dropped,Paused,`).
14. Show-level list match: `grep -q -w "id1\|id2\|..."` over all anilist ids mapped to the tvdb id
    (includes season-0 ids), against a one-line comma list; `-w` = word boundaries (digits/letters/_),
    so `12` does not match `123`. Season-level matches per cour id.
15. Awards: `> /dev/null` on the `award_check=$(...)` assignments is a no-op. `cr_awards` is from the
    **last** `get-animes-award` call (last processed cour of the last season; a NOT_YET_RELEASED cour
    is skipped before the call). If empty, `write-metadata` calls `get-animes-award` for the
    main anilist id **regardless of `ANIME_AWARDS`** (ANIME_AWARDS=No only disables the per-season
    labels). With `ANIME_AWARDS_NO_FVA=Yes` entries whose `cr_award` contains English/Arabic/Spanish/
    Castilian/French/German/Italian/Portuguese/Russian are dropped. Label is just `AA Winner`.
16. Season label rules: see the `IGNORE_S1_ONLY_RATING` / `IGNORE_SEASONS_RATING` / `last_season -eq 1`
    branches; `Score` is added/removed accordingly, season 0 always prints `label.remove: Score`.
17. Order of keys inside a show: title/sort_title/original_title, genre.sync, studio, `seasons:` block,
    rating line(s), airing/award/list `label` then `label.remove`.

Airing status (`get-airing-status`)
18. `ongoing.tsv` (tvdb ids) -> `Airing`. Else status NOT_YET_RELEASED of the main anilist id -> `Planned`.
    Else follow SEQUEL edges whose node `format` is TV/ONA/MOVIE/OVA/null (others ignored); any
    sequel node whose status is NOT_YET_RELEASED => `Planned`; no qualifying sequel => `Ended`;
    cap 50 hops => `Ended` (cycles).
19. Multiple sequels: `wc -l` on `printf %s` of ids; the first multi-hop writes the id file **with** a
    trailing newline, but a multi->multi step writes it **without**, so `while read` **skips the last
    id** (show 1006: the dropped branch contains the NOT_YET_RELEASED sequel, so bash says `Ended`).
    In the multi step all listed ids' JSON are concatenated and sequels of any of them are followed.
    When only the first of several ids is followed, `anilist_id=head -n 1` is used for the next hop.

Input parsing
20. `while IFS=$'\t' read` collapses consecutive tabs (tab is IFS-whitespace): empty fields in the middle
    vanish and leading/trailing whitespace is trimmed. Movie override rows are written as
    `imdb<TAB><TAB>anilist<TAB>title<TAB>asset` by movies-renamer.sh (mal_id empty) and only work because
    of this collapsing. A 6th animes.tsv column is the override `ignore_seasons` flag; only the literal
    `yes` counts. Non-override rows carry an empty 6th field.
21. Override lookups are by **anilist id in column 2** of the override tsv, for every call
    (`get-romaji-title`, `get-english-title`, `get-studios`) — also for season anilist ids. An override
    with `last_season == 1` forces `valid_anilist_id=1` and replaces season 1's cour list with the
    override id (works even when the tvdb id is absent from the mapping).
22. `get-season-infos` leaves `IFS=","` set for the rest of the process (harmless to output so far).
23. `season_check` = mapping `tvdb_season` of the show anilist id; `-1` => plain (non-season) path.
    `valid_anilist_id` empty (tvdb id absent from mapping, no override hack) => plain path.
24. Posters / caches have no effect on the YAML in the offline fixture; `get-score` / `get-mal-score`
    would delete + refetch on `null` (not testable offline).
