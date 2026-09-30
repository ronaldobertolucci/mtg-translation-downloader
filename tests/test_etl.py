import io
import json
import unittest

from mtg_translation_downloader.etl import ALLOWED_SETS, consolidate, eligible, group_cards, iter_cards


def card(**changes):
    return dict({
        "id": "printing-1", "oracle_id": "oracle-1", "lang": "pt", "promo": False,
        "textless": False, "set_type": "expansion", "released_at": "2024-01-01",
        "name": "Lightning Bolt", "type_line": "Instant", "oracle_text": "English rules",
        "printed_name": "Raio", "printed_type_line": "Mágica Instantânea",
        "printed_text": "Regras novas",
    }, **changes)


class StreamingTests(unittest.TestCase):
    def test_fragmented_unicode_and_escaped_strings(self):
        cards = [card(printed_text='Ação, [texto] "com aspas" \\ e\nlinha'), card()]
        for chunk in (1, 2, 13, 65536):
            with self.subTest(chunk=chunk):
                self.assertEqual(list(iter_cards(io.StringIO(json.dumps(cards, ensure_ascii=False)), chunk)), cards)

    def test_bad_json_is_rejected(self):
        for value in ('', '{}', '[{}', '[{},]', '[{},', '[{} {}]', '[{}]junk', '[null]', '[1]', '[}'): 
            with self.subTest(value=value), self.assertRaises(ValueError):
                list(iter_cards(io.StringIO(value), chunk_size=1))

    def test_empty_and_whitespace(self):
        self.assertEqual(list(iter_cards(io.StringIO(' \n[ ] \t'), 1)), [])

    def test_bounded_reads(self):
        class Bounded(io.StringIO):
            def read(self, size=-1):
                if size < 0 or size > 32:
                    raise AssertionError("Unbounded read")
                return super().read(size)
        self.assertEqual(len(list(iter_cards(Bounded(json.dumps([card()] * 500)), 32))), 500)

    def test_oversized_object(self):
        with self.assertRaises(ValueError):
            list(iter_cards(io.StringIO(json.dumps([card()])), 8, 32))


