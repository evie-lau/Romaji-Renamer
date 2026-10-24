#!/usr/bin/env python3
"""Metadata WRITE stage (port of write-metadata in functions.sh), python 3.10+, stdlib only.

    python3 tools/write_metadata.py --script-folder DIR --media animes|movies --output FILE

All settings come from the process environment (same names as config/default.env). The YAML must be
byte-identical to what the bash implementation produced (tests/golden/), quirks included. State that
bash keeps in global variables (anime_score, anime_tags, score_*, cr_awards, ...) is kept as attributes
of the Writer object for the same reason.

printf '%.1f' emulation: bash's printf parses its argument with strtold (x87 80-bit long double:
64-bit mantissa) and rounds the *exact* binary value half-to-even to one decimal. fmt1() reproduces
this with fractions.Fraction: parse the decimal string exactly, round it to a 64-bit mantissa
(round-half-even), then round that exact value to 1 decimal (round-half-even on exact ties only).
Python float formatting gives different results (e.g. 6.45 -> 6.5 where bash prints 6.4).
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rr_fetch  # noqa: E402

USERLIST_TYPES = ("Completed", "Watching", "Dropped", "Paused", "Planning")
FVA_WORDS = ("English", "Arabic", "Spanish", "Castilian", "French", "German", "Italian", "Portuguese", "Russian")
SEQUEL_FORMATS = ("TV", "ONA", "MOVIE", "OVA")
NYR_RE = re.compile(r"(?<!\w)NOT_YET_RELEASED(?!\w)")
NUM_PREFIX_RE = re.compile(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
ERR = object()  # jq runtime error (no output)


# Settings that can change a show's YAML (ASSET_FOLDER / LOG_FOLDER / FRAGMENT_CACHE cannot).
KEY_ENV = ("ADD_ANIME_TAG", "ALLOW_RENAMING", "ANILIST_LISTS", "ANILIST_LISTS_LEVEL", "ANILIST_TAGS_P",
           "ANILIST_USERNAME", "ANIME_AWARDS", "ANIME_AWARDS_NO_FVA", "CACHE_TIME_AIRING", "CACHE_TIME_FINISHED",
           "CACHE_TIME_UPCOMING", "DATA_CACHE_TIME", "DISABLE_TAGS", "IGNORE_S1_ONLY_RATING", "IGNORE_SEASONS",
           "IGNORE_SEASONS_RATING", "MAIN_TITLE_ENG", "ORIGINAL_TITLE_NATIVE", "POSTER_DOWNLOAD",
           "POSTER_SEASON_DOWNLOAD", "POSTER_SOURCE", "RATING_1_SOURCE", "RATING_1_TYPE", "RATING_2_SOURCE",
           "RATING_2_TYPE", "REDUCE_TITLE_CAPS", "RENAME_SEASONS", "SEASON_YEAR", "SORT_TITLE_ENG", "TAG_SOURCE")
# bash globals that leak from one show into the next (quirk carry-over); part of the key and restored on a hit
CARRIED = ("anime_score", "anime_tags", "score_1", "score_2", "score_1_cour", "score_2_cour", "cr_awards", "mal_id",
           "airing_loop", "season_loop", "season_number", "cours_count", "cours_count_total")


def _source_hash():
    here = os.path.dirname(os.path.abspath(__file__))
    digest = hashlib.sha256()
    for name in ("write_metadata.py", "rr_fetch.py"):
        with open(os.path.join(here, name), "rb") as fh:
            digest.update(fh.read())
    return digest.hexdigest()


SOURCE_HASH = _source_hash()


class FatalError(Exception):
    pass


# --------------------------------------------------------------------------- number helpers
def awk_num(text):
    m = NUM_PREFIX_RE.match(text or "")
    return float(m.group(1)) if m else 0.0


def awk_str(value):
    """awk `print x` for a number: integers as integers, otherwise OFMT %.6g."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return "%.6g" % value


def _round_half_even(frac):
    floor = frac.numerator // frac.denominator
    rest = frac - floor
    if rest > Fraction(1, 2) or (rest == Fraction(1, 2) and floor % 2 == 1):
        return floor + 1
    return floor


def _pow2(exp):
    return Fraction(1 << exp) if exp >= 0 else Fraction(1, 1 << -exp)


def to_long_double(frac):
    """Round an exact rational to the x87 80-bit extended format (64-bit mantissa, ties-to-even)."""
    if frac == 0:
        return frac
    sign = -1 if frac < 0 else 1
    mag = abs(frac)
    exp = mag.numerator.bit_length() - mag.denominator.bit_length() - 63
    while True:
        scaled = mag / _pow2(exp)
        if scaled >= 1 << 64:
            exp += 1
        elif scaled < 1 << 63:
            exp -= 1
        else:
            break
    return sign * _round_half_even(scaled) * _pow2(exp)


