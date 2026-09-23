import unittest

from ankictl import repair_markers


class RepairMarkersTest(unittest.TestCase):
    def test_preserves_card_identity_and_ignores_unrelated_tags(self):
        notes = [{
            "noteId": 17,
            "decks": ["Course::Unit"],
            "tags": [
                "weibao::repair::explanation::card_101",
                "weibao::repair::confusion::card_102",
                "weibao::repair::explanation::card_bad",
                "other::tag",
            ],
            "fields": {"Front": {"order": 0, "value": "<b>Which?</b>"}},
        }]

        self.assertEqual(repair_markers(notes), [
            {"noteId": 17, "cardId": 101, "reason": "explanation",
             "decks": ["Course::Unit"], "preview": "Which?"},
            {"noteId": 17, "cardId": 102, "reason": "confusion",
             "decks": ["Course::Unit"], "preview": "Which?"},
        ])


if __name__ == "__main__":
    unittest.main()
