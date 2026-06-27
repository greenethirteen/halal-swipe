"""Import marriage profiles from a JSON-Lines (.txt/.jsonl) export.

Each line is one JSON object with keys like:
  "Profile ID", "Candidate Gender", "Looking For", "Name", "Age", "Height",
  "Location / Hometown", "Education", "Profession", "Marital Status",
  "Father", "Mother", "Siblings", "Expectations", "Dress Code",
  "Phone Numbers", "WhatsApp Numbers", "Contact Person", "Raw Profile", ...

Usage:
    python scripts/import_jsonl.py "/path/to/profiles.txt" [--status approved|pending]

Run from the project root (so app.db and settings resolve correctly).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import db, init_db  # noqa: E402
from app.parser import normalised_hash, remove_contact_from_summary  # noqa: E402

PLACEHOLDER = {"", "[available upon request]", "available upon request", "n/a", "na", "none", "null"}


def clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip("'").strip()
    if text.lower() in PLACEHOLDER:
        return None
    return text or None


def clean_name(value) -> str | None:
    text = clean(value)
    if not text:
        return None
    low = text.lower()
    # drop junk that leaked into the Name field in the source export
    if "looking for" in low or low in {"groom", "bride", "male", "female"}:
        return None
    if len(text) > 60:  # likely a whole sentence, not a name
        return None
    return text


def to_age(value) -> int | None:
    text = clean(value)
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    age = int(digits[:2]) if len(digits) >= 2 else int(digits)
    return age if 18 <= age <= 99 else None


def profile_type_from(row: dict) -> str:
    gender = (clean(row.get("Candidate Gender")) or "").lower()
    if gender.startswith("f"):
        return "Bride"
    if gender.startswith("m"):
        return "Groom"
    looking = (clean(row.get("Looking For")) or "").lower()
    if "groom" in looking:  # looking for a groom => candidate is the bride
        return "Bride"
    if "bride" in looking:
        return "Groom"
    return "Unknown"


def join_nonempty(parts: list[str | None], sep: str = "\n") -> str | None:
    items = [p for p in parts if p]
    return sep.join(items) if items else None


def build_family(row: dict) -> str | None:
    return join_nonempty(
        [
            f"Father: {clean(row.get('Father'))}" if clean(row.get("Father")) else None,
            f"Mother: {clean(row.get('Mother'))}" if clean(row.get("Mother")) else None,
            f"Family status: {clean(row.get('Family Status'))}" if clean(row.get("Family Status")) else None,
            f"Siblings: {clean(row.get('Siblings'))}" if clean(row.get("Siblings")) else None,
        ]
    )


def build_contact(row: dict) -> str | None:
    return join_nonempty(
        [
            f"Phones: {clean(row.get('Phone Numbers'))}" if clean(row.get("Phone Numbers")) else None,
            f"WhatsApp: {clean(row.get('WhatsApp Numbers'))}" if clean(row.get("WhatsApp Numbers")) else None,
            f"Contact: {clean(row.get('Contact Person'))}" if clean(row.get("Contact Person")) else None,
        ]
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--status", choices=["approved", "pending"], default="approved")
    args = ap.parse_args()

    init_db()
    path = Path(args.path)
    inserted = duplicates = skipped = bad = 0

    with db() as conn:
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue

                raw = clean(row.get("Raw Profile")) or ""
                contact = build_contact(row)
                # dedupe key: normalised raw text + contact (phone-insensitive raw, contact appended)
                import_hash = normalised_hash(raw + (contact or "") + str(row.get("Profile ID") or line_no))

                if conn.execute("SELECT 1 FROM profiles WHERE import_hash = ?", (import_hash,)).fetchone():
                    duplicates += 1
                    continue

                ref = clean(row.get("Profile ID")) or f"NP-{import_hash[:8].upper()}"
                # ensure unique reference_code
                if conn.execute("SELECT 1 FROM profiles WHERE reference_code = ?", (ref,)).fetchone():
                    ref = f"{ref}-{import_hash[:4].upper()}"

                bio = remove_contact_from_summary(raw)[:1200] if raw else None
                if not (bio or contact):
                    skipped += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO profiles (
                        reference_code, status, profile_type, full_name, age, height, city, district, country,
                        marital_status, education, profession, family_background, faith_notes, expectations,
                        bio_summary, contact_details, raw_text, source_name, import_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Sri Lanka', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ref,
                        args.status,
                        profile_type_from(row),
                        clean_name(row.get("Name")),
                        to_age(row.get("Age")),
                        clean(row.get("Height")),
                        clean(row.get("Location / Hometown")),
                        clean(row.get("Current Location / Country")),
                        clean(row.get("Marital Status")),
                        clean(row.get("Education")),
                        clean(row.get("Profession")),
                        build_family(row),
                        clean(row.get("Dress Code")),
                        clean(row.get("Expectations")),
                        bio,
                        contact,
                        raw or None,
                        clean(row.get("Source")),
                        import_hash,
                    ),
                )
                inserted += 1

    print(f"Inserted: {inserted}  Duplicates: {duplicates}  Skipped(empty): {skipped}  Bad lines: {bad}")


if __name__ == "__main__":
    main()