def fmt1(text):
    """bash: printf '%.*f' 1 "$text"."""
    m = NUM_PREFIX_RE.match(text or "")
    value = to_long_double(Fraction(m.group(1))) if m else Fraction(0)
    scaled = abs(value) * 10
    digits = _round_half_even(scaled)
    sign = "-" if value < 0 else ""
    return "%s%d.%d" % (sign, digits // 10, digits % 10)


def toint(text):
    """bash arithmetic evaluation of a plain word: integers as is, anything else (null, empty) is 0."""
    m = re.fullmatch(r"\s*(-?\d+)\s*", text or "")
    return int(m.group(1)) if m else 0


# --------------------------------------------------------------------------- jq-ish helpers
def jget(doc, *path):
    cur = doc
    for key in path:
        if cur is None:
            return None
        if not isinstance(cur, dict):
            return ERR
        cur = cur.get(key)
    return cur


def jstr(value):
    """jq -r rendering of a scalar."""
    if value is ERR:
        return ""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e17:
            return str(int(value))
        return repr(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def jraw(doc, *path):
    return jstr(jget(doc, *path))


def read_json(path):
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read())
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- text helpers
def capitalise_words(text):
    """sed 's/\\( \\|^\\)\\(.\\)/\\1\\u\\2/g'"""
    def up(m):
        ch = m.group(2)
        upper = ch.upper()
        return m.group(1) + (upper if len(upper) == 1 else ch)
    return re.sub(r"( |^)(.)", up, text)


def less_caps_title(title, enabled=True):
    if enabled:
        remainder = re.sub(r"[^ a-zA-Z]", "", title).replace(" ", "")
        if re.fullmatch(r"[A-Z]+", remainder):
            lowered = re.sub(r"[A-Z]", lambda m: m.group(0).lower(), title)  # tr is ASCII-only in C.utf8
            title = capitalise_words(lowered)
    return title


def remove_type(text, userlist_type):
    """sed s/X// | sed 's/^,//' | sed 's/,,/,/g' (first occurrence only, may leave a trailing comma)."""
    text = text.replace(userlist_type, "", 1)
    if text.startswith(","):
        text = text[1:]
    return text.replace(",,", ",")


def add_type(current, userlist_type):
    """Bash accumulation bug: the 'more than one entry' test is never true, so the last match wins."""
    count = len(current.split(",")) if current else 0
    return "%s,%s" % (current, userlist_type) if count > 1 else userlist_type


def bash_read(line, nvars):
    """`IFS=$'\\t' read -r a b c ...`: tabs are IFS-whitespace, so runs collapse and edges are trimmed."""
    rest = line.lstrip("\t")
    fields = []
    for _ in range(nvars - 1):
        if not rest:
            fields.append("")
            continue
        idx = rest.find("\t")
        if idx < 0:
            fields.append(rest)
            rest = ""
        else:
            fields.append(rest[:idx])
            rest = rest[idx:].lstrip("\t")
    fields.append(rest.rstrip("\t"))
    return fields


def read_lines(path):
    """Lines like awk sees them (a last line without newline is included)."""
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            data = fh.read()
    except OSError:
        return []
    lines = data.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def tsv_rows(path):
    return [line.split("\t") for line in read_lines(path)]


def season_label_lines(label_add, label_remove, last_is_1, ignore_s1_only, ignore_seasons_rating, indent="        "):
    """label / label.remove lines of one season block (rules of IGNORE_S1_ONLY_RATING & co)."""
    lines = []
    score_off = (ignore_s1_only or ignore_seasons_rating) if last_is_1 else ignore_seasons_rating
    if label_add:
        lines.append("%slabel: %s%s\n" % (indent, "" if score_off else "Score,", label_add))
    else:
        if last_is_1:
            show_score = (not ignore_s1_only) or (not ignore_seasons_rating)
        else:
            show_score = not ignore_seasons_rating
        if show_score:
            lines.append("%slabel: Score\n" % indent)
    if label_remove:
        lines.append("%slabel.remove: %s%s\n" % (indent, "Score," if score_off else "", label_remove))
    elif score_off:
        lines.append("%slabel.remove: Score\n" % indent)
    return lines


# --------------------------------------------------------------------------- airing status
def sequel_nodes(docs):
    nodes = []
    for doc in docs:
        edges = jget(doc, "data", "Media", "relations", "edges")
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, dict) or edge.get("relationType") != "SEQUEL":
                continue
            node = edge.get("node")
            if isinstance(node, dict) and (node.get("format") in SEQUEL_FORMATS or node.get("format") is None):
                nodes.append(node)
    return nodes


def walk_airing_status(anilist_id, get_doc, max_hops=50):
    """get-airing-status: 'Planned' or 'Ended'. get_doc(id) returns the parsed AniList document.

    Reproduces the bash quirks: several sequels are followed through a file of ids that is written
    with a trailing newline by the first hop but without one by later hops, so `while read` skips the
    last id there; only the first id is used as the current id when several sequels exist.
    """
    if NYR_RE.search(jraw(get_doc(anilist_id), "data", "Media", "status")):
        return "Planned"
    current = anilist_id
    hops = 0
    multi = 0
    ids_file = ""
    while hops < max_hops:
        if multi > 0:
            ids = ids_file.split("\n")[:-1]  # unterminated last line is not read
            docs = [get_doc(i) for i in ids]
            writes_newline = False
        else:
            docs = [get_doc(current)]
            writes_newline = True
        nodes = sequel_nodes(docs)
        if not nodes:
            return "Ended"
        if any(NYR_RE.search(json.dumps(node, ensure_ascii=False)) for node in nodes):
            return "Planned"
        ids = [jstr(node.get("id")) for node in nodes]
        multi = len(ids) - 1
        if multi > 0:
            ids_file = "\n".join(ids) + ("\n" if writes_newline else "")
        current = ids[0]
        hops += 1
    return "Ended"


# --------------------------------------------------------------------------- logging
class Logger:
    def __init__(self, log_path, match_path):
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self.fh = open(log_path, "a", encoding="utf-8", errors="surrogateescape")
        self.match_fh = open(match_path, "a", encoding="utf-8", errors="surrogateescape")

    def log(self, text):
        """text is everything after the HH:MM:SS prefix (leading tabs / ' - ' included)."""
        line = "%s%s\n" % (time.strftime("%H:%M:%S"), text)
        with self.lock:
            self.fh.write(line)
            sys.stdout.write(line)

    def match(self, text):
        with self.lock:
            self.match_fh.write("%s%s\n" % (time.strftime("%H:%M:%S"), text))

    def close(self):
        self.fh.close()
        self.match_fh.close()


