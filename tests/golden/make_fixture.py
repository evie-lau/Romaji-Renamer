#!/usr/bin/env python3
"""Build a self-contained, offline fake SCRIPT_FOLDER for the write-metadata stage.

Usage: make_fixture.py --out DIR [--username NAME]

Everything is deterministic except file mtimes, which are set to "now" so that
every cache file is fresh (cache-is-fresh in functions.sh). Posters are
pre-created (>= 10000 bytes) so no download is ever attempted.

NOTE: AniList `averageScore: null` / MAL `score: null` are deliberately not used:
the bash code reacts to them by deleting the cache file and calling curl.
A score of 0 exercises the "no rating" path offline.
"""
import argparse
import json
import os
import shutil

FINISHED, RELEASING, NYR = "FINISHED", "RELEASING", "NOT_YET_RELEASED"

anilist = {}   # id -> media dict
mal = {}       # mal id -> data dict
awards = []    # cr-award.json rows
mapping = []   # anime-mapping.tsv rows (anilist, tvdb, season, offset, mal)
anilist_mal = []  # anilist-mal.tsv rows (anilist, mal)
animes = []    # (tvdb, anilist, title, asset, seasons, flag, override?)
movies = []    # (imdb, anilist, title, asset, override?)
ov_animes = []  # (tvdb, anilist, title, studio, ignore, notes)
ov_movies = []  # (imdb, anilist, title, studio, notes)
ongoing = []
lists = {"Completed": [], "Watching": [], "Dropped": [], "Paused": [], "Planning": []}
assets = set()


def ANIM(n):
    return (n, True)


def OTHER(n):
    return (n, False)


def media(aid, romaji, english="*", native="*", score=70, status=FINISHED,
          genres=("Action",), tags=(), studios=(ANIM("Studio Alpha"),),
          season=None, season_year=None, start=(None, None), sequels=(),
          mal_id=None):
    if english == "*":
        english = romaji + " (EN)"
    if native == "*":
        native = "native-" + str(aid)
    anilist[aid] = {"data": {"Media": {
        "relations": {"edges": [
            {"relationType": rt, "node": {"id": nid, "type": "ANIME", "format": fmt,
                                           "title": {"romaji": "rel-%s" % nid}, "status": st}}
            for (rt, nid, fmt, st) in sequels]},
        "title": {"romaji": romaji, "english": english, "native": native},
        "averageScore": score,
        "genres": list(genres),
        "tags": [{"name": n, "rank": r} for n, r in tags],
        "studios": {"edges": [{"node": {"name": n, "isAnimationStudio": a}} for n, a in studios]},
        "startDate": {"year": start[0], "month": start[1]},
        "season": season,
        "seasonYear": season_year,
        "coverImage": {"extraLarge": "https://img.invalid/%s.jpg" % aid},
        "status": status,
        "idMal": mal_id}}}


def seq(nid, fmt="TV", st=FINISHED, rt="SEQUEL"):
    return (rt, nid, fmt, st)


def malinfo(mid, score, genres=(), demos=(), themes=()):
    mal[mid] = {"data": {
        "score": score,
        "genres": [{"name": n} for n in genres],
        "demographics": [{"name": n} for n in demos],
        "themes": [{"name": n} for n in themes],
        "images": {"jpg": {"large_image_url": "https://img.invalid/mal-%s.jpg" % mid}}}}


def tags(*pairs):
    return list(pairs)


def add_anime(tvdb, aid, title, seasons, asset=None, flag="", override=False):
    asset = asset or "%s (%s)" % (title, tvdb)
    animes.append((tvdb, aid, title, asset, seasons, flag, override))
    assets.add(asset)


def add_movie(imdb, aid, title, override=False, asset=None):
    asset = asset or "%s (%s)" % (title, imdb)
    movies.append((imdb, aid, title, asset, override))
    assets.add(asset)


