import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import write_metadata as wm  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "golden", "make_fixture.py")
BASE_ENV = os.path.join(ROOT, "tests", "golden", "scenarios", "_base.env")


FETCH_CALLS = []


def offline_fetch(url, *_args, **_kwargs):
    FETCH_CALLS.append(url)  # rr_fetch swallows exceptions, so count the attempts instead
    raise OSError("offline")


def base_env():
    env = {}
    with open(BASE_ENV) as fh:
        for line in fh:
            m = re.match(r"([A-Z0-9_]+)=(.*)$", line.strip())
            if m:
                env[m.group(1)] = m.group(2)
    return env


class FragmentCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = os.path.join(self.tmp.name, "fx")
        subprocess.run([sys.executable, FIXTURE, "--out", self.folder], check=True, stdout=subprocess.DEVNULL)
        self.env = base_env()
        self.env["LOG_FOLDER"] = os.path.join(self.tmp.name, "logs")
        self.total = 20
        del FETCH_CALLS[:]

    def run_once(self, name="out.yml", **env_changes):
        env = dict(self.env, **env_changes)
        out = os.path.join(self.tmp.name, name)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status = wm.run(self.folder, "animes", out, env=env, fetch=offline_fetch, sleep=lambda _s: None)
        self.assertEqual(status, 0)
        m = re.search(r"Fragment cache: (\d+) hits, (\d+) misses", buf.getvalue())
        self.assertIsNotNone(m, buf.getvalue())
        with open(out) as fh:
            return int(m.group(1)), int(m.group(2)), fh.read()

    def test_cold_then_warm_identical(self):
        hits, misses, cold = self.run_once()
        self.assertEqual((hits, misses), (0, self.total))
        hits, misses, warm = self.run_once("warm.yml")
        self.assertEqual((hits, misses), (self.total, 0))
        self.assertEqual(cold, warm)
        self.assertEqual(FETCH_CALLS, [])

    def test_disabled_matches_and_writes_nothing(self):
        _h, _m, cached = self.run_once()
        fragments = os.path.join(self.folder, "config", "tmp", "fragments", "animes")
        for name in os.listdir(fragments):
            os.unlink(os.path.join(fragments, name))
        env = dict(self.env, FRAGMENT_CACHE="No")
        out = os.path.join(self.tmp.name, "off.yml")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(wm.run(self.folder, "animes", out, env=env, fetch=offline_fetch), 0)
        self.assertNotIn("Fragment cache", buf.getvalue())
        self.assertEqual(os.listdir(fragments), [])
        with open(out) as fh:
            self.assertEqual(fh.read(), cached)

    def test_ongoing_membership_invalidates_only_that_show(self):
        _h, _m, before = self.run_once()
        path = os.path.join(self.folder, "config", "data", "ongoing.tsv")
        with open(path, "w") as fh:
            fh.write("1004\n")  # 1009 leaves the airing list
        hits, misses, after = self.run_once("after.yml")
        self.assertEqual((hits, misses), (self.total - 1, 1))
        self.assertNotEqual(before, after)

    def test_env_setting_invalidates_everything(self):
        self.run_once()
        hits, misses, _ = self.run_once("after.yml", ADD_ANIME_TAG="No")
        self.assertEqual((hits, misses), (0, self.total))

    def test_unrelated_env_setting_keeps_fragments(self):
        self.run_once()
        hits, misses, _ = self.run_once("after.yml", LOG_FOLDER=self.env["LOG_FOLDER"] + "2")
        self.assertEqual((hits, misses), (self.total, 0))

    def test_cache_json_change_invalidates_only_that_show(self):
        _h, _m, before = self.run_once()
        path = os.path.join(self.folder, "config", "data", "anilist-2001.json")
        with open(path) as fh:
            doc = json.load(fh)
        doc["data"]["Media"]["title"]["romaji"] += " Changed"
        with open(path, "w") as fh:
            json.dump(doc, fh)
        hits, misses, after = self.run_once("after.yml")
        self.assertEqual((hits, misses), (self.total - 1, 1))
        self.assertIn("Changed", after)
        self.assertNotIn("Changed", before)

    def test_stale_cache_file_is_not_a_hit(self):
        self.run_once()
        path = os.path.join(self.folder, "config", "data", "anilist-2001.json")
        old = os.stat(path).st_mtime - 400 * 86400
        os.utime(path, (old, old))
        hits, misses, _ = self.run_once("after.yml")
        self.assertEqual((hits, misses), (self.total - 1, 1))
        self.assertTrue(FETCH_CALLS)

    def test_source_hash_is_sha256(self):
        self.assertEqual(len(wm.SOURCE_HASH), 64)


if __name__ == "__main__":
    unittest.main()
