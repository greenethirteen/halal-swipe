#!/usr/bin/env python3
"""Import the contactable HalalSwipe JSONL profile superset.

Usage:
  python scripts/import_contactable_jsonl.py /path/to/profiles.txt --replace
  python scripts/import_contactable_jsonl.py /path/to/profiles.txt --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import db, init_db  # noqa: E402
from app.parser import EMAIL_RE, PHONE_RE, normalised_hash, remove_contact_from_summary  # noqa: E402

PLACEHOLDER = {"", "not specified", "upon request", "available upon request", "n/a", "na", "none", "null", "nil", "-"}
GENERIC_TITLE_RE = re.compile(
    r"\b(?:profile|details|bismill?aahi|rahmanir|halal\s+business|address|personal\s+details|of\s+the\s+(?:bride|groom))\b",
    re.IGNORECASE,
)
TRAILING_TRUNCATION_RE = re.compile(r"[\s,;:.-]*(?:\.\.\.|…)+$")
NOISY_APPEARANCE_RE = re.compile(r"\b(?:ideally|actions|while|looking|prefer|profession|businessmen|height)\b", re.IGNORECASE)


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\u200e", "").replace("\u200f", "").strip()
    text = re.sub(r"\s+", " ", text)
    text = TRAILING_TRUNCATION_RE.sub("", text).strip(" ;,")
    if text.lower() in PLACEHOLDER:
        return None
    return text or None


def strip_contact_text(value: Any) -> str | None:
    text = clean(value)
    if not text:
        return None
    text = PHONE_RE.sub("", text)
    text = EMAIL_RE.sub("", text)
    text = re.sub(r"\([^)]*(?:contact|phone|whatsapp|call|number)[^)]*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:contact|contect|phone|whatsapp|whats\s*app|call|number|tel)\s*(?:no|only|person)?\s*[:\-]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ;,")
    return clean(text)


def compact_text(value: Any, limit: int) -> str | None:
    text = strip_contact_text(value)
    if not text:
        return None
    pieces = [p.strip(" ;,") for p in re.split(r"\s*[;|]\s*", text) if clean(p)]
    text = "; ".join(pieces) if pieces else text
    return clean(text[:limit])


def parse_age(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        age = int(value)
    except (TypeError, ValueError):
        match = re.search(r"\d{1,2}", str(value))
        age = int(match.group()) if match else 0
    return age if 18 <= age <= 99 else None


def profile_type(row: dict[str, Any]) -> str:
    ptype = (clean(row.get("profile_type")) or "").lower()
    gender = (clean(row.get("gender")) or "").lower()
    if ptype.startswith("bride") or gender.startswith("f"):
        return "Bride"
    if ptype.startswith("groom") or gender.startswith("m"):
        return "Groom"
    return "Unknown"


def display_title(
    row: dict[str, Any],
    ptype: str,
    city: str | None,
    district: str | None,
    age: int | None,
    education: str | None,
    profession: str | None,
) -> str:
    title = clean(row.get("title"))
    if not title or GENERIC_TITLE_RE.search(title):
        location = city or district
        if location:
            return f"{ptype} from {location}"
        if profession:
            return f"{ptype}, {profession[:36].strip()}"
        if education:
            return f"{ptype}, {education[:36].strip()}"
        if age:
            return f"{age}-year-old {ptype}"
        return f"{ptype} profile"
    if len(title) > 70:
        location = city or district
        return f"{ptype} from {location}" if location else f"{ptype} profile"
    return title


def clean_location(value: Any) -> str | None:
    text = clean(value)
    if not text:
        return None
    text = re.sub(r"^(?:living\s+in|place\s+of\s+birth|of\s+birth)\s*:?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*,\s*", ", ", text)
    return clean(text[:80])


def clean_profession(value: Any, ptype: str) -> str | None:
    text = compact_text(value, 120)
    if not text:
        return None
    low = text.lower()
    if low in {"education", "ordinary level", "advanced level", "school", "student"}:
        return None
    if re.search(r"\b(?:follow\s+proper\s+islam|of\s+reference|al\s+background|mahram\s+guidelines)\b", low):
        return None
    if ptype == "Bride" and low in {"businessman", "business man"}:
        return None
    return text


def clean_appearance(value: Any) -> str | None:
    text = strip_contact_text(value)
    if not text:
        return None
    if len(text) > 80 or NOISY_APPEARANCE_RE.search(text):
        return None
    return f"Appearance: {text}"


def phones(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in row.get("phones") or []:
        cleaned = clean(value)
        if cleaned:
            values.append(cleaned)
    primary = clean(row.get("primary_phone"))
    if primary:
        values.insert(0, primary)
    return list(dict.fromkeys(values))


def contact_details(row: dict[str, Any]) -> str | None:
    pieces = []
    person = strip_contact_text(row.get("contact_person"))
    nums = phones(row)
    if person:
        pieces.append(f"Contact: {person}")
    if nums:
        pieces.append(f"Phone: {nums[0]}")
    if len(nums) > 1:
        pieces.append("Other phones: " + ", ".join(nums[1:]))
    return "\n".join(pieces) if pieces else None


def profile_summary(ptype: str, age: int | None, city: str | None, education: str | None, profession: str | None) -> str:
    who = ptype.lower() if ptype else "profile"
    intro = f"A {age}-year-old {who}" if age else f"A {who}"
    if city:
        intro += f" from {city}"
    bits = [intro + "."]
    if education:
        bits.append(f"Highest qualification: {education}.")
    if profession:
        bits.append(f"Work: {profession}.")
    return remove_contact_from_summary(" ".join(bits))


def parse_row(row: dict[str, Any], source_name: str) -> tuple[dict[str, Any] | None, str | None]:
    ref = clean(row.get("profile_id"))
    if not ref:
        return None, "missing profile_id"

    raw_age = row.get("age")
    age = parse_age(raw_age)
    if raw_age not in (None, "") and age is None:
        return None, f"invalid age {raw_age}"

    ptype = profile_type(row)
    city = clean_location(row.get("hometown"))
    district = clean_location(row.get("current_location"))
    if district == city:
        district = None

    education = compact_text(row.get("qualification"), 120)
    profession = clean_profession(row.get("job"), ptype)
    family = compact_text(row.get("family"), 520)
    expectations = compact_text(row.get("expectations"), 520)
    contact = contact_details(row)
    if not contact:
        return None, "missing phone"

    raw_text = json.dumps(row, ensure_ascii=False, sort_keys=True)
    profile = {
        "reference_code": ref,
        "profile_type": ptype,
        "full_name": display_title(row, ptype, city, district, age, education, profession),
        "age": age,
        "height": clean(row.get("height")),
        "city": city,
        "district": district,
        "country": "Sri Lanka",
        "marital_status": clean(row.get("marital_status")),
        "education": education,
        "profession": profession,
        "family_background": family,
        "faith_notes": clean_appearance(row.get("appearance")),
        "expectations": expectations,
        "bio_summary": profile_summary(ptype, age, city, education, profession),
        "contact_details": contact,
        "raw_text": raw_text,
        "source_name": source_name,
        "source_message_at": clean(row.get("source_date")),
        "import_hash": normalised_hash(ref + raw_text),
    }
    return profile, None


def load_profiles(path: Path) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    profiles: list[dict[str, Any]] = []
    counts = {"total": 0, "bad_json": 0, "skipped": 0}
    reasons: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            counts["total"] += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                counts["bad_json"] += 1
                reasons.append(f"line {line_no}: bad JSON ({exc})")
                continue
            profile, reason = parse_row(row, path.name)
            if reason:
                counts["skipped"] += 1
                reasons.append(f"line {line_no} {row.get('profile_id')}: {reason}")
            elif profile:
                profiles.append(profile)
    return profiles, counts, reasons


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--replace", action="store_true", help="Delete existing profile data before importing.")
    ap.add_argument("--dry-run", action="store_true", help="Parse and report without writing to the database.")
    ap.add_argument("--status", choices=["approved", "pending"], default="approved")
    args = ap.parse_args()

    path = Path(args.path)
    profiles, counts, reasons = load_profiles(path)

    if args.dry_run:
        print(f"Total lines: {counts['total']}")
        print(f"Importable profiles: {len(profiles)}")
        print(f"Skipped: {counts['skipped']}  Bad JSON: {counts['bad_json']}")
        for reason in reasons[:25]:
            print(f"- {reason}")
        return 0

    init_db()
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
                    bio_summary, contact_details, raw_text, source_name, source_message_at, import_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    p["source_message_at"],
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
            (
                path.name,
                path.name,
                counts["total"],
                inserted,
                duplicates,
                counts["skipped"] + counts["bad_json"],
                "Imported contactable JSONL superset. Public text stripped of phone/contact fragments.",
            ),
        )

    print(f"Total lines: {counts['total']}")
    print(f"Inserted: {inserted}  Duplicates: {duplicates}  Skipped: {counts['skipped']}  Bad JSON: {counts['bad_json']}")
    for reason in reasons[:25]:
        print(f"- {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