# --------------------------------------------------------------------------- the writer
class Writer:
    def __init__(self, script_folder, media, env, logger, client):
        self.sf = script_folder
        self.media = media
        self.env = env
        self.logger = logger
        self.client = client
        self.out = []
        self.poster_jobs = {}
        self.al = {}
        self.mal = {}
        cfg = os.path.join(script_folder, "config")
        self.data_dir = os.path.join(cfg, "data")
        self.asset_folder = env.get("ASSET_FOLDER") or os.path.join(script_folder, "pmm", "assets")
        override = "override-ID-%s.tsv" % media
        self.override = {}
        for row in tsv_rows(os.path.join(cfg, override)):
            key = row[1] if len(row) > 1 else ""
            if key not in self.override:
                self.override[key] = (row[2] if len(row) > 2 else "", row[3] if len(row) > 3 else "")
        self.mapping_by_tvdb = {}
        self.mapping_seasons = {}
        for row in tsv_rows(os.path.join(cfg, "tmp", "anime-mapping.tsv")):
            row = row + [""] * (5 - len(row))
            self.mapping_by_tvdb.setdefault(row[1], []).append((row[0], row[2], row[3]))
            self.mapping_seasons.setdefault(row[0], []).append(row[2])
        self.anilist_mal = {}
        for row in tsv_rows(os.path.join(cfg, "tmp", "anilist-mal.tsv")):
            self.anilist_mal.setdefault(row[0], row[1] if len(row) > 1 else "")
        self.ongoing = {row[0] for row in tsv_rows(os.path.join(self.data_dir, "ongoing.tsv"))}
        self.awards = {}
        awards_doc = read_json(os.path.join(self.data_dir, "cr-award.json"))
        for rec in awards_doc if isinstance(awards_doc, list) else []:
            if isinstance(rec, dict) and isinstance(rec.get("anilist_id"), str):
                self.awards.setdefault(rec["anilist_id"], []).append(rec.get("cr_award"))
        self.userlists = {}
        if env.get("ANILIST_LISTS") == "Yes":
            for utype in USERLIST_TYPES:
                path = os.path.join(self.data_dir, "anilist-%s-%s.tsv" % (env.get("ANILIST_USERNAME", ""), utype))
                lines = read_lines(path)
                tokens = set()
                for line in lines:
                    tokens.update(re.findall(r"\w+", line))
                self.userlists[utype] = (tokens, lines)
        # "globals" of the bash implementation
        self.anilist_id = ""
        self.tvdb_id = ""
        self.plex_title = ""
        self.asset_name = ""
        self.mal_id = ""
        self.anime_score = ""
        self.anime_tags = ""
        self.score_1 = ""
        self.score_2 = ""
        self.cr_awards = ""
        self.airing_loop = 0
        self.season_loop = 0
        self.season_number = ""
        self.cours_count = 0
        self.cours_count_total = 0
        self.capture = None
        self.touched = {}
        self.cache_enabled = env.get("FRAGMENT_CACHE", "Yes") != "No"
        self.fragment_dir = os.path.join(cfg, "tmp", "fragments", media)
        self.fragment_hits = 0
        self.fragment_misses = 0
        self.fragment_used = set()

    # ----- env / output
    def e(self, name):
        return self.env.get(name, "")

    def w(self, text):
        self.out.append(text)

    def log(self, text):
        if self.capture is not None:
            self.capture["logs"].append(["log", text])
        self.logger.log(text)

    def match(self, text):
        if self.capture is not None:
            self.capture["logs"].append(["match", text])
        self.logger.match(text)

    # ----- cache handling (get-anilist-infos / get-mal-infos)
    def cache_ttl(self, status):
        env = self.env
        if status in ("FINISHED", "CANCELLED"):
            name = "CACHE_TIME_FINISHED"
        elif status in ("RELEASING", "HIATUS"):
            name = "CACHE_TIME_AIRING"
        elif status == "NOT_YET_RELEASED":
            name = "CACHE_TIME_UPCOMING"
        else:
            name = None
        ttl = (env.get(name) if name else "") or env.get("DATA_CACHE_TIME") or "5"
        if not re.fullmatch(r"[0-9]+", ttl):
            raise FatalError("Error invalid cache TTL: %s" % ttl)
        return int(ttl)

    @staticmethod
    def is_fresh(path, days):
        try:
            st = os.stat(path)
        except OSError:
            return False
        return st.st_size > 0 and time.time() - st.st_mtime < days * 86400

    def anilist_label(self, aid):
        if self.airing_loop == 1:
            return "\t\t\t - Downloading airing info for Anilist : %s" % aid
        if self.cours_count_total > 1:
            return "\t\t - Downloading data for S%s part-%s Anilist : %s" % (self.season_number, self.cours_count, aid)
        if self.season_loop == 1:
            return "\t\t - Downloading data for S%s anilist : %s" % (self.season_number, aid)
        return "\t\t - Downloading data for anilist : %s" % aid

    def load_al(self, aid):
        """get-anilist-infos + one-time load of the JSON."""
        if re.fullmatch(r"[0-9]+", aid):
            self.touched[self.client.anilist_path(aid)] = None
        if aid in self.al:
            return self.al[aid]
        doc = None
        if re.fullmatch(r"[0-9]+", aid):
            path = self.client.anilist_path(aid)
            doc = read_json(path)
            status = jget(doc, "data", "Media", "status")
            status = status if isinstance(status, str) else ""
            ttl = self.cache_ttl(status)
            if not (status and self.is_fresh(path, ttl)):
                try:
                    doc = self.client.fetch_anilist(aid, self.anilist_label(aid))
                except rr_fetch.FetchError:
                    if not status:
                        raise FatalError("can't download AniList data for %s" % aid)
                    self.log(" - AniList refresh failed, retaining cached data for Anilist : %s" % aid)
        self.al[aid] = doc
        return doc

    def get_mal_id(self):
        raw = jraw(self.load_al(self.anilist_id), "data", "Media", "idMal")
        if raw in ("null", ""):
            raw = self.anilist_mal.get(self.anilist_id, "")
        self.mal_id = raw

    def missing_mal(self):
        self.log("\t\t - Missing MAL ID for Anilist : %s / %s" % (self.anilist_id, self.plex_title))
        self.match(" - Missing MAL ID for Anilist : %s / %s" % (self.anilist_id, self.plex_title))

    def get_mal_infos(self):
        self.mal_id = ""
        self.get_mal_id()
        mal = self.mal_id
        if mal in ("null", ""):
            self.missing_mal()
            return
        path = self.client.mal_path(mal)
        status = jget(self.load_al(self.anilist_id), "data", "Media", "status")
        ttl = self.cache_ttl(status if isinstance(status, str) else "")
        self.touched[path] = ttl
        if mal in self.mal:
            return
        cached = read_json(path)
        cached_valid = rr_fetch.valid_mal(cached)
        if cached_valid and self.is_fresh(path, ttl):
            self.mal[mal] = cached
            return
        if self.cours_count_total > 1:
            label = "\t\t - Downloading data for S%s part-%s MAL : %s" % (self.season_number, self.cours_count, mal)
        elif self.season_loop == 1:
            label = "\t\t - Downloading data for S%s MAL : %s" % (self.season_number, mal)
        else:
            label = "\t\t - Downloading data for MAL : %s" % mal
        doc = self.client.fetch_mal(mal, cached_valid, label)
        if doc is not None:
            self.mal[mal] = doc
        elif cached_valid:
            self.mal[mal] = cached
        else:
            raise FatalError("can't download valid MAL data for MAL : %s" % mal)

    # ----- getters
    def get_title(self, aid, field):
        """get-romaji-title / get-english-title (override title wins, then caps reduction)."""
        override = self.override.get(aid)
        if override is not None and override[0] != "":
            title = override[0]
        else:
            title = jraw(self.load_al(aid), "data", "Media", "title", field)
        return less_caps_title(title, self.e("REDUCE_TITLE_CAPS") == "Yes").rstrip("\n")

    def get_studios(self, aid):
        override = self.override.get(aid)
        studio = override[1] if override is not None else ""
        if studio == "":
            doc = self.load_al(aid)
            studio = self.first_studio(doc, True)
            if studio == "":
                studio = self.first_studio(doc, False)
        return studio

    @staticmethod
    def first_studio(doc, only_animation):
        edges = jget(doc, "data", "Media", "studios", "edges")
        if not isinstance(edges, list):
            return ""
        for edge in edges:
            if not isinstance(edge, dict):
                return ""
            node = edge.get("node")
            if only_animation and not (isinstance(node, dict) and node.get("isAnimationStudio") is True):
                continue
            name = node.get("name") if isinstance(node, dict) else None
            return jstr(name).split("\n")[0]
        return ""

    def get_score(self):
        raw = jraw(self.load_al(self.anilist_id), "data", "Media", "averageScore")
        if raw in ("null", ""):
            self.anime_score = "0"
        else:
            fields = raw.split()
            self.anime_score = awk_str(awk_num(fields[0] if fields else "") / 10)

    def get_mal_score(self):
        self.mal_id = ""
        self.get_mal_id()
        if self.mal_id in ("null", ""):
            self.missing_mal()
            return
        self.anime_score = "0"
        self.get_mal_infos()
        raw = jraw(self.mal.get(self.mal_id), "data", "score")
        self.anime_score = "0" if raw in ("null", "") else raw

    def get_anilist_tags(self):
        doc = self.load_al(self.anilist_id)
        genres = jget(doc, "data", "Media", "genres")
        names = []
        if isinstance(genres, list):
            names = [jstr(g) for g in genres]
            tags = jget(doc, "data", "Media", "tags")
            try:
                threshold = json.loads(self.e("ANILIST_TAGS_P"))
            except ValueError:
                threshold = None
            if isinstance(tags, list) and isinstance(threshold, (int, float)):
                for tag in tags:
                    if not isinstance(tag, dict):
                        break
                    rank = tag.get("rank")
                    if rank is None or (isinstance(rank, (int, float)) and rank < threshold):
                        continue
                    names.append(jstr(tag.get("name")))
        self.anime_tags = ",".join(names)

    def get_mal_tags(self):
        self.mal_id = ""
        self.get_mal_id()
        if self.mal_id in ("null", ""):
            self.missing_mal()
            return
        self.get_mal_infos()
        doc = self.mal.get(self.mal_id)
        names = []
        for key in ("genres", "demographics", "themes"):
            items = jget(doc, "data", key)
            if not isinstance(items, list):
                break
            names.extend(jstr(item.get("name")) if isinstance(item, dict) else jstr(ERR) for item in items)
        self.anime_tags = ",".join(names)

    def get_animes_season_year(self):
        doc = self.load_al(self.anilist_id)
        combined = "%s %s" % (jraw(doc, "data", "Media", "seasonYear"), jraw(doc, "data", "Media", "season"))
        season = capitalise_words(combined.lower())
        if season == "Null Null":
            year = jraw(doc, "data", "Media", "startDate", "year")
            month = toint(jraw(doc, "data", "Media", "startDate", "month"))
            if month <= 3:
                name = "Winter"
            elif month <= 6:
                name = "Spring"
            elif month <= 9:
                name = "Summer"
            else:
                name = "Fall"
            season = "%s %s" % (year, name)
        return season

    def get_animes_award(self, aid):
        self.cr_awards = ""
        entries = self.awards.get(aid, [])
        if self.e("ANIME_AWARDS_NO_FVA") == "Yes":
            entries = [a for a in entries if isinstance(a, str) and not any(w in a for w in FVA_WORDS)]
        if entries:
            self.cr_awards = "AA"

    def get_airing_status(self):
        self.airing_loop = 1
        try:
            return walk_airing_status(self.anilist_id, self.load_al)
        finally:
            self.airing_loop = 0

    # ----- posters (collected, downloaded at the end)
    def poster_url(self):
        if self.e("POSTER_SOURCE") == "MAL":
            self.get_mal_infos()
            return jraw(self.mal.get(self.mal_id), "data", "images", "jpg", "large_image_url"), "MAL", self.mal_id
        return jraw(self.load_al(self.anilist_id), "data", "Media", "coverImage", "extraLarge"), "anilist", self.anilist_id

    def queue_poster(self, dest, season=None):
        if self.capture is not None:
            self.capture["posters"].append([os.path.basename(dest), season, self.anilist_id])
        if os.path.isfile(dest) and os.path.getsize(dest) >= 10000:
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        url, kind, ident = self.poster_url()
        what = "%s%s : %s" % ("S%s " % season if season is not None else "", kind, ident)
        self.poster_jobs.setdefault(dest, (url, what))

    def get_poster(self):
        if self.e("POSTER_DOWNLOAD") == "Yes":
            self.queue_poster(os.path.join(self.asset_folder, self.asset_name, "poster.jpg"))

    def get_season_poster(self):
        if self.e("POSTER_SEASON_DOWNLOAD") == "Yes":
            number = self.season_number
            name = "Season0%s.jpg" % number if toint(number) < 10 else "Season%s.jpg" % number
            self.queue_poster(os.path.join(self.asset_folder, self.asset_name, name), number)

    def run_poster_downloads(self):
        if not self.poster_jobs:
            return

        def job(item):
            dest, (url, what) = item
            self.log("\t\t - Downloading poster for %s" % what)
            if not url or url == "null":
                self.log("\t\t - No poster url for %s, skipping" % what)
                return
            if self.client.download_binary(url, dest):
                self.log("\t\t - Done")
            else:
                self.log("\t\t - Poster download failed for %s" % what)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(job, self.poster_jobs.items()))

    # ----- ratings
    def src(self, n):
        return self.e("RATING_%d_SOURCE" % n)

    def fetch_score(self, n):
        if self.src(n) == "ANILIST":
            self.get_score()
        else:
            self.get_mal_score()
        return self.anime_score

    def invalid_rating_log(self, n, ident_mal=False):
        if self.src(n) == "ANILIST":
            self.log("\t\t - invalid rating for Anilist : %s skipping " % self.anilist_id)
        else:
            self.log("\t\t - invalid rating for MAL : %s skipping " % self.mal_id)

    def get_rating(self, n):
        if self.src(n) in ("ANILIST", "MAL"):
            setattr(self, "score_%d" % n, self.fetch_score(n))
        score = getattr(self, "score_%d" % n)
        if score == "0":
            self.invalid_rating_log(n)
        else:
            score = fmt1(score)
            setattr(self, "score_%d" % n, score)
            self.w("    %s_rating: %s\n" % (self.e("RATING_%d_TYPE" % n), score))

    def check_rating_valid(self, n):
        if self.src(n) not in ("ANILIST", "MAL"):
            return
        score = getattr(self, "score_%d" % n)
        if score == "0":
            if self.src(n) == "ANILIST":
                self.invalid_rating_log(n)
            else:
                self.mal_id = ""
                self.get_mal_id()
                # bash: rating 1 tests `!= null || -n` (always true), rating 2 `== null || -n`
                if n == 1 or self.mal_id == "null" or self.mal_id != "":
                    self.log("\t\t - invalid rating for MAL : %s skipping " % self.mal_id)
        else:
            score = fmt1(score)
            setattr(self, "score_%d" % n, score)
            self.w("    %s_rating: %s\n" % (self.e("RATING_%d_TYPE" % n), score))

    def user_in_list(self, utype, ids):
        entry = self.userlists.get(utype)
        if entry is None:
            return False
        tokens, lines = entry
        if ids and all(re.fullmatch(r"\w+", i) for i in ids):
            return any(i in tokens for i in ids)
        pattern = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(re.escape(i) for i in ids))
        return any(pattern.search(line) for line in lines)

    # ----- seasons
    def mapped_season_ids(self, tvdb, season):
        rows = [(epoff, aid) for aid, s, epoff in self.mapping_by_tvdb.get(tvdb, ()) if s == season]
        rows.sort(key=lambda r: r[0])  # stable, epoffset compared as a string like `sort -k1,1`
        return [aid for _e, aid in rows]

    def get_season_infos(self, seasons_list):
        env = self.env
        backup = self.anilist_id
        tvdb = self.tvdb_id
        season_check = "\n".join(self.mapping_seasons.get(backup, ())).rstrip("\n")
        fields = seasons_list.split(",")
        first_season = fields[0]
        last_season = fields[-1] if seasons_list else ""
        total_seasons = len(fields) if seasons_list else 0
        valid = "\n".join(a for a, _s, _o in self.mapping_by_tvdb.get(tvdb, ())).rstrip("\n")
        override_id = ""
        last1 = toint(last_season) == 1
        if backup in self.override and last1:
            valid = "1"
            override_id = backup
        if toint(first_season) == 0:
            total_seasons -= 1
        r1 = self.src(1) in ("ANILIST", "MAL")
        r2 = self.src(2) in ("ANILIST", "MAL")
        if valid and season_check != "-1":
            total = {1: "0", 2: "0"}
            season_score = {1: "0", 2: "0"}
            no_rating_seasons = {1: 0, 2: 0}
            self.season_loop = 0
            anime_season = ""
            self.cr_awards = ""
            self.w("    seasons:\n")
            lists_on = env.get("ANILIST_LISTS") == "Yes" and env.get("ANILIST_LISTS_LEVEL") in ("season", "both")
            ign_s1 = env.get("IGNORE_S1_ONLY_RATING") == "Yes"
            ign_rating = env.get("IGNORE_SEASONS_RATING") == "Yes"
            season_list = seasons_list.split(",")
            if season_list and season_list[-1] == "":
                season_list.pop()
            for season_number in season_list:
                self.season_number = season_number
                if toint(season_number) == 0:
                    self.w("      0:\n        label.remove: Score\n")
                    continue
                self.season_loop = 1
                ids = self.mapped_season_ids(tvdb, season_number)
                if override_id and toint(season_number) == 1:
                    ids = [backup]
                self.cours_count_total = len(ids)
                cours_total = {1: "0", 2: "0"}
                no_rating_cours = {1: 0, 2: 0}
                self.cours_count = 0
                all_cours_anime_season = ""
                season_add = ""
                for aid in ids:
                    self.cours_count += 1
                    self.anilist_id = aid
                    if aid != "":
                        doc = self.load_al(aid)
                        if NYR_RE.search(jraw(doc, "data", "Media", "status")):
                            no_rating_cours[1] += 1
                            no_rating_cours[2] += 1
                            continue
                        if lists_on:
                            for utype in USERLIST_TYPES:
                                if self.user_in_list(utype, [aid]):
                                    season_add = add_type(season_add, utype)
                        for n in (1, 2):
                            if self.src(n) in ("ANILIST", "MAL"):
                                cour = fmt1(self.fetch_score(n))
                                setattr(self, "score_%d_cour" % n, cour)
                                if cour == "0.0":
                                    no_rating_cours[n] += 1
                        if env.get("ANIME_AWARDS") == "Yes":
                            self.get_animes_award(aid)
                        if env.get("SEASON_YEAR") == "Yes":
                            anime_season = self.get_animes_season_year()
                            if self.cours_count > 1:
                                all_cours_anime_season = "%s,%s" % (anime_season, all_cours_anime_season)
                            else:
                                all_cours_anime_season = anime_season
                    else:
                        self.log("\t\t - Missing Anilist ID for tvdb : %s - Season : %s cour : %s / %s"
                                 % (tvdb, season_number, self.cours_count, self.plex_title))
                        self.match(" - Missing Anilist ID for tvdb : %s - Season : %s cour : %s / %s"
                                          % (tvdb, season_number, self.cours_count, self.plex_title))
                        no_rating_cours[1] += 1
                        no_rating_cours[2] += 1
                    for n, on in ((1, r1), (2, r2)):
                        if on:
                            cour_score = getattr(self, "score_%d_cour" % n, "")
                            cours_total[n] = awk_str(awk_num(cour_score) + awk_num(cours_total[n]))
                anime_season = all_cours_anime_season
                for n, on in ((1, r1), (2, r2)):
                    if not on:
                        continue
                    if cours_total[n] != "0":
                        count = self.cours_count - no_rating_cours[n]
                        if count != 0:
                            season_score[n] = fmt1(awk_str(awk_num(cours_total[n]) / count))
                        else:
                            season_score[n] = "0"
                            no_rating_seasons[n] += 1
                    else:
                        # bash typo: the rating 2 branch updates the rating 1 variables
                        season_score[1] = "0"
                        no_rating_seasons[1] += 1
                self.cours_count_total = 0
                main_id = ""
                for aid, s, epoff in self.mapping_by_tvdb.get(tvdb, ()):
                    if s == season_number and epoff == "0":
                        main_id = aid
                        break
                if override_id and toint(season_number) == 1:
                    main_id = backup
                self.anilist_id = main_id
                if main_id == "":
                    self.log("\t\t - Missing Anilist ID for tvdb : %s - Season : %s / %s" % (tvdb, season_number, self.plex_title))
                    self.match(" - Missing Anilist ID for tvdb : %s - Season : %s / %s" % (tvdb, season_number, self.plex_title))
                    no_rating_seasons[1] += 1
                    no_rating_seasons[2] += 1
                    continue
                self.w("      %s:\n" % season_number)
                romaji = self.get_title(main_id, "romaji")
                english = self.get_title(main_id, "english")
                if english == "null":
                    english = romaji
                if env.get("ALLOW_RENAMING") == "Yes" and env.get("RENAME_SEASONS") == "Yes":
                    shown = english if env.get("MAIN_TITLE_ENG") == "Yes" else romaji
                    self.w("        title: |-\n          %s\n" % shown)
                label_add = "AA Winner" if self.cr_awards else ""
                label_remove = ""
                if lists_on:
                    remove = ",".join(USERLIST_TYPES)
                    if season_add:
                        for utype in season_add.split(","):
                            remove = remove_type(remove, utype)
                        label_add = "%s,%s" % (label_add, season_add) if label_add else season_add
                    if remove:
                        label_remove = remove
                if anime_season:
                    label_add = "%s,%s" % (label_add, anime_season) if label_add else anime_season
                for line in season_label_lines(label_add, label_remove, last1, ign_s1, ign_rating):
                    self.w(line)
                if not ign_rating and (not last1 or not ign_s1):
                    self.w("        user_rating: %s\n" % season_score[1])
                for n, on in ((1, r1), (2, r2)):
                    if on:
                        total[n] = awk_str(awk_num(season_score[n]) + awk_num(total[n]))
                self.get_season_poster()
            self.season_loop = 0
            for n, on in ((1, r1), (2, r2)):
                if not on:
                    continue
                score = "0"
                if total[n] != "0":
                    count = total_seasons - no_rating_seasons[n]
                    if count != 0:
                        score = fmt1(awk_str(awk_num(total[n]) / count))
                setattr(self, "score_%d" % n, score)
        else:
            for n in (1, 2):
                if self.src(n) in ("ANILIST", "MAL"):
                    setattr(self, "score_%d" % n, self.fetch_score(n))
                if getattr(self, "score_%d" % n) != "0":
                    setattr(self, "score_%d" % n, fmt1(getattr(self, "score_%d" % n)))
        self.anilist_id = backup

    # ----- one show / movie
    def write_metadata(self, key, seasons_list="", override_seasons_ignore=""):
        env = self.env
        self.load_al(self.anilist_id)
        self.w("  %s:\n" % key)
        romaji = self.get_title(self.anilist_id, "romaji")
        english = self.get_title(self.anilist_id, "english")
        native = jraw(self.load_al(self.anilist_id), "data", "Media", "title", "native").rstrip("\n")
        if english == "null":
            english = romaji
        if native == "null":
            native = romaji
        if env.get("ALLOW_RENAMING") == "Yes":
            if env.get("MAIN_TITLE_ENG") == "Yes":
                original = native if env.get("ORIGINAL_TITLE_NATIVE") == "Yes" else romaji
                self.w("    title: |-\n      %s\n    sort_title: |-\n      %s\n    original_title: |-\n      %s\n"
                       % (english, english, original))
            else:
                self.w("    title: |-\n      %s\n" % romaji)
                self.w("    sort_title: |-\n      %s\n" % (english if env.get("SORT_TITLE_ENG") == "Yes" else romaji))
                self.w("    original_title: |-\n      %s\n" % (native if env.get("ORIGINAL_TITLE_NATIVE") == "Yes" else english))
        if env.get("DISABLE_TAGS") != "Yes":
            if env.get("TAG_SOURCE") == "MAL":
                self.get_mal_tags()
            else:
                self.get_anilist_tags()
            if env.get("ADD_ANIME_TAG") == "No":
                self.w("    genre.sync: %s\n" % self.anime_tags)
            else:
                self.w("    genre.sync: Anime,%s\n" % self.anime_tags)
        studio = self.get_studios(self.anilist_id)
        if studio:
            self.w("    studio: %s\n" % studio)
        self.get_poster()
        if self.media == "animes":
            if env.get("IGNORE_SEASONS") == "Yes" or override_seasons_ignore == "yes":
                self.get_rating(1)
                self.get_rating(2)
            else:
                self.get_season_infos(seasons_list)
                self.check_rating_valid(1)
                self.check_rating_valid(2)
        else:
            self.get_rating(1)
            self.get_rating(2)
        label_add = ""
        label_remove = ""
        if self.media == "animes":
            self.log("\t\t - Writing airing status")
            if self.tvdb_id in self.ongoing:
                label_add, label_remove = "Airing", "Planned,Ended"
            elif self.get_airing_status() == "Planned":
                label_add, label_remove = "Planned", "Airing,Ended"
            else:
                label_add, label_remove = "Ended", "Planned,Airing"
            self.log("\t\t - Done")
        if not self.cr_awards:
            self.get_animes_award(self.anilist_id)
        if self.cr_awards:
            label_add = "AA Winner,%s" % label_add if label_add else "AA Winner"
        if env.get("ANILIST_LISTS") == "Yes" and env.get("ANILIST_LISTS_LEVEL") in ("show", "both"):
            add = ""
            remove = ",".join(USERLIST_TYPES)
            if self.media == "animes":
                ids = [a for a, _s, _o in self.mapping_by_tvdb.get(self.tvdb_id, ())]
            else:
                ids = [self.anilist_id]
            for utype in USERLIST_TYPES:
                if self.user_in_list(utype, ids):
                    add = add_type(add, utype)
                    remove = remove_type(remove, utype)
            if add:
                label_add = "%s,%s" % (label_add, add) if label_add else add
            if remove:
                label_remove = "%s,%s" % (label_remove, remove) if label_remove else remove
        if label_add:
            self.w("    label: %s\n" % label_add)
        if label_remove:
            self.w("    label.remove: %s\n" % label_remove)
        self.tvdb_id = ""
        self.anilist_id = ""
        self.mal_id = ""
        self.cr_awards = ""

    # ----- per-show fragment cache (see README / FRAGMENT_CACHE)
    def show_ids(self):
        ids = [self.anilist_id]
        if self.media == "animes":
            ids += [a for a, _s, _o in self.mapping_by_tvdb.get(self.tvdb_id, ())]
        return list(dict.fromkeys(ids))

    def fragment_key(self, key, seasons_list, override_seasons_ignore):
        ids = self.show_ids()
        mapped = ids[1:] if self.media == "animes" else []
        lists = None
        if self.userlists:
            lists = {utype: [self.user_in_list(utype, mapped), [self.user_in_list(utype, [i]) for i in ids]]
                     for utype in USERLIST_TYPES}
        inputs = {
            "version": SOURCE_HASH,
            "media": self.media,
            "env": [self.env.get(name) for name in KEY_ENV],
            "row": [key, self.tvdb_id, self.anilist_id, self.plex_title, self.asset_name, seasons_list,
                    override_seasons_ignore],
            "carried": {name: self.__dict__.get(name) for name in CARRIED},
            "override": [self.override.get(i) for i in ids],
            "mapping": [self.mapping_by_tvdb.get(self.tvdb_id), self.mapping_seasons.get(self.anilist_id)],
            "mal_fallback": [self.anilist_mal.get(i) for i in ids],
            "ongoing": self.tvdb_id in self.ongoing,
            "awards": [self.awards.get(i) for i in ids],
            "userlists": lists,
        }
        blob = json.dumps(inputs, sort_keys=True, ensure_ascii=True).encode("ascii")
        return hashlib.sha256(blob).hexdigest()

    @staticmethod
    def file_digest(path):
        try:
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return "-"

    def fragment_valid(self, frag):
        """Every cache JSON the show read must be byte-identical and still inside its TTL."""
        for name, digest, ttl in frag["deps"]:
            path = os.path.join(self.data_dir, name)
            if self.file_digest(path) != digest or not self.is_fresh(path, ttl):
                return False
        return True

    def load_fragment(self, ckey):
        try:
            with open(os.path.join(self.fragment_dir, ckey + ".json"), "rb") as fh:
                frag = json.loads(fh.read())
            if frag.get("key") == ckey and self.fragment_valid(frag):
                return frag
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def store_fragment(self, ckey, out_start):
        deps = []
        for path, ttl in self.touched.items():
            if ttl is None:
                status = jget(read_json(path), "data", "Media", "status")
                ttl = self.cache_ttl(status if isinstance(status, str) else "")
            deps.append([os.path.basename(path), self.file_digest(path), ttl])
        frag = {"key": ckey, "out": "".join(self.out[out_start:]), "logs": self.capture["logs"],
                "posters": self.capture["posters"], "deps": deps,
                "state": {name: self.__dict__.get(name) for name in CARRIED}}
        try:
            os.makedirs(self.fragment_dir, exist_ok=True)
            rr_fetch.atomic_write(os.path.join(self.fragment_dir, ckey + ".json"),
                                  json.dumps(frag, ensure_ascii=True).encode("ascii"))
        except OSError:
            pass  # a read-only cache folder only costs speed

    def replay_fragment(self, frag):
        self.w(frag["out"])
        for kind, text in frag["logs"]:
            (self.logger.log if kind == "log" else self.logger.match)(text)
        # posters are always checked / queued, exactly as in a miss
        backup = self.anilist_id
        self.capture = {"logs": [], "posters": []}
        try:
            for base, season, aid in frag["posters"]:
                self.anilist_id = aid
                self.queue_poster(os.path.join(self.asset_folder, self.asset_name, base), season)
        finally:
            self.capture = None
            self.anilist_id = backup
        for name, value in frag["state"].items():
            if value is None:
                self.__dict__.pop(name, None)
            else:
                setattr(self, name, value)
        self.tvdb_id = ""
        self.anilist_id = ""

    def process_show(self, key, seasons_list="", override_seasons_ignore=""):
        if not self.cache_enabled:
            self.write_metadata(key, seasons_list, override_seasons_ignore)
            return
        ckey = self.fragment_key(key, seasons_list, override_seasons_ignore)
        self.fragment_used.add(ckey)
        frag = self.load_fragment(ckey)
        if frag is not None:
            self.fragment_hits += 1
            self.replay_fragment(frag)
            return
        self.fragment_misses += 1
        self.capture = {"logs": [], "posters": []}
        self.touched = {}
        out_start = len(self.out)
        try:
            self.write_metadata(key, seasons_list, override_seasons_ignore)
            self.store_fragment(ckey, out_start)
        finally:
            self.capture = None

    def prune_fragments(self):
        """Drop fragments no show of this run used, so the folder does not grow forever."""
        try:
            names = os.listdir(self.fragment_dir)
        except OSError:
            return
        for name in names:
            if name.endswith(".json") and name[:-5] not in self.fragment_used:
                try:
                    os.unlink(os.path.join(self.fragment_dir, name))
                except OSError:
                    pass


