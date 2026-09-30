import argparse
import gzip
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from mtg_translation_downloader.__main__ import BULK_URL, download_bulk, open_request, run


class Response(io.BytesIO):
    status = 201


class JobTests(unittest.TestCase):
    def test_current_jsonl_gzip_download_and_dry_run(self):
        metadata = {"data": [{
            "type": "all_cards", "jsonl_download_uri": "https://example.test/all.jsonl.gz",
            "download_uri": "https://example.test/legacy.json",
        }]}
        card = {"oracle_id": "id-1", "lang": "pt", "set_type": "expansion",
                "released_at": "2024-01-01", "printed_name": "Ação",
                "printed_type_line": "Feitiço", "printed_text": "Compre um card."}
        compressed = gzip.compress((json.dumps(card, ensure_ascii=False) + "\n").encode())
        with tempfile.TemporaryDirectory() as work, patch(
            "mtg_translation_downloader.__main__.open_request",
            side_effect=[Response(json.dumps(metadata).encode()), Response(compressed)],
        ) as request:
            args = self.args(work, True)
            args.input = None
            self.assertEqual(run(args), 1)
            self.assertEqual(json.loads(args.output.read_text())["name"], "Ação")
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args.args[0], "https://example.test/all.jsonl.gz")

    def test_download_discovers_all_cards(self):
        metadata = {"data": [
            {"type": "oracle_cards", "download_uri": "https://example.test/wrong"},
            {"type": "all_cards", "download_uri": "https://example.test/all"},
        ]}
        with tempfile.TemporaryDirectory() as work, patch(
            "mtg_translation_downloader.__main__.open_request",
            side_effect=[Response(json.dumps(metadata).encode()), Response(b'[]')],
        ) as request:
            destination = Path(work) / "bulk.json"
            download_bulk(destination, 10)
            self.assertEqual(destination.read_bytes(), b'[]')
            self.assertEqual(request.call_args_list[0].args, (BULK_URL, 10))
            self.assertEqual(request.call_args_list[1].args, ("https://example.test/all", 10))
            self.assertFalse(destination.with_suffix('.part').exists())

    def test_download_failure_cleans_partial(self):
        class Broken(Response):
            def read(self, size=-1):
                raise OSError("Disconnected")
        metadata = b'{"data":[{"type":"all_cards","download_uri":"https://example.test/all"}]}'
        with tempfile.TemporaryDirectory() as work, patch(
            "mtg_translation_downloader.__main__.open_request",
            side_effect=[Response(metadata), Broken()],
        ):
            destination = Path(work) / "bulk.json"
            with self.assertRaises(OSError):
                download_bulk(destination, 10)
            self.assertEqual(list(Path(work).iterdir()), [])

    def test_request_headers_and_utf8(self):
        with patch("mtg_translation_downloader.__main__.urlopen") as request:
            open_request("http://localhost:8002/translations", 10, payload={"name": "Ação"}, token="secret")
            req = request.call_args.args[0]
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(req.get_header("Authorization"), "Bearer secret")
            self.assertEqual(req.get_header("Content-type"), "application/json")
            self.assertEqual(json.loads(req.data), {"name": "Ação"})

    def args(self, work, dry_run=False):
        return argparse.Namespace(input=Path(work) / "bulk.json", output=Path(work) / "out.jsonl",
                                  dry_run=dry_run, manager_url="http://localhost:8002/translations",
                                  timeout=10, work_dir=Path(work))

    def write_bulk(self, path):
        path.write_text(json.dumps([{
            "oracle_id": "id-1", "lang": "pt", "set_type": "core",
            "released_at": "2024-01-01", "printed_name": "Raio",
            "printed_type_line": "Mágica Instantânea", "printed_text": "Raio causa 3 pontos de dano.",
        }]), encoding="utf-8")

    def test_dry_run_never_sends(self):
        with tempfile.TemporaryDirectory() as work, patch(
            "mtg_translation_downloader.__main__.open_request"
        ) as request:
            args = self.args(work, True)
            self.write_bulk(args.input)
            self.assertEqual(run(args), 1)
            request.assert_not_called()
            self.assertEqual(json.loads(args.output.read_text())["name"], "Raio")

    def test_ingestion_sends_normalized_payload(self):
        with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, {"MTG_MANAGER_TOKEN": "token"}), patch(
            "mtg_translation_downloader.__main__.open_request", return_value=Response()
        ) as request:
            args = self.args(work)
            self.write_bulk(args.input)
            self.assertEqual(run(args), 1)
            self.assertEqual(request.call_args.kwargs["payload"], json.loads(args.output.read_text()))
            self.assertEqual(request.call_args.kwargs["token"], "token")

    def test_malformed_bulk_never_sends(self):
        with tempfile.TemporaryDirectory() as work, patch(
            "mtg_translation_downloader.__main__.open_request"
        ) as request:
            args = self.args(work)
            self.write_bulk(args.input)
            with args.input.open("a") as source:
                source.write("junk")
            with self.assertRaises(ValueError):
                run(args)
            request.assert_not_called()

    def test_incomplete_translation_never_sends(self):
        with tempfile.TemporaryDirectory() as work, patch(
            "mtg_translation_downloader.__main__.open_request"
        ) as request:
            args = self.args(work)
            self.write_bulk(args.input)
            cards = json.loads(args.input.read_text())
            cards[0]["printed_name"] = None
            args.input.write_text(json.dumps(cards))
            with self.assertLogs("mtg_translation_downloader.__main__", level="INFO") as logs:
                self.assertEqual(run(args), 0)
            self.assertTrue(any("Skipped 1 oracle IDs" in line for line in logs.output))
            request.assert_not_called()
            self.assertEqual(args.output.read_text(), "")

    def test_failure_reports_progress_without_retry(self):
        with tempfile.TemporaryDirectory() as work, patch(
            "mtg_translation_downloader.__main__.open_request",
            side_effect=HTTPError("http://localhost", 409, "Conflict", {}, None),
        ) as request:
            args = self.args(work)
            self.write_bulk(args.input)
            with self.assertRaisesRegex(RuntimeError, "id-1 after 0/1"):
                run(args)
            self.assertEqual(request.call_count, 1)
            self.assertTrue(args.output.exists())
