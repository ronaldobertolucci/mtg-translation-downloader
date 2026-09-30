"""Run with python -m mtg_translation_downloader --help."""

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import zlib
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .etl import consolidate, group_cards, iter_bulk

BULK_URL = "https://api.scryfall.com/bulk-data"
USER_AGENT = "mtg-translation-downloader/1.0"
LOG = logging.getLogger(__name__)


def open_request(url, timeout, *, payload=None, token=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urlopen(Request(url, data=data, headers=headers), timeout=timeout)


def download_bulk(destination, timeout):
    with open_request(BULK_URL, timeout) as response:
        metadata = json.load(response)
    entry = next((item for item in metadata["data"] if item.get("type") == "all_cards"), None)
    if entry is None:
        raise ValueError("Scryfall metadata has no all_cards entry")
    uri = entry.get("jsonl_download_uri") or entry.get("download_uri")
    if not isinstance(uri, str) or not uri.startswith("https://"):
        raise ValueError("Missing or invalid all_cards jsonl_download_uri / download_uri")
    LOG.info("Downloading all_cards bulk to disk")
    partial = destination.with_suffix(".part")
    try:
        with open_request(uri, timeout) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def run(args):
    # Use temporary disk storage for bulk and normalized payloads; no network
    # writes until the entire source has been parsed and consolidated.
    with tempfile.TemporaryDirectory(prefix="mtg-translations-", dir=args.work_dir) as work:
        source = args.input or Path(work) / "all-cards.bulk"
        if not args.input:
            download_bulk(source, args.timeout)
        groups = group_cards(iter_bulk(source))
        normalized = Path(work) / "translations.jsonl"
        count = 0
        with normalized.open("w", encoding="utf-8") as output:
            for payload in consolidate(groups):
                output.write(json.dumps(payload, ensure_ascii=False) + "\n")
                count += 1
        skipped = len(groups) - count
        del groups
        if args.output:
            shutil.copyfile(normalized, args.output)
        LOG.info("Consolidated %d oracle IDs", count)
        LOG.info("Skipped %d oracle IDs without a complete translation", skipped)
        if args.dry_run:
            return count
        sent = 0
        with normalized.open(encoding="utf-8") as payloads:
            for line in payloads:
                payload = json.loads(line)
                try:
                    with open_request(args.manager_url, args.timeout, payload=payload,
                                      token=os.environ.get("MTG_MANAGER_TOKEN")) as response:
                        if not 200 <= response.status < 300:
                            raise ValueError(f"Unexpected HTTP status {response.status}")
                except (HTTPError, URLError, OSError, ValueError) as exc:
                    # Do not log response bodies or tokens, or retry ambiguous POSTs.
                    raise RuntimeError(
                        f"Ingestion failed for {payload['oracle_id']} after {sent}/{count} deliveries"
                    ) from exc
                sent += 1
        LOG.info("Delivered %d translations", sent)
        return sent


def main():
    parser = argparse.ArgumentParser(description="ETL de traduções portuguesas do Scryfall")
    parser.add_argument("--input", type=Path, help="Bulk local JSON/JSONL, opcionalmente gzip; evita o download")
    parser.add_argument("--output", type=Path, help="Salvar payloads consolidados em JSONL")
    parser.add_argument("--dry-run", action="store_true", help="Consolidar sem enviar")
    parser.add_argument("--manager-url", default=os.environ.get(
        "MTG_MANAGER_URL", "http://localhost:8002/translations"),
                        help="URL completa do endpoint de ingestão (ou MTG_MANAGER_URL)")
    parser.add_argument("--work-dir", type=Path, help="Diretório com espaço para o bulk")
    parser.add_argument("--timeout", type=float, default=120, help="Timeout de rede em segundos")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.dry_run and not args.output:
        parser.error("--dry-run requires --output")
    if not args.dry_run and not args.manager_url:
        parser.error("Set --manager-url / MTG_MANAGER_URL or use --dry-run")
    if args.input and args.output and args.input.resolve() == args.output.resolve():
        parser.error("--input and --output must differ")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        run(args)
    except (OSError, EOFError, zlib.error, ValueError, KeyError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