def read_version(script_folder):
    for line in read_lines(os.path.join(script_folder, "VERSION")):
        m = re.match(r"\s*version=(.*)$", line)
        if m:
            return m.group(1).strip().strip("\"'")
    return ""


def run(script_folder, media, output, env=None, fetch=rr_fetch.default_fetch, sleep=time.sleep):
    started = time.perf_counter()
    env = os.environ if env is None else env
    log_folder = env.get("LOG_FOLDER") or os.path.join(script_folder, "config", "logs")
    logger = Logger(os.path.join(log_folder, "RR_%s.log" % time.strftime("%Y.%m.%d")),
                    os.path.join(log_folder, "%s-missing-id.log" % media))
    client = rr_fetch.Client(os.path.join(script_folder, "config", "data"), logger.log, fetch, sleep)
    writer = Writer(script_folder, media, env, logger, client)
    status = 0
    try:
        logger.log(" - Start writing the metadata file ")
        writer.w("# Romaji-Renamer v%s\n" % read_version(script_folder))
        writer.w("metadata:\n")
        if media == "animes":
            rows = read_lines(os.path.join(script_folder, "config", "ID", "animes.tsv"))
            for line in rows:
                tvdb, aid, title, asset, seasons, ignore = bash_read(line, 6)
                writer.tvdb_id, writer.anilist_id, writer.plex_title, writer.asset_name = tvdb, aid, title, asset
                logger.log("\t - Writing metadata for %s / tvdb : %s / Anilist : %s " % (title, tvdb, aid))
                writer.process_show(tvdb, seasons, ignore)
                logger.log("\t - Done")
        else:
            rows = read_lines(os.path.join(script_folder, "config", "ID", "movies.tsv"))
            for line in rows:
                imdb, aid, title, asset = bash_read(line, 4)
                writer.anilist_id, writer.plex_title, writer.asset_name = aid, title, asset
                logger.log("\t - Writing metadata for %s / imdb : %s / Anilist : %s " % (title, imdb, aid))
                writer.process_show(imdb)
                logger.log("\t - Done")
        tmp = "%s.%d.tmp" % (output, os.getpid())
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            fh.write("".join(writer.out))
        os.replace(tmp, output)
        writer.run_poster_downloads()
        if writer.cache_enabled:
            writer.prune_fragments()
            logger.log(" - Fragment cache: %d hits, %d misses" % (writer.fragment_hits, writer.fragment_misses))
        logger.log(" - Run finished")
        logger.log(" - Total time %.2fs" % (time.perf_counter() - started))
    except (FatalError, rr_fetch.FetchError) as err:
        logger.log(" - Error: %s, stopping script" % err)
        status = 1
    finally:
        logger.close()
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write the PMM metadata file (port of write-metadata)")
    parser.add_argument("--script-folder", required=True)
    parser.add_argument("--media", required=True, choices=("animes", "movies"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    return run(os.path.abspath(args.script_folder), args.media, os.path.abspath(args.output))


if __name__ == "__main__":
    sys.exit(main())