def build():
    global mapping
    # 1. simple single season, award, Completed, non-animation studio listed first
    media(2001, "Tanjun na Single", "Simple Single", "シンプルシングル", 78,
          genres=("Action", "Comedy"),
          tags=tags(("Male Protagonist", 80), ("Parody", 60), ("Super Power", 70)),
          studios=(OTHER("Producer Co"), ANIM("Animation Studio X"), ANIM("Second Anim")),
          season="FALL", season_year=2020, start=(2020, 10), mal_id=6001)
    malinfo(6001, 7.84, ("Action", "Comedy"), ("Shounen",), ("Parody",))
    mapping.append((2001, 1001, 1, 0, 6001))
    add_anime(1001, 2001, "Simple Single", "1", "Simple Single (2020)")
    awards.append({"anilist_id": "2001", "year": "2021", "cr_award": "Anime of the Year"})
    lists["Completed"].append(2001)

    # 2. multi-season 0,1,2,3, rating-2 quirk (MAL score 0 on S2), award on last season, Planned sequel
    media(2101, "Multi Season", "Multi Season", "マルチ", 82, genres=("Drama",),
          season="WINTER", season_year=2018, sequels=(seq(2102),), mal_id=6101)
    media(2102, "Multi Season 2nd", "Multi Season 2", "マルチ2", 75, genres=("Drama",),
          season="SUMMER", season_year=2019,
          sequels=(seq(2103), seq(2150, "MANGA", RELEASING, "ADAPTATION")), mal_id=6102)
    media(2103, "Multi Season 3rd", "Multi Season 3", "マルチ3", 91, genres=("Drama",),
          season="SPRING", season_year=2021, sequels=(seq(2104, "TV", NYR),), mal_id=6103)
    media(2104, "Multi Season 4th", status=NYR, score=0, season=None, mal_id=6104)
    malinfo(6101, 8.3, ("Drama",))
    malinfo(6102, 0, ("Drama",))
    malinfo(6103, 8.05, ("Drama",), (), ("Mecha",))
    malinfo(6104, 0, ("Drama",))
    mapping += [(2199, 1002, 0, 0, ""), (2101, 1002, 1, 0, 6101),
                (2102, 1002, 2, 0, 6102), (2103, 1002, 3, 0, 6103)]
    add_anime(1002, 2101, "Multi Season", "0,1,2,3")
    awards.append({"anilist_id": "2103", "year": "2022", "cr_award": "Best Drama"})
    lists["Planning"].append(2101)
    lists["Watching"].append(2102)
    lists["Dropped"].append(2103)

    # 3. multi-cour seasons; lexicographic offset order (0,100,12); NYR cours; long sequel chain
    media(2201, "Cour Show", "Cour Show", season="SPRING", season_year=2019, score=71,
          sequels=(seq(2203),), mal_id=6201)
    media(2203, "Cour Show Part 2", "Cour Show Part 2", season="SUMMER", season_year=2019,
          score=74, sequels=(seq(2202),), mal_id=6203)
    media(2202, "Cour Show Part 3", "Cour Show Part 3", season="WINTER", season_year=2020,
          score=77, sequels=(seq(2210),), mal_id=6202)
    media(2210, "Cour Show S2", "Cour Show S2", season="FALL", season_year=2021, score=80,
          sequels=(seq(2211, "TV", NYR),), mal_id=6210)
    media(2211, "Cour Show S2 Part 2", "Cour Show S2 Part 2", season="WINTER", season_year=2022,
          score=0, status=NYR, mal_id=6211)
    media(2220, "Cour Show S3", "Cour Show S3", status=NYR, score=0, season="SPRING",
          season_year=2023, mal_id=6220)
    media(2221, "Cour Show S3 Part 2", "Cour Show S3 Part 2", season="FALL", season_year=2023,
          score=66, mal_id=6221)
    for mid, sc in ((6201, 7.1), (6203, 7.45), (6202, 7.7), (6210, 8.0), (6211, 0),
                    (6220, 0), (6221, 6.6)):
        malinfo(mid, sc, ("Adventure",), (), ("Isekai",))
    mapping += [(2201, 1003, 1, 0, 6201), (2202, 1003, 1, 12, 6202), (2203, 1003, 1, 100, 6203),
                (2210, 1003, 2, 0, 6210), (2211, 1003, 2, 13, 6211),
                (2220, 1003, 3, 0, 6220), (2221, 1003, 3, 12, 6221)]
    add_anime(1003, 2201, "Cour Show", "1,2,3")

    # 4. ongoing show; award only a foreign-voice-actor one
    media(2301, "Ongoing Show", "Ongoing Show", "連載中", 69, status=RELEASING,
          genres=("Fantasy", "Adventure"), tags=tags(("Magic", 71), ("Isekai", 69)),
          season="FALL", season_year=2024, mal_id=6301)
    malinfo(6301, 6.95, ("Fantasy",), (), ("Isekai",))
    mapping.append((2301, 1004, 1, 0, 6301))
    add_anime(1004, 2301, "Ongoing Show", "1")
    ongoing.append(1004)
    awards.append({"anilist_id": "2301", "year": "2024", "cr_award": "Best Voice Actor (English)"})
    lists["Watching"].append(2301)

    # 5. ALL CAPS title, two sequels branching (multi -> single), award on S1 only
    media(2401, "86 EIGHTY-SIX", "86 EIGHTY-SIX", "86―エイティシックス―", 85,
          genres=("Action", "Drama", "Sci-Fi"),
          tags=tags(("Military", 100), ("War", 70), ("Mecha", 69)),
          studios=(ANIM("A-1 Pictures"),), season="SPRING", season_year=2021,
          sequels=(seq(2402), seq(2403, "ONA")), mal_id=6401)
    media(2402, "86 EIGHTY-SIX Part 2", "86 EIGHTY-SIX Part 2", "86 2", 83,
          genres=("Action",), season="FALL", season_year=2021,
          sequels=(seq(2404, "OVA"),), mal_id=6402)
    media(2403, "86 Side Story", "86 Side Story", "86 side", 70, season="WINTER",
          season_year=2022, sequels=(seq(2405, "SPECIAL", NYR),), mal_id=6403)
    media(2404, "86 OVA", "86 OVA", "86 ova", 68, mal_id=6404)
    media(2405, "86 Special", status=NYR, score=0, mal_id=6405)
    for mid, sc in ((6401, 8.45), (6402, 8.3), (6403, 7.0), (6404, 6.8), (6405, 0)):
        malinfo(mid, sc, ("Action", "Drama"), ("Seinen",), ("Military", "Mecha"))
    mapping += [(2401, 1005, 1, 0, 6401), (2402, 1005, 2, 0, 6402)]
    add_anime(1005, 2401, "86 EIGHTY-SIX", "1,2")
    awards.append({"anilist_id": "2401", "year": "2022", "cr_award": "Best Anime"})
    awards.append({"anilist_id": "2401", "year": "2022", "cr_award": "Best Score"})
    lists["Completed"] += [2401, 2402]
    lists["Dropped"].append(2402)

    # 6. multi -> multi sequel chain; last id dropped by `read` (no trailing newline); idMal via mapping
    media(2501, "Branchy", "Branchy", "ブランチ", 73, mal_id=None,
          sequels=(seq(2502), seq(2503)))
    media(2502, "Branchy 2", "Branchy 2", "b2", 72, sequels=(seq(2504), seq(2505)), mal_id=6502)
    media(2503, "Branchy 3", "Branchy 3", "b3", 71, mal_id=6503)
    media(2504, "Branchy 4", "Branchy 4", "b4", 70, mal_id=6504)
    media(2505, "Branchy 5", "Branchy 5", "b5", 69, sequels=(seq(2506, "TV", NYR),), mal_id=6505)
    media(2506, "Branchy 6", status=NYR, score=0, mal_id=6506)
    anilist_mal.append((2501, 6501))
    for mid, sc in ((6501, 7.3), (6502, 7.2), (6503, 7.1), (6504, 7.0), (6505, 6.9), (6506, 0)):
        malinfo(mid, sc, ("Comedy",))
    mapping.append((2501, 1006, 1, 0, 6501))
    add_anime(1006, 2501, "Branchy", "1")
    lists["Paused"].append(2503)

    # 7. not yet released show
    media(2601, "Future Show", "Future Show", "未来", 0, status=NYR, genres=("Fantasy",),
          start=(2027, 1), mal_id=6601)
    malinfo(6601, 0, ("Fantasy",))
    mapping.append((2601, 1007, 1, 0, 6601))
    add_anime(1007, 2601, "Future Show", "1")
    lists["Planning"].append(2601)

    # 8. override with custom title + studio + ignore seasons
    media(2701, "Overridden Original", "Overridden English", "上書き", 80,
          studios=(ANIM("Original Studio"),), season="SUMMER", season_year=2017, mal_id=6701)
    media(2702, "Overridden Original S2", "Overridden English S2", "上書き2", 60,
          season="WINTER", season_year=2018, mal_id=6702)
    malinfo(6701, 8.1, ("Comedy",))
    malinfo(6702, 6.2, ("Comedy",))
    mapping += [(2701, 1008, 1, 0, 6701), (2702, 1008, 2, 0, 6702)]
    add_anime(1008, 2701, "Overridden Original", "1,2", flag="yes", override=True)
    ov_animes.append((1008, 2701, "CUSTOM TITLE: Override!", "Custom Studio", "yes", "custom+ignore"))

    # 9. override without title/studio, last_season 1 (valid_anilist_id hack), not in mapping, airing
    media(2801, "Override Plain", "Override Plain EN", "オーバー", 74, season="SPRING",
          season_year=2022, mal_id=6801, sequels=(seq(2802, "TV", RELEASING),))
    media(2802, "Override Plain S2", "Override Plain EN S2", "o2", 74, status=RELEASING, mal_id=6802)
    malinfo(6801, 7.4, ("Slice of Life",))
    malinfo(6802, 7.4, ("Slice of Life",))
    add_anime(1009, 2801, "Override Plain", "1", override=True)
    ov_animes.append((1009, 2801, "", "", "", "plain"))
    ongoing.append(1009)

    # 10. override title only on S1 anilist id; S2 not overridden
    media(2901, "Title Only Orig", "Title Only EN", "t1", 77, season="WINTER", season_year=2016,
          sequels=(seq(2902),), mal_id=6901)
    media(2902, "Title Only Orig 2", "Title Only EN 2", "t2", 79, season="FALL", season_year=2016,
          mal_id=6902)
    malinfo(6901, 7.7, ("Mystery",))
    malinfo(6902, 7.9, ("Mystery",))
    mapping += [(2901, 1010, 1, 0, 6901), (2902, 1010, 2, 0, 6902)]
    add_anime(1010, 2901, "Title Only Orig", "1,2", override=True)
    ov_animes.append((1010, 2901, "Override Title Only", "", "", "title only"))

    # 11. null english/native, empty genres/tags/studios, mapping season -1 -> plain rating path
    media(3001, "Minimal Titles", None, None, 55, genres=(), tags=(), studios=(),
          season="WINTER", season_year=2014, mal_id=7001)
    malinfo(7001, 5.5)
    mapping.append((3001, 1011, -1, 0, 7001))
    add_anime(1011, 3001, "Minimal Titles", "1")

    # 12. no MAL id anywhere, tvdb id absent from mapping, two seasons -> plain path, stale vars
    media(3101, "No Mapping", "No Mapping", "nm", 64, genres=("Horror",),
          tags=tags(("Gore", 90)), mal_id=None)
    add_anime(1012, 3101, "No Mapping", "1,2")

    # 13. sequel cycle (50 hop limit)
    media(3201, "Cycle A", "Cycle A", "ca", 61, sequels=(seq(3202),), mal_id=7201)
    media(3202, "Cycle B", "Cycle B", "cb", 62, sequels=(seq(3201),), mal_id=7202)
    malinfo(7201, 6.1)
    malinfo(7202, 6.2)
    mapping.append((3201, 1013, 1, 0, 7201))
    add_anime(1013, 3201, "Cycle A", "1")

    # 14-16. season-year fallbacks
    media(3301, "Year Null Both", "Year Null Both", "yn", 70, start=(2015, 5), mal_id=7301)
    media(3401, "Year Only", "Year Only", "yo", 70, season_year=2020, mal_id=7401)
    media(3501, "Month Null", "Month Null", "mn", 70, start=(2008, None), mal_id=7501)
    for mid in (7301, 7401, 7501):
        malinfo(mid, 7.0, ("Sports",))
    mapping += [(3301, 1014, 1, 0, 7301), (3401, 1015, 1, 0, 7401), (3501, 1016, 1, 0, 7501)]
    add_anime(1014, 3301, "Year Null Both", "1")
    add_anime(1015, 3401, "Year Only", "1")
    add_anime(1016, 3501, "Month Null", "1")

    # 17. three seasons, startDate month fallback for Winter/Summer/Fall
    for aid, mo in ((3601, 2), (3602, 7), (3603, 11)):
        media(aid, "Months %d" % (aid - 3600), "Months %d" % (aid - 3600), "mo", 60 + mo,
              start=(2010 + aid - 3600, mo), mal_id=aid + 4000)
        malinfo(aid + 4000, 6.0 + mo / 10.0, ("Music",))
    mapping += [(3601, 1017, 1, 0, 7601), (3602, 1017, 2, 0, 7602), (3603, 1017, 3, 0, 7603)]
    add_anime(1017, 3601, "Months", "1,2,3")

    # 18. only season 2 in Plex; special characters in titles/studio
    media(3701, "Konosuba! 100% \\ \"q\" it's: ok", "Special: Chars % \\ ' \"", "特殊", 88,
          genres=("Comedy", "Fantasy"), tags=tags(("Isekai", 95),), mal_id=7701,
          studios=(ANIM("Studio, With Comma"),))
    media(3702, "Konosuba 2", "Konosuba 2", "k2", 87, mal_id=7702)
    malinfo(7701, 8.8, ("Comedy",))
    malinfo(7702, 8.7, ("Comedy",))
    mapping += [(3701, 1018, 1, 0, 7701), (3702, 1018, 2, 0, 7702)]
    add_anime(1018, 3701, "Konosuba", "1,2", "Special Chars (2016)")

    # 19. accented all caps title (tr/sed locale behaviour)
    media(3801, "POKÉMON", "POKÉMON", "ポケモン", 72, mal_id=7801)
    malinfo(7801, 7.2)
    mapping.append((3801, 1019, 1, 0, 7801))
    add_anime(1019, 3801, "Pokemon", "1")

    # 20. only season 2 in Plex (last_season != 1, no season 1 row for this tvdb id)
    media(3901, "Second Only", "Second Only", "so", 90, mal_id=7901, season="FALL", season_year=2012)
    malinfo(7901, 9.0)
    mapping.append((3901, 1020, 2, 0, 7901))
    add_anime(1020, 3901, "Second Only", "2")

    # unrelated mapping rows (noise)
    mapping += [(9901, 9001, 1, 0, 9501), (9902, 9001, 1, 12, 9502)]

    # ---- movies ----
    media(4001, "Movie One", "Movie One EN", "映画1", 81, genres=("Drama", "Romance"),
          tags=tags(("Love Triangle", 75),), studios=(ANIM("Movie Studio"),), mal_id=8001)
    malinfo(8001, 8.15, ("Drama", "Romance"), (), ("School",))
    awards.append({"anilist_id": "4001", "year": "2020", "cr_award": "Best Film"})
    lists["Completed"].append(4001)
    add_movie("tt0000001", 4001, "Movie One")

    media(4002, "Movie Override", "Movie Override EN", "映画2", 76, mal_id=8002,
          studios=(OTHER("Only Producer"),))
    malinfo(8002, 7.55, ("Adventure",))
    add_movie("tt0000002", 4002, "Movie Override", override=True)
    ov_movies.append(("tt0000002", 4002, "Movie Custom Title", "Movie Custom Studio", "custom"))

    media(4003, "AKIRA", "AKIRA", None, 0, genres=("Sci-Fi",), mal_id=8003)
    malinfo(8003, 0, ("Sci-Fi",))
    add_movie("tt0000003", 4003, "Akira")

    media(4004, "No MAL Movie", None, "映画4", 79, studios=(OTHER("Prod Only"),), mal_id=None,
          genres=(), tags=tags(("Rare", 10),))
    add_movie("tt0000004", 4004, "No MAL Movie")

    media(4005, "Movie Override Plain", "Movie Override Plain EN", "映画5", 66, mal_id=8005,
          season="SUMMER", season_year=2023)
    malinfo(8005, 6.45, ("Comedy",), ("Kids",))
    add_movie("tt0000005", 4005, "Movie Override Plain", override=True)
    ov_movies.append(("tt0000005", 4005, "", "", ""))
    lists["Watching"].append(4005)
    lists["Planning"].append(4005)

    media(4006, "Movie Planned", "Movie Planned EN", "映画6", 0, status=NYR, mal_id=8006,
          genres=("Fantasy",))
    malinfo(8006, 0, ("Fantasy",))
    add_movie("tt0000006", 4006, "Movie Planned")


