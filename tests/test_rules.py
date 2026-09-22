from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from ndabudilka.rules import (DEFAULT_WORDS, clip_utf8, match_words, message_topic,
                             parse_schedule, parse_source, parse_words, priority_for)


class MatchingTests(unittest.TestCase):
    def test_case_punctuation_and_cyrillic(self):
        self.assertEqual(match_words("#short / LONG. шОрТ, ЛОНГ!", DEFAULT_WORDS), DEFAULT_WORDS)

    def test_substrings_are_not_words(self):
        self.assertEqual(match_words("shorter longing шорты лонговый aSHORT LONG_1", DEFAULT_WORDS), [])

    def test_duplicates_phrases_and_literal_hyphen(self):
        self.assertEqual(parse_words("SHORT, short;лонг\nLONG"), ["SHORT", "лонг", "LONG"])
        self.assertEqual(match_words("open long; risk-on", ["open long", "risk-on"]), ["open long", "risk-on"])

    def test_invalid_word_lists(self):
        for value in ("", ", ,", "a" * 65, "s.*", ",".join(str(n) for n in range(51))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_words(value)

    def test_utf8_truncation(self):
        clipped = clip_utf8("Лонг 🚀" * 2000, 3000)
        self.assertLessEqual(len(clipped.encode("utf-8")), 3000)
        self.assertTrue(clipped.endswith("…"))
        self.assertNotIn("�", clipped)


class NightTests(unittest.TestCase):
    def setUp(self):
        self.user = dict(night_enabled=1, night_start=60, night_end=420, night_priority=2)

    def utc(self, hour, minute=0):
        return datetime(2026, 9, 21, hour, minute, tzinfo=timezone.utc)

    def test_moscow_exact_boundaries(self):
        self.assertEqual(priority_for(self.user, self.utc(21, 59)), 5)  # 00:59 MSK
        self.assertEqual(priority_for(self.user, self.utc(22)), 2)     # 01:00 MSK
        self.assertEqual(priority_for(self.user, self.utc(3, 59)), 2)  # 06:59 MSK
        self.assertEqual(priority_for(self.user, self.utc(4)), 5)      # 07:00 MSK

    def test_period_across_midnight(self):
        self.user.update(night_start=1380, night_end=480)
        for hour in (20, 22, 0, 4):
            self.assertEqual(priority_for(self.user, self.utc(hour)), 2)
        for hour in (5, 10, 19):
            self.assertEqual(priority_for(self.user, self.utc(hour)), 5)

    def test_disabled_uses_maximum(self):
        self.user["night_enabled"] = 0
        self.assertEqual(priority_for(self.user, self.utc(22)), 5)

    def test_schedule_validation(self):
        self.assertEqual(parse_schedule("01:00-07:00"), (60, 420))
        self.assertEqual(parse_schedule("23:00 – 08:00"), (1380, 480))
        for value in ("24:00-07:00", "01:60-07:00", "01:00-01:00", "1-7"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_schedule(value)


class TopicTests(unittest.TestCase):
    def test_direct_and_nested_topic_replies(self):
        direct = SimpleNamespace(reply_to=SimpleNamespace(forum_topic=True, reply_to_msg_id=42, reply_to_top_id=None))
        nested = SimpleNamespace(reply_to=SimpleNamespace(forum_topic=True, reply_to_msg_id=99, reply_to_top_id=42))
        self.assertEqual(message_topic(direct, True), 42)
        self.assertEqual(message_topic(nested, True), 42)

    def test_general_and_non_forum_replies(self):
        reply = SimpleNamespace(reply_to=SimpleNamespace(forum_topic=False, reply_to_msg_id=99))
        self.assertEqual(message_topic(reply, True), 1)
        self.assertEqual(message_topic(reply, False), 0)
        self.assertEqual(message_topic(SimpleNamespace(reply_to=None), True), 1)

    def test_source_ids(self):
        self.assertEqual(parse_source("-1001234567890 42"), (-1001234567890, 42))
        self.assertEqual(parse_source("-1001234567890"), (-1001234567890, 0))
        for value in ("123", "@channel", "-1001234567890 -2", "-1001234567890 test", "-1001234567890 1 2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_source(value)
