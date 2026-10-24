import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import rr_fetch  # noqa: E402
import write_metadata as wm  # noqa: E402

GOOD_ANILIST = json.dumps({"data": {"Media": {"status": "FINISHED", "idMal": 5}}}).encode()
GOOD_MAL = json.dumps({"data": {"score": 7.5}}).encode()


class Stub:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, data=None, headers=None, timeout=30):
        self.calls.append((url, data))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sleeps = []
        self.logs = []

    def client(self, responses):
        self.stub = Stub(responses)
        return rr_fetch.Client(self.tmp.name, self.logs.append, self.stub, self.sleeps.append)

    def test_anilist_success_and_rate_limit_sleep(self):
        client = self.client([(200, {"x-ratelimit-remaining": "50"}, GOOD_ANILIST)])
        doc = client.fetch_anilist("12", "\t\t - Downloading data for anilist : 12")
        self.assertEqual(doc["data"]["Media"]["status"], "FINISHED")
        self.assertEqual(self.sleeps, [1])
        with open(os.path.join(self.tmp.name, "anilist-12.json"), "rb") as fh:
            self.assertEqual(fh.read(), GOOD_ANILIST)
        self.assertEqual(os.listdir(self.tmp.name), ["anilist-12.json"])
        query = json.loads(self.stub.calls[0][1])["query"]
        self.assertIn("Media(type: ANIME, id: 12)", query)

    def test_anilist_low_remaining_waits_30(self):
        client = self.client([(200, {"x-ratelimit-remaining": "2"}, GOOD_ANILIST)])
        client.fetch_anilist("12")
        self.assertEqual(self.sleeps, [30])

    def test_anilist_missing_header_waits_60(self):
        client = self.client([(200, {}, GOOD_ANILIST)])
        client.fetch_anilist("12")
        self.assertEqual(self.sleeps, [61])

    def test_anilist_retries_invalid_then_succeeds(self):
        client = self.client([(200, {}, b'{"data": null}'), OSError("down"),
                              (200, {"x-ratelimit-remaining": "9"}, GOOD_ANILIST)])
        client.fetch_anilist("12")
        self.assertEqual(self.sleeps, [61, 61, 1])
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.tmp.name)))

    def test_anilist_gives_up_after_four_failures(self):
        client = self.client([(429, {}, b"nope")] * 4)
        with self.assertRaises(rr_fetch.FetchError):
            client.fetch_anilist("12")
        self.assertEqual(self.sleeps, [61] * 4)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_mal_success(self):
        client = self.client([(200, {}, GOOD_MAL)])
        doc = client.fetch_mal("5")
        self.assertEqual(doc["data"]["score"], 7.5)
        self.assertEqual(self.stub.calls[0][0], "https://malapi.animemap.dev/v4/anime/5")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "MAL-5.json")))

    def test_mal_retry_429_then_ok(self):
        client = self.client([(429, {}, b""), (200, {}, b"not json"), (200, {}, GOOD_MAL)])
        self.assertIsNotNone(client.fetch_mal("5"))
        self.assertEqual(self.sleeps[:2], [30, 10])

    def test_mal_failure_returns_none(self):
        client = self.client([(500, {}, b"")] * 3)
        self.assertIsNone(client.fetch_mal("5"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "MAL-5.json")))

    def test_download_binary_validates_size(self):
        dest = os.path.join(self.tmp.name, "poster.jpg")
        client = self.client([(200, {}, b"x" * 100), (200, {}, b"x" * 20000)])
        self.assertFalse(client.download_binary("http://img/a.jpg", dest))
        self.assertFalse(os.path.exists(dest))
        self.assertTrue(client.download_binary("http://img/a.jpg", dest))
        self.assertEqual(os.path.getsize(dest), 20000)


class WriterFallbackTest(unittest.TestCase):
    """Writer.load_al falls back to the (stubbed) network when the cache is missing or invalid."""

    def make(self, files, responses):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = os.path.join(tmp.name, "config", "data")
        os.makedirs(data)
        os.makedirs(os.path.join(tmp.name, "config", "logs"))
        for name, body in files.items():
            with open(os.path.join(data, name), "wb") as fh:
                fh.write(body)
        logger = wm.Logger(os.path.join(tmp.name, "config", "logs", "a.log"), os.path.join(tmp.name, "config", "logs", "m.log"))
        self.addCleanup(logger.close)
        stub = Stub(responses)
        client = rr_fetch.Client(data, lambda t: None, stub, lambda s: None)
        return wm.Writer(tmp.name, "animes", {"DATA_CACHE_TIME": "5"}, logger, client), stub, data

    def test_missing_cache_is_fetched(self):
        writer, stub, data = self.make({}, [(200, {"x-ratelimit-remaining": "9"}, GOOD_ANILIST)])
        doc = writer.load_al("77")
        self.assertEqual(doc["data"]["Media"]["idMal"], 5)
        self.assertTrue(os.path.exists(os.path.join(data, "anilist-77.json")))
        writer.load_al("77")
        self.assertEqual(len(stub.calls), 1)

    def test_fresh_cache_is_not_fetched(self):
        writer, stub, _ = self.make({"anilist-77.json": GOOD_ANILIST}, [])
        self.assertIsNotNone(writer.load_al("77"))
        self.assertEqual(stub.calls, [])

    def test_invalid_cache_is_refetched(self):
        writer, stub, _ = self.make({"anilist-77.json": b"{broken"}, [(200, {"x-ratelimit-remaining": "9"}, GOOD_ANILIST)])
        self.assertIsNotNone(writer.load_al("77"))
        self.assertEqual(len(stub.calls), 1)

    def test_airing_walk_fetches_missing_sequel(self):
        sequel = json.dumps({"data": {"Media": {"status": "FINISHED", "relations": {"edges": []}}}}).encode()
        main = json.dumps({"data": {"Media": {"status": "FINISHED", "relations": {"edges": [
            {"relationType": "SEQUEL", "node": {"id": 78, "format": "TV", "status": "FINISHED"}}]}}}}).encode()
        writer, stub, _ = self.make({"anilist-77.json": main}, [(200, {"x-ratelimit-remaining": "9"}, sequel)])
        writer.anilist_id = "77"
        self.assertEqual(writer.get_airing_status(), "Ended")
        self.assertEqual(len(stub.calls), 1)


if __name__ == "__main__":
    unittest.main()