def jdump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def write_tsv(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write("\t".join(str(c) for c in r) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--username", default="Arialz")
    a = ap.parse_args()
    build()

    out = os.path.abspath(a.out)
    if os.path.exists(out):
        shutil.rmtree(out)
    data, tmp, idd, logs = (os.path.join(out, "config", d) for d in ("data", "tmp", "ID", "logs"))
    for d in (data, tmp, idd, logs, os.path.join(out, "pmm")):
        os.makedirs(d)

    with open(os.path.join(out, "VERSION"), "w") as f:
        f.write("#!/bin/bash\n\nversion=0.0.0-golden\n")

    # every sequel node must be a cached file (get-airing-status loads them)
    for m in list(anilist.values()):
        for e in m["data"]["Media"]["relations"]["edges"]:
            nid = e["node"]["id"]
            if nid not in anilist and e["relationType"] == "SEQUEL":
                media(nid, "stub-%s" % nid, status=e["node"]["status"], score=0)
    for aid, obj in anilist.items():
        jdump(os.path.join(data, "anilist-%s.json" % aid), obj)
    for mid, obj in mal.items():
        jdump(os.path.join(data, "MAL-%s.json" % mid), obj)
    jdump(os.path.join(data, "cr-award.json"), awards)

    with open(os.path.join(data, "ongoing.tsv"), "w") as f:
        f.write("".join("%s\n" % t for t in ongoing))
    for t, ids in lists.items():
        with open(os.path.join(data, "anilist-%s-%s.tsv" % (a.username, t)), "w") as f:
            f.write(",".join(str(i) for i in ids) + "\n")

    write_tsv(os.path.join(tmp, "anime-mapping.tsv"), mapping)
    write_tsv(os.path.join(tmp, "anilist-mal.tsv"), anilist_mal)

    # ID files laid out exactly as animes-renamer.sh / movies-renamer.sh write them
    write_tsv(os.path.join(idd, "animes.tsv"),
              [(t, i, ti, a_, s, f) for t, i, ti, a_, s, f, _ in animes])
    rows = []
    for imdb, aid, title, asset, ov in movies:
        if ov:  # movies-renamer.sh writes an empty mal_id column for overrides
            rows.append((imdb, "", aid, title, asset))
        else:
            rows.append((imdb, aid, title, asset))
    write_tsv(os.path.join(idd, "movies.tsv"), rows)

    cfg = os.path.join(out, "config")
    write_tsv(os.path.join(cfg, "override-ID-animes.tsv"),
              [("tvdb-id", "anilist-id", "Title", "Studio", "ignore_seasons", "notes")] + ov_animes)
    write_tsv(os.path.join(cfg, "override-ID-movies.tsv"),
              [("imdb-id", "anilist-id", "Title", "Studio", "notes")] + ov_movies)

    poster = b"\0" * 10240
    for asset in sorted(assets):
        d = os.path.join(out, "pmm", "assets", asset)
        os.makedirs(d, exist_ok=True)
        for n in ["poster.jpg"] + ["Season%02d.jpg" % k for k in range(1, 13)]:
            with open(os.path.join(d, n), "wb") as f:
                f.write(poster)


if __name__ == "__main__":
    main()
