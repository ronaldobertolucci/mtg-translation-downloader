import gzip
import json
from pathlib import Path
import tempfile
import unittest

from mtg_translation_downloader.etl import iter_bulk


class BulkFormatTests(unittest.TestCase):
    def test_formats_detected_by_content(self):
        cards = [{"name": "Ação"}, {"name": "Raio"}]
        for content in (json.dumps(cards), "\n".join(json.dumps(c) for c in cards)):
            for compressed in (False, True):
                with self.subTest(compressed=compressed, content=content), tempfile.TemporaryDirectory() as work:
                    path = Path(work) / "bulk.data"
                    data = (" \n" + content + "\n").encode()
                    path.write_bytes(gzip.compress(data) if compressed else data)
                    self.assertEqual(list(iter_bulk(path)), cards)

    def test_invalid_jsonl(self):
        for content in (b'', b' \n', b'{}\n{"name":', b'{}\nnull\n', b'{} {}\n'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as work:
                path = Path(work) / "bulk.jsonl"
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    list(iter_bulk(path))

    def test_line_size_limit(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "bulk.jsonl"
            path.write_text(json.dumps({"name": "x" * 100}))
            with self.assertRaisesRegex(ValueError, "Oversized"):
                list(iter_bulk(path, max_object_size=32))

    def test_truncated_gzip_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "bulk.gz"
            path.write_bytes(gzip.compress(b'{"name":"Raio"}\n')[:-5])
            with self.assertRaises((EOFError, OSError)):
                list(iter_bulk(path))
