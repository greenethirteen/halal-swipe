#!/usr/bin/env python3
"""Import the cleaned app-ready profile text export.

Usage:
  python scripts/import_app_ready_text.py /path/to/profiles.txt --replace
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import db, init_db  # noqa: E402
from app.parser import normalised_hash, remove_contact_from_summary  # noqa: E402

SEPARATOR_RE = re.compile(r"^-{20,}\s*$", re.MULTILINE)
PLACEHOLDER = {"", "not specified", "upon request", "available upon request", "n/a", "na", "none", "null"}
QUAL_RE = re.compile(
    r"\b(?:o/l|a/l|acca|hnd|hnda|bsc|ba|bcom|b\.?com|llb|ll\.?b|mbbs|md|msc|ma|mba|phd|"
    r"diploma|dip\.?|degree|graduate|undergraduate|bachelor|master|alim|aalim|hafiz|"
    r"foundation|certificate|nvq|city\s*&?\s*guilds)\b",
    re.IGNORECASE,
)
SCHOOL_RE = re.compile(r"\b(?:school|college|central\s+college|maha\s+vidyalaya|vidyalaya)\b", re.IGNORECASE)
WORK_NOISE_RE = re.compile(
    r"\b(?:completed\s+o/l|completed\s+a/l|^a/l\s+(?:background|qualification|qualifications)$|"
    r"other\s+qualification|father\b|mother\b|sibling|siblings|family\b|details?:|expecting\s+(?:bride|groom))",
    re.IGNORECASE,
)
FAMILY_NOISE_RE = re.compile(r"\bexpecting\s+(?:bride|groom)\s+details?\b.*", re.IGNORECASE)


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if text.lower() in PLACEHOLDER:
        return None
    return text or None


def split_profiles(text: str) -> list[str]:
    blocks = []
    for block in SEPARATOR_RE.split(text):
        block = block.strip()
        if re.search(r"^PROFILE\s+NP-\d+", block, flags=re.MULTILINE):
            blocks.append(block)
    return blocks


def field(block: str, label: str) -> str | None:
    match = re.search(rf"^{re.escape(label)}:\s*(.+)$", block, flags=re.MULTILINE)
    return clean(match.group(1)) if match else None


def reference_code(block: str) -> str | None:
    match = re.search(r"^PROFILE\s+([A-Z]+-\d+)", block, flags=re.MULTILINE)
    return match.group(1) if match else None


def parse_candidate(value: str | None) -> tuple[str, str | None]:
    if not value:
        return "Unknown", None
    parts = [part.strip() for part in value.split("|")]
    candidate = "Unknown"
    looking_for = None
    first = clean(parts[0]) if parts else None
    if first in {"Bride", "Groom", "Unknown"}:
        candidate = first
        parts = parts[1:]
    for part in parts:
        key, _, raw = part.partition(":")
        val = clean(raw)
        if key.strip().lower() == "candidate" and val in {"Bride", "Groom", "Unknown"}:
            candidate = val
        elif key.strip().lower() == "looking for":
            looking_for = val
    return candidate, looking_for


def parse_basic(value: str | None) -> dict[str, object]:
    parsed: dict[str, object] = {"age": None, "height": None, "marital_status": None, "dress_code": None}
    if not value:
        return parsed
    for part in value.split("|"):
        key, _, raw = part.partition(":")
        key = key.strip().lower()
        val = clean(raw)
        if not val:
            continue
        if key == "age":
            match = re.search(r"\d{1,2}", val)
            if match:
                age = int(match.group())
                parsed["age"] = age if 18 <= age <= 99 else None
        elif key == "height":
            parsed["height"] = val
        elif key == "marital status":
            parsed["marital_status"] = val
        elif key == "dress code":
            parsed["dress_code"] = val
    return parsed


def contact(value: str | None) -> str | None:
    if not value:
        return None
    pieces = []
    for part in value.split("|"):
        cleaned = clean(part)
        if cleaned:
            pieces.append(cleaned)
    return "\n".join(pieces) if pieces else None


def clean_piece(value: str | None) -> str | None:
    text = clean(value)
    if not text:
        return None
    text = re.sub(r"[🌻✳️*]+", " ", text)
    text = re.sub(r"(?:^|[;|]\s*)\d+\.\s*", "; ", text)
    text = re.sub(r"\s+", " ", text).strip(" ;,")
    return clean(text)


def split_pieces(value: str | None) -> list[str]:
    text = clean_piece(value)
    if not text:
        return []
    return [p.strip(" ;,") for p in re.split(r"\s*[;|]\s*", text) if clean(p)]


def compact_education(value: str | None, block: str) -> str | None:
    text = clean_piece(value)
    if not text:
        return None
    first = split_pieces(text)[0] if split_pieces(text) else text
    if SCHOOL_RE.search(first) and not QUAL_RE.search(first):
        return None
    if QUAL_RE.search(first):
        return first[:90]
    match = QUAL_RE.search(block)
    return match.group(0).strip() if match else first[:90]


def compact_work(value: str | None) -> str | None:
    pieces = []
    for piece in split_pieces(value):
        if WORK_NOISE_RE.search(piece):
            continue
        piece = re.sub(r"\s+from\s+.+?(?:teachers?\s+college|school|college)\b.*", "", piece, flags=re.IGNORECASE).strip()
        if clean(piece):
            pieces.append(piece)
    if not pieces:
        return None
    work = pieces[0]
    work = re.sub(r"^currently\s+(?:working\s+)?(?:as\s+)?", "", work, flags=re.IGNORECASE).strip()
    return clean(work[:110])


def compact_family(value: str | None) -> str | None:
    text = clean_piece(value)
    if not text:
        return None
    text = FAMILY_NOISE_RE.sub("", text)
    pieces = []
    for piece in split_pieces(text):
        if re.search(r"\bexpecting\s+(?:bride|groom)\b", piece, flags=re.IGNORECASE):
            continue
        pieces.append(piece)
    return "; ".join(pieces)[:500] if pieces else None


def compact_expectations(value: str | None) -> str | None:
    pieces = split_pieces(value)
    useful = [p for p in pieces if not re.search(r"\bcontacts?\b|\bwhatsapp\b|\bphone\b", p, flags=re.IGNORECASE)]
    return "; ".join(useful[:4])[:420] if useful else None


def profile_summary(profile_type: str, age: object, city: str | None, education: str | None, profession: str | None) -> str:
    who = (profile_type or "profile").lower()
    bits = []
    intro = f"A {age}-year-old {who}" if age else f"A {who}"
    if city:
        intro += f" from {city}"
    bits.append(intro + ".")
    if education:
        bits.append(f"Highest qualification: {education}.")
    if profession:
        bits.append(f"Work: {profession}.")
    return " ".join(bits)


def parse_profile(block: str, source_name: str) -> dict | None:
    ref = reference_code(block)
    title = field(block, "Title")
    candidate, looking_for = parse_candidate(field(block, "Candidate"))
    basic = parse_basic(field(block, "Basic details"))
    expectations = field(block, "Looking for")
    raw_contact = field(block, "Contact details")
    gender_confidence = field(block, "Gender confidence")

    faith_notes = basic.get("dress_code")
    if gender_confidence:
        faith_notes = "\n".join([p for p in [faith_notes, f"Gender confidence: {gender_confidence}"] if p])

    education = compact_education(field(block, "Highest qualification"), block)
    profession = compact_work(field(block, "Work"))
    profile = {
        "reference_code": ref,
        "profile_type": candidate,
        "full_name": title,
        "age": basic.get("age"),
        "height": basic.get("height"),
        "city": field(block, "Location"),
        "district": None,
        "country": "Sri Lanka",
        "marital_status": basic.get("marital_status"),
        "education": education,
        "profession": profession,
        "family_background": compact_family(field(block, "Family")),
        "faith_notes": faith_notes,
        "expectations": compact_expectations(expectations) or (f"Looking for: {looking_for}" if looking_for else None),
        "bio_summary": remove_contact_from_summary(profile_summary(candidate, basic.get("age"), field(block, "Location"), education, profession)),
        "contact_details": contact(raw_contact),
        "raw_text": block,
        "source_name": source_name,
    }
    if not profile["reference_code"]:
        profile["reference_code"] = f"NP-{normalised_hash(block)[:8].upper()}"
    if not any(profile.get(k) for k in ["age", "city", "education", "profession", "family_background"]):
        return None
    profile["import_hash"] = normalised_hash(block + profile["reference_code"])
    return profile


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--replace", action="store_true", help="Delete existing profile data before importing.")
    ap.add_argument("--status", choices=["approved", "pending"], default="approved")
    args = ap.parse_args()

    init_db()
    path = Path(args.path)
    text = path.read_text(encoding="utf-8")
    profiles = [p for p in (parse_profile(block, path.name) for block in split_profiles(text)) if p]

    inserted = duplicates = 0
    with db() as conn:
        if args.replace:
            conn.execute("DELETE FROM contact_views")
            conn.execute("DELETE FROM profiles")
            conn.execute("DELETE FROM import_batches")
        for p in profiles:
            if conn.execute("SELECT 1 FROM profiles WHERE import_hash = ?", (p["import_hash"],)).fetchone():
                duplicates += 1
                continue
            ref = p["reference_code"]
            if conn.execute("SELECT 1 FROM profiles WHERE reference_code = ?", (ref,)).fetchone():
                ref = f"{ref}-{p['import_hash'][:4].upper()}"
            conn.execute(
                """
                INSERT INTO profiles (
                    reference_code, status, profile_type, full_name, age, height, city, district, country,
                    marital_status, education, profession, family_background, faith_notes, expectations,
                    bio_summary, contact_details, raw_text, source_name, import_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref,
                    args.status,
                    p["profile_type"],
                    p["full_name"],
                    p["age"],
                    p["height"],
                    p["city"],
                    p["district"],
                    p["country"],
                    p["marital_status"],
                    p["education"],
                    p["profession"],
                    p["family_background"],
                    p["faith_notes"],
                    p["expectations"],
                    p["bio_summary"],
                    p["contact_details"],
                    p["raw_text"],
                    p["source_name"],
                    p["import_hash"],
                ),
            )
            inserted += 1
        conn.execute(
            """
            INSERT INTO import_batches
            (filename, source_name, total_candidates, inserted, duplicates, skipped, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (path.name, path.name, len(profiles), inserted, duplicates, 0, "Imported app-ready cleaned text export."),
        )

    print(f"Parsed: {len(profiles)}  Inserted: {inserted}  Duplicates: {duplicates}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