class MergeTests(unittest.TestCase):
    def merge(self, cards):
        return list(consolidate(group_cards(cards)))

    def test_filters(self):
        for set_type in ALLOWED_SETS:
            self.assertTrue(eligible(card(set_type=set_type)))
        for changes in ({"lang": "en"}, {"promo": True}, {"textless": True},
                        {"set_type": "funny"}, {"set_type": "promo"}, {"set_type": None}):
            self.assertEqual(self.merge([card(**changes)]), [])

    def test_latest_rules_and_historical_flavor(self):
        old = card(released_at="2020-01-01", printed_name="Nome antigo", printed_text="Antigo",
                   printed_flavor_text="História antiga")
        latest = card(printed_flavor_text=" \n")
        older = card(released_at="2010-01-01", printed_flavor_text="Muito antigo")
        result = self.merge([older, latest, old])[0]
        self.assertEqual(result["name"], "Raio")
        self.assertEqual(result["oracle_text"], "Regras novas")
        self.assertEqual(result["type_line"], "Mágica Instantânea")
        self.assertEqual(result["flavor_text"], "História antiga")

    def test_selects_latest_complete_impression_without_mixing_fields(self):
        latest = card(printed_name=None, printed_text="", printed_type_line=None,
                      flavor_text="Flavor mais recente")
        valid = card(released_at="2020-01-01", printed_name="Nome válido",
                     printed_text="Regras válidas", printed_type_line="Tipo válido")
        older = card(released_at="2000-01-01", printed_name="Nome antigo")
        result = self.merge([older, latest, valid])[0]
        self.assertEqual(result["name"], "Nome válido")
        self.assertEqual(result["oracle_text"], "Regras válidas")
        self.assertEqual(result["type_line"], "Tipo válido")
        self.assertEqual(result["flavor_text"], "Flavor mais recente")

    def test_incomplete_translation_is_skipped(self):
        for field in ("printed_name", "printed_type_line", "printed_text"):
            for value in (None, "", " \n", 42):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.merge([card(**{field: value})]), [])
            missing = card()
            del missing[field]
            self.assertEqual(self.merge([missing]), [])

    def test_no_rules_allows_empty_printed_text(self):
        for printed in (None, "", " \n"):
            for oracle in (None, "", " \n"):
                with self.subTest(printed=printed, oracle=oracle):
                    result = self.merge([card(printed_text=printed, oracle_text=oracle)])[0]
                    self.assertEqual(result["oracle_text"], "")
        missing = card()
        del missing["printed_text"], missing["oracle_text"]
        self.assertEqual(self.merge([missing])[0]["oracle_text"], "")

    def test_native_flavor_field_and_printed_priority(self):
        result = self.merge([card(flavor_text="Nativo")])[0]
        self.assertEqual(result["flavor_text"], "Nativo")
        result = self.merge([card(flavor_text="Nativo", printed_flavor_text="Impresso")])[0]
        self.assertEqual(result["flavor_text"], "Impresso")

    def test_faces_merge_independently(self):
        latest = card(card_faces=[
            {"printed_name": "Frente", "printed_type_line": "Criatura", "printed_text": "Nova frente"},
            {"printed_name": "Verso", "printed_type_line": "Criatura", "printed_text": "Novo verso",
             "printed_flavor_text": "Verso novo"},
        ], printed_flavor_text="Não usar raiz")
        old = card(released_at="2000-01-01", card_faces=[
            {"printed_name": "Velha", "printed_flavor_text": "Frente antiga"},
            {"printed_name": "Velho", "printed_flavor_text": "Verso antigo"},
        ])
        result = self.merge([old, latest])[0]
        self.assertNotIn("name", result)
        front, back = result["card_faces"]
        self.assertEqual((front["face_index"], front["name"], front["oracle_text"], front["flavor_text"]),
                         (0, "Frente", "Nova frente", "Frente antiga"))
        self.assertEqual((back["face_index"], back["name"], back["oracle_text"], back["flavor_text"]),
                         (1, "Verso", "Novo verso", "Verso novo"))

    def test_all_faces_must_be_complete_in_same_impression(self):
        front = {"printed_name": "Frente", "printed_type_line": "Criatura", "printed_text": "Regras"}
        back = {"printed_name": "Verso", "printed_type_line": "Criatura", "oracle_text": ""}
        latest = card(card_faces=[dict(front, printed_name="Frente nova"),
                                  dict(back, printed_name=None, flavor_text="Flavor novo")])
        old = card(released_at="2020-01-01", card_faces=[front, back])
        result = self.merge([old, latest])[0]
        self.assertEqual(result["card_faces"][0]["name"], "Frente")
        self.assertEqual(result["card_faces"][1]["name"], "Verso")
        self.assertEqual(result["card_faces"][1]["oracle_text"], "")
        self.assertEqual(result["card_faces"][1]["flavor_text"], "Flavor novo")
        self.assertEqual(self.merge([latest]), [])
        incomplete_old = card(released_at="2020-01-01", card_faces=[dict(front, printed_text=None,
                              oracle_text="English rules"), back])
        self.assertEqual(self.merge([latest, incomplete_old]), [])

    def test_separate_ids_and_deterministic_ties(self):
        a, b = card(id="a", printed_name="A"), card(id="b", printed_name="B")
        self.assertEqual(self.merge([a, b]), self.merge([b, a]))
        self.assertEqual(len(self.merge([a, card(oracle_id="oracle-2")])), 2)

    def test_invalid_eligible_card_fails(self):
        for changes in ({"oracle_id": None}, {"released_at": None}, {"released_at": "bad"},
                        {"card_faces": None}, {"card_faces": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.merge([card(**changes)])


if __name__ == "__main__":
    unittest.main()
