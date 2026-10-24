import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import write_metadata as wm  # noqa: E402


class RoundingTest(unittest.TestCase):
    def test_long_double_half_cases(self):
        # bash printf '%.1f' (x87 long double) vs python float formatting
        expected = {"6.45": "6.4", "7.55": "7.6", "7.95": "7.9", "0.15": "0.2", "8.05": "8.1",
                    "2.25": "2.2", "0.25": "0.2", "9.95": "9.9", "7.84": "7.8", "8.3": "8.3"}
        for text, want in expected.items():
            self.assertEqual(wm.fmt1(text), want, text)

    def test_python_float_would_differ(self):
        self.assertEqual("%.1f" % 6.45, "6.5")
        self.assertEqual(wm.fmt1("6.45"), "6.4")

    def test_empty_and_zero(self):
        self.assertEqual(wm.fmt1(""), "0.0")
        self.assertEqual(wm.fmt1("0"), "0.0")
        self.assertEqual(wm.fmt1("junk"), "0.0")
        self.assertEqual(wm.fmt1("7"), "7.0")

    def test_awk_helpers(self):
        self.assertEqual(wm.awk_str(wm.awk_num("78") / 10), "7.8")
        self.assertEqual(wm.awk_str(0.0), "0")
        self.assertEqual(wm.awk_str(15.0), "15")
        self.assertEqual(wm.awk_str(20 / 3), "6.66667")
        self.assertEqual(wm.awk_num(""), 0.0)


class TitleTest(unittest.TestCase):
    def test_caps_title(self):
        self.assertEqual(wm.less_caps_title("86 EIGHTY-SIX"), "86 Eighty-six")
        self.assertEqual(wm.less_caps_title("POKÉMON"), "PokÉmon")
        self.assertEqual(wm.less_caps_title("Mixed Case"), "Mixed Case")
        self.assertEqual(wm.less_caps_title("86"), "86")
        self.assertEqual(wm.less_caps_title("ONE PIECE"), "One Piece")
        self.assertEqual(wm.less_caps_title("ONE PIECE", enabled=False), "ONE PIECE")

    def test_season_year_capitalisation(self):
        self.assertEqual(wm.capitalise_words("2020 fall"), "2020 Fall")
        self.assertEqual(wm.capitalise_words("null null"), "Null Null")


class LabelTest(unittest.TestCase):
    def test_userlist_remove_leaves_trailing_comma(self):
        remove = ",".join(wm.USERLIST_TYPES)
        self.assertEqual(wm.remove_type(remove, "Completed"), "Watching,Dropped,Paused,Planning")
        self.assertEqual(wm.remove_type(remove, "Planning"), "Completed,Watching,Dropped,Paused,")
        once = wm.remove_type(remove, "Completed")
        self.assertEqual(wm.remove_type(once, "Planning"), "Watching,Dropped,Paused,")

    def test_userlist_add_last_match_wins(self):
        add = ""
        for t in ("Completed", "Watching"):
            add = wm.add_type(add, t)
        self.assertEqual(add, "Watching")

    def test_season_labels(self):
        f = wm.season_label_lines
        self.assertEqual(f("2020 Fall", "", False, True, False), ["        label: Score,2020 Fall\n"])
        self.assertEqual(f("2020 Fall", "", True, True, False), ["        label: 2020 Fall\n",
                                                                  "        label.remove: Score\n"])
        # bash tests `!= Yes || != Yes` here, so "label: Score" is printed unless both flags are Yes
        self.assertEqual(f("", "", True, True, False), ["        label: Score\n", "        label.remove: Score\n"])
        self.assertEqual(f("", "", True, True, True), ["        label.remove: Score\n"])
        self.assertEqual(f("", "", True, False, False), ["        label: Score\n"])
        self.assertEqual(f("", "", False, False, True), ["        label.remove: Score\n"])
        self.assertEqual(f("AA Winner", "Planning,", False, False, True),
                         ["        label: AA Winner\n", "        label.remove: Score,Planning,\n"])
        self.assertEqual(f("", "Dropped", False, False, False), ["        label: Score\n",
                                                                   "        label.remove: Dropped\n"])

    def test_trailing_comma_season_year(self):
        # a skipped (not yet released) first cour leaves a trailing comma
        all_cours = ""
        for count, season in ((2, "2023 Fall"),):
            all_cours = "%s,%s" % (season, all_cours) if count > 1 else season
        self.assertEqual(all_cours, "2023 Fall,")


