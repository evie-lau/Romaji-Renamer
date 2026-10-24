"""Small rate-limited AniList / MAL client used by write_metadata.py (stdlib only).

Mirrors get-anilist-infos / get-mal-infos from functions.sh. Everything that touches the
outside world (HTTP, sleeping, clock, logging) is injectable so it can be unit tested offline.

``fetch(url, data=None, headers=None, timeout=30)`` must return ``(status, headers, body)`` where
``headers`` has lower-case keys and ``body`` is bytes; it may raise on transport errors.
"""
import json
import os
import time
import urllib.error
import urllib.request

ANILIST_URL = "https://graphql.anilist.co/"
MAL_URL = "https://malapi.animemap.dev/v4/anime/%s"
ANILIST_QUERY = (
    "{ Media(type: ANIME, id: %s) { relations { edges { relationType node { id type format title { romaji } status } } } "
    "title { romaji(stylised: false) english(stylised: false) native(stylised: false) } averageScore genres "
    "tags { name rank } studios { edges { node { name isAnimationStudio } } } startDate { year month } season "
    "seasonYear coverImage { extraLarge } status idMal} }"
)
USER_AGENT = "Romaji-Renamer"


class FetchError(Exception):
    pass


def default_fetch(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}))
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        body = err.read()
        return err.code, {k.lower(): v for k, v in err.headers.items()}, body


def valid_anilist(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), dict):
        return False
    media = doc["data"].get("Media")
    return isinstance(media, dict) and isinstance(media.get("status"), str)


def valid_mal(doc):
    return isinstance(doc, dict) and isinstance(doc.get("data"), dict)


def parse_json(body):
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def atomic_write(path, body):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Client:
    def __init__(self, data_dir, log, fetch=default_fetch, sleep=time.sleep):
        self.data_dir = data_dir
        self.log = log  # log(text): text starts after the HH:MM:SS prefix
        self.fetch = fetch
        self.sleep = sleep

    def anilist_path(self, anilist_id):
        return os.path.join(self.data_dir, "anilist-%s.json" % anilist_id)

    def mal_path(self, mal_id):
        return os.path.join(self.data_dir, "MAL-%s.json" % mal_id)

    def fetch_anilist(self, anilist_id, label=""):
        """Download one AniList media, save it atomically and return the parsed document.

        Raises FetchError after 4 failed/invalid answers (bash exits the script there).
        ``label`` is the log text of the "Downloading ..." line (after the timestamp).
        """
        payload = json.dumps({"query": ANILIST_QUERY % anilist_id}).encode()
        headers = {"content-type": "application/json"}
        retries = 0
        self.log(label)
        while True:
            try:
                _status, resp_headers, body = self.fetch(ANILIST_URL, payload, headers)
            except Exception:
                resp_headers, body = {}, None
            if body is not None:
                doc = parse_json(body)
                if valid_anilist(doc):
                    atomic_write(self.anilist_path(anilist_id), body)
                    break
                self.log(" - Invalid json from AniList API down, waiting 60s")
            else:
                self.log(" - AniList request failed, waiting 60s")
            retries += 1
            self.sleep(61)
            if retries >= 4:
                self.log(" - Error can't download anilist data stopping script")
                raise FetchError("anilist %s" % anilist_id)
        remaining = (resp_headers or {}).get("x-ratelimit-remaining", "")
        if not remaining.isdigit():
            self.log("\t\t - Cloudflare limit rate reached watiting 60s")
            self.sleep(61)
        elif int(remaining) >= 3:
            self.sleep(1)
        else:
            self.log("\t\t - Anilist API limit reached watiting 30s")
            self.sleep(30)
        self.log("\t\t - Done")
        return doc

    def fetch_mal(self, mal_id, have_cached=False, label=""):
        """Download one MAL entry (3 attempts like mal-fetch-one). Returns the document or None."""
        self.log(label)
        url = MAL_URL % mal_id
        for attempt in range(1, 4):
            status = 0
            body = None
            try:
                status, _headers, body = self.fetch(url, None, {})
            except Exception:
                status = 0
            doc = parse_json(body) if status == 200 and body is not None else None
            if valid_mal(doc):
                atomic_write(self.mal_path(mal_id), body)
                self.sleep(1.1)
                self.log("\t\t - Done")
                return doc
            self.log("\t - MAL %s attempt %d/3 failed (HTTP %03d)" % (mal_id, attempt, status))
            if attempt < 3:
                self.sleep(30 if status == 429 else attempt * 5)
        if have_cached:
            self.log(" - MAL refresh failed, retaining cached data for MAL : %s" % mal_id)
        else:
            self.log(" - Error can't download valid MAL data for MAL : %s" % mal_id)
        return None

    def download_binary(self, url, dest, min_size=10000, timeout=30):
        """Download url to dest through a temp file; return True when a file of >= min_size was stored."""
        tmp = "%s.%d.%s.tmp" % (dest, os.getpid(), id(url))
        try:
            status, _headers, body = self.fetch(url, None, {}, timeout)
            if status != 200 or body is None or len(body) < min_size:
                return False
            with open(tmp, "wb") as fh:
                fh.write(body)
            os.replace(tmp, dest)
            return True
        except Exception:
            return False
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
