"""Streaming extraction, strict filtering, and translation consolidation."""

import gzip
import json
from datetime import date
from typing import TextIO

ALLOWED_SETS = frozenset({
    "core", "expansion", "masters", "commander", "draft_innovation", "starter"
})


def iter_bulk(path, max_object_size=8 * 1024 * 1024):
    """Read JSON arrays or JSONL, optionally gzip compressed, without expansion on disk."""
    with path.open("rb") as raw:
        compressed = raw.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else open
    with opener(path, "rt", encoding="utf-8") as source:
        first = source.read(1)
        while first and first in " \t\r\n":
            first = source.read(1)
        source.seek(0)
        if first == "[":
            yield from iter_cards(source, max_object_size=max_object_size)
            return
        if not first:
            raise ValueError("Empty bulk data")
        line_number = 0
        while True:
            line = source.readline(max_object_size + 1)
            if not line:
                break
            line_number += 1
            if len(line) > max_object_size:
                raise ValueError(f"Oversized JSONL card at line {line_number}")
            if not line.strip():
                continue
            try:
                card = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL card at line {line_number}") from exc
            if not isinstance(card, dict):
                raise ValueError(f"JSONL entry at line {line_number} must be an object")
            yield card


def iter_cards(source: TextIO, chunk_size=65536, max_object_size=8 * 1024 * 1024):
    """Parse a JSON array incrementally, bounding the buffer to one card.

    Reject malformed/truncated input instead of silently ingesting a partial bulk.
    The size limit is in Unicode characters and is independent of file size.
    """
    if chunk_size <= 0 or max_object_size <= 0:
        raise ValueError("Stream limits must be positive")
    decoder = json.JSONDecoder()
    buffer = ""
    eof = False

    def read_more():
        nonlocal buffer, eof
        chunk = source.read(chunk_size)
        eof = not chunk
        buffer += chunk

    def whitespace():
        nonlocal buffer
        buffer = buffer.lstrip(" \t\r\n")
        while not buffer and not eof:
            read_more()
            buffer = buffer.lstrip(" \t\r\n")

    whitespace()
    if not buffer.startswith("["):
        raise ValueError("Bulk data must be a JSON array")
    buffer = buffer[1:]
    whitespace()
    if not buffer.startswith("]"):
        while True:
            while True:
                try:
                    card, end = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError as exc:
                    if eof or len(buffer) > max_object_size:
                        raise ValueError("Malformed, truncated, or oversized bulk card") from exc
                    read_more()
            if end > max_object_size or not isinstance(card, dict):
                raise ValueError("Bulk entries must be bounded JSON objects")
            buffer = buffer[end:]
            yield card
            whitespace()
            if buffer.startswith("]"):
                break
            if not buffer.startswith(","):
                raise ValueError("Expected comma or closing bracket in bulk data")
            buffer = buffer[1:]
            whitespace()
    buffer = buffer[1:]
    whitespace()
    if buffer:
        raise ValueError("Unexpected content after bulk array")


def eligible(card):
    return (
        card.get("lang") == "pt"
        and card.get("promo") is not True
        and card.get("textless") is not True
        and card.get("set_type") in ALLOWED_SETS
    )


TEXT_FIELDS = (
    "name", "type_line", "oracle_text", "printed_name", "printed_type_line",
    "printed_text", "printed_flavor_text", "flavor_text",
)


def group_cards(cards):
    """Only retain eligible impressions and fields required by the merge."""
    groups = {}
    for card in cards:
        if not eligible(card):
            continue
        oracle_id = card.get("oracle_id")
        if not isinstance(oracle_id, str) or not oracle_id:
            raise ValueError("Eligible card has no oracle_id")
        released_at = card.get("released_at")
        if not isinstance(released_at, str):
            raise ValueError(f"Missing released_at for {oracle_id}")
        date.fromisoformat(released_at)
        impression = {key: card.get(key) for key in TEXT_FIELDS}
        impression.update(released_at=released_at, id=card.get("id", ""))
        if "card_faces" in card:
            faces = card["card_faces"]
            if not isinstance(faces, list) or not faces or not all(isinstance(f, dict) for f in faces):
                raise ValueError(f"Invalid card_faces for {oracle_id}")
            impression["card_faces"] = [
                {key: face.get(key) for key in TEXT_FIELDS} for face in faces
            ]
        groups.setdefault(oracle_id, []).append(impression)
    return groups


def has_text(value):
    return isinstance(value, str) and bool(value.strip())


def empty_text(value):
    return value is None or (isinstance(value, str) and not value.strip())


def complete_translation(face):
    if not all(has_text(face.get(key)) for key in ("printed_name", "printed_type_line")):
        return False
    printed = face.get("printed_text")
    return has_text(printed) or (empty_text(printed) and empty_text(face.get("oracle_text")))


def complete_impression(card):
    if "card_faces" in card:
        return bool(card["card_faces"]) and all(complete_translation(face) for face in card["card_faces"])
    return complete_translation(card)


def merge_texts(latest, history):
    # Called only for a complete translation. Never copy original-language text.
    result = {
        "name": latest["printed_name"],
        "type_line": latest["printed_type_line"],
        "oracle_text": latest["printed_text"] if has_text(latest.get("printed_text")) else "",
    }
    result["flavor_text"] = next((
        value for face in history
        for value in (face.get("printed_flavor_text"), face.get("flavor_text"))
        if isinstance(value, str) and value.strip()
    ), None)
    return result


def consolidate(groups):
    for oracle_id in sorted(groups):
        # Stable tie-break for releases on the same day, independent of bulk order.
        history = sorted(groups[oracle_id], key=lambda c: (c["released_at"], c["id"]), reverse=True)
        latest = next((card for card in history if complete_impression(card)), None)
        if latest is None:
            continue
        payload = {"oracle_id": oracle_id, "lang": "pt"}
        if "card_faces" in latest:
            payload["card_faces"] = []
            for index, face in enumerate(latest["card_faces"]):
                face_history = [card["card_faces"][index] for card in history
                                if len(card.get("card_faces", [])) > index]
                payload["card_faces"].append({
                    "face_index": index, **merge_texts(face, face_history)
                })
        else:
            payload.update(merge_texts(latest, history))
        yield payload