class ParsingTest(unittest.TestCase):
    def test_bash_read_collapses_tabs(self):
        self.assertEqual(wm.bash_read("tt1\t\t12\tTitle\tasset", 4), ["tt1", "12", "Title", "asset"])
        self.assertEqual(wm.bash_read("1\t2\tT\tA\t1,2\t", 6), ["1", "2", "T", "A", "1,2", ""])
        self.assertEqual(wm.bash_read("\t1\t2", 4), ["1", "2", "", ""])


def doc(status="FINISHED", sequels=()):
    edges = [{"relationType": "SEQUEL", "node": {"id": i, "format": fmt, "status": st}} for i, fmt, st in sequels]
    return {"data": {"Media": {"status": status, "relations": {"edges": edges}}}}


class AiringTest(unittest.TestCase):
    def walk(self, docs, start="1"):
        seen = []

        def get(i):
            seen.append(i)
            return docs.get(i)
        return wm.walk_airing_status(start, get), seen

    def test_main_not_yet_released(self):
        self.assertEqual(self.walk({"1": doc("NOT_YET_RELEASED")})[0], "Planned")

    def test_no_sequel_ended(self):
        self.assertEqual(self.walk({"1": doc()})[0], "Ended")

    def test_chain_to_planned(self):
        docs = {"1": doc(sequels=[(2, "TV", "FINISHED")]), "2": doc(sequels=[(3, "TV", "NOT_YET_RELEASED")])}
        self.assertEqual(self.walk(docs)[0], "Planned")

    def test_ignored_formats(self):
        docs = {"1": doc(sequels=[(2, "SPECIAL", "NOT_YET_RELEASED")])}
        self.assertEqual(self.walk(docs)[0], "Ended")
        docs = {"1": doc(sequels=[(2, None, "NOT_YET_RELEASED")])}
        self.assertEqual(self.walk(docs)[0], "Planned")

    def test_cycle_stops_after_50_hops(self):
        docs = {"1": doc(sequels=[(2, "TV", "FINISHED")]), "2": doc(sequels=[(1, "TV", "FINISHED")])}
        status, seen = self.walk(docs)
        self.assertEqual(status, "Ended")
        self.assertEqual(len(seen), 51)  # initial status check + 50 hops

    def test_multi_sequel_first_hop_reads_all_ids(self):
        # 1 -> {2, 3}; first multi step (file has a trailing newline) reads both, 3 -> NYR 6
        docs = {"1": doc(sequels=[(2, "TV", "FINISHED"), (3, "TV", "FINISHED")]),
                "2": doc(), "3": doc(sequels=[(6, "TV", "NOT_YET_RELEASED")])}
        self.assertEqual(self.walk(docs)[0], "Planned")

    def test_multi_to_multi_skips_last_id(self):
        # 1 -> {2,3}; 2 -> {4,5}; the multi->multi step writes the id file without a trailing newline so
        # `while read` drops id 5, whose sequel 7 is not yet released: bash answers Ended
        docs = {"1": doc(sequels=[(2, "TV", "FINISHED"), (3, "TV", "FINISHED")]),
                "2": doc(sequels=[(4, "TV", "FINISHED"), (5, "TV", "FINISHED")]),
                "3": doc(),
                "4": doc(), "5": doc(sequels=[(7, "TV", "NOT_YET_RELEASED")])}
        status, seen = self.walk(docs)
        self.assertEqual(status, "Ended")
        self.assertIn("4", seen)
        self.assertNotIn("5", seen)

    def test_multi_to_multi_reads_earlier_ids(self):
        docs = {"1": doc(sequels=[(2, "TV", "FINISHED"), (3, "TV", "FINISHED")]),
                "2": doc(sequels=[(4, "TV", "FINISHED"), (5, "TV", "FINISHED")]),
                "3": doc(),
                "4": doc(sequels=[(7, "TV", "NOT_YET_RELEASED")]), "5": doc()}
        self.assertEqual(self.walk(docs)[0], "Planned")


if __name__ == "__main__":
    unittest.main()
