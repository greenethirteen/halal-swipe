from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .database import db

CONTROL_CHARS = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\ufeff"
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().\-]{7,}\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

MESSAGE_RE = re.compile(
    r"^[\u200e\ufeff]?\[(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s*(?P<time>[^\]]+)\]\s*(?P<sender>.*?):\s*(?P<body>.*)$"
)

FIELD_PATTERNS = {
    "full_name": [r"(?:^|\n)\s*(?:name|full\s*name)\s*[:\-]\s*(.+)"],
    "age": [r"(?:^|\n)\s*age\s*[:\-]\s*(\d{1,2})\b", r"\b(\d{2})\s*(?:years|yrs|y/o)\b"],
    "height": [r"(?:^|\n)\s*height\s*[:\-]\s*([^\n]+)"],
    "city": [r"(?:^|\n)\s*(?:home\s*town|hometown|location|city|area|residence)\s*[:\-]\s*([^\n]+)"],
    "district": [r"(?:^|\n)\s*district\s*[:\-]\s*([^\n]+)"],
    "country": [r"(?:^|\n)\s*country\s*[:\-]\s*([^\n]+)"],
    "marital_status": [r"(?:^|\n)\s*marital\s*status\s*[:\-]\s*([^\n]+)", r"(?:^|\n)\s*status\s*[:\-]\s*([^\n]+)"],
    "education": [r"(?:^|\n)\s*(?:education|educational\s*qualification|qualification|studies)\s*[:\-]\s*([^\n]+)"],
    "profession": [r"(?:^|\n)\s*(?:profession|occupation|job|employed\s*as|currently\s*employed\s*as)\s*[:\-]?\s*([^\n]+)"],
    "family_background": [r"(?:^|\n)\s*(?:family\s*background|family\s*status|father|mother|siblings)\s*[:\-]\s*([^\n]+)"],
    "faith_notes": [r"(?:^|\n)\s*(?:religion|religious|dress\s*code|madhab|qur.?an|hafiz|sharia)\s*[:\-]?\s*([^\n]+)"],
    "expectations": [r"(?:^|\n)\s*(?:expectation|expectations|looking\s*for|seeking|preferred)\s*[:\-]?\s*([^\n]+)"],
}

BAD_VALUES = {
    "available upon request",
    "upon request",
    "private",
    "n/a",
    "na",
    "nil",
    "-",
}

@dataclass
class WhatsAppMessage:
    date: str
    time: str
    sender: str
    body: str


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    for ch in CONTROL_CHARS:
        value = value.replace(ch, "")
    value = value.replace(" ", " ").replace("∙", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "utf-8", "latin1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_whatsapp_messages(text: str) -> list[WhatsAppMessage]:
    messages: list[WhatsAppMessage] = []
    current: WhatsAppMessage | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        match = MESSAGE_RE.match(line)
        if match:
            if current:
                current.body = clean_text(current.body)
                messages.append(current)
            current = WhatsAppMessage(
                date=match.group("date"),
                time=match.group("time"),
                sender=clean_text(match.group("sender")),
                body=match.group("body"),
            )
        elif current:
            current.body += "\n" + line

    if current:
        current.body = clean_text(current.body)
        messages.append(current)
    return messages


def looks_like_profile(text: str) -> bool:
    t = clean_text(text).lower()
    if len(t) < 120:
        return False
    system_noise = [
        "message was deleted",
        "joined using a group link",
        "added ",
        "removed ",
        "messages and calls are end-to-end encrypted",
        "changed their phone number",
        "this reply was deleted",
    ]
    if any(phrase in t for phrase in system_noise):
        return False
    score = 0
    for phrase in [
        "looking for a groom",
        "looking for groom",
        "looking for a bride",
        "looking for bride",
        "bride details",
        "groom details",
        "biodata",
        "bio data",
        "marital status",
        "home town",
        "height",
        "education",
        "profession",
        "siblings",
    ]:
        if phrase in t:
            score += 1
    return score >= 2


def _first_match(text: str, patterns: Iterable[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            value = clean_text(match.group(1))
            value = value.strip(" :;-•*_|~")
            value = re.sub(r"\s{2,}", " ", value)
            if value and value.lower() not in BAD_VALUES:
                return value[:350]
    return None


def _extract_age(text: str) -> int | None:
    raw = _first_match(text, FIELD_PATTERNS["age"])
    if not raw:
        return None
    match = re.search(r"\d{1,2}", raw)
    if not match:
        return None
    age = int(match.group())
    return age if 0 < age < 100 else None


def infer_profile_type(text: str) -> str:
    t = text.lower()
    if "looking for a groom" in t or "looking for groom" in t or "bride details" in t:
        return "Bride"
    if "looking for a bride" in t or "looking for bride" in t or "groom details" in t:
        return "Groom"
    if re.search(r"(?:^|\n)\s*gender\s*[:\-]\s*female", t):
        return "Bride"
    if re.search(r"(?:^|\n)\s*gender\s*[:\-]\s*male", t):
        return "Groom"
    return "Unknown"


def extract_contact(text: str) -> str | None:
    phones = [clean_text(p) for p in PHONE_RE.findall(text)]
    emails = EMAIL_RE.findall(text)
    contact_lines = []
    for line in text.splitlines():
        if re.search(r"contact|whatsapp|admin|phone|call|inbox", line, re.IGNORECASE):
            contact_lines.append(clean_text(line)[:250])
    pieces = []
    if phones:
        pieces.append("Phones: " + ", ".join(dict.fromkeys(phones[:4])))
    if emails:
        pieces.append("Email: " + ", ".join(dict.fromkeys(emails[:2])))
    if contact_lines:
        pieces.append("Notes: " + " | ".join(dict.fromkeys(contact_lines[:3])))
    return "\n".join(pieces) if pieces else None


def first_phone(contact_details: str | None) -> str | None:
    """Pull the first phone-like number out of stored contact details."""
    if not contact_details:
        return None
    match = PHONE_RE.search(contact_details)
    return clean_text(match.group(0)) if match else None


def to_wa_number(phone: str | None) -> str | None:
    """Normalise a phone number to international digits for wa.me / tel: (Sri Lanka default)."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None
    if digits.startswith("0094"):
        digits = digits[2:]
    elif digits.startswith("94"):
        pass
    elif digits.startswith("0"):
        digits = "94" + digits[1:]
    elif len(digits) == 9:  # local mobile without leading 0
        digits = "94" + digits
    return digits


def remove_contact_from_summary(text: str) -> str:
    text = PHONE_RE.sub("[contact hidden]", text)
    text = EMAIL_RE.sub("[email hidden]", text)
    return text


def normalised_hash(text: str) -> str:
    base = clean_text(text).lower()
    base = PHONE_RE.sub("", base)
    base = EMAIL_RE.sub("", base)
    base = re.sub(r"[^a-z0-9]+", "", base)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def extract_profile(text: str, source_name: str, sender: str = "", date: str = "") -> dict | None:
    text = clean_text(text)
    age = _extract_age(text)
    if age is not None and age < 18:
        return None

    profile = {
        "profile_type": infer_profile_type(text),
        "full_name": _first_match(text, FIELD_PATTERNS["full_name"]),
        "age": age,
        "height": _first_match(text, FIELD_PATTERNS["height"]),
        "city": _first_match(text, FIELD_PATTERNS["city"]),
        "district": _first_match(text, FIELD_PATTERNS["district"]),
        "country": _first_match(text, FIELD_PATTERNS["country"]) or "Sri Lanka",
        "marital_status": _first_match(text, FIELD_PATTERNS["marital_status"]),
        "education": _first_match(text, FIELD_PATTERNS["education"]),
        "profession": _first_match(text, FIELD_PATTERNS["profession"]),
        "family_background": _first_match(text, FIELD_PATTERNS["family_background"]),
        "faith_notes": _first_match(text, FIELD_PATTERNS["faith_notes"]),
        "expectations": _first_match(text, FIELD_PATTERNS["expectations"]),
        "bio_summary": remove_contact_from_summary(text)[:900],
        "contact_details": extract_contact(text),
        "raw_text": text,
        "source_name": source_name,
        "source_sender": clean_text(sender)[:150],
        "source_message_at": f"{date}".strip(),
        "import_hash": normalised_hash(text),
    }
    if not any(profile.get(k) for k in ["age", "city", "education", "profession", "marital_status"]):
        return None
    return profile


def profiles_from_zip(zip_path: str | Path) -> tuple[list[dict], int, str]:
    zip_path = Path(zip_path)
    profiles: list[dict] = []
    total_candidates = 0
    notes = []
    with zipfile.ZipFile(zip_path) as zf:
        txt_names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if not txt_names:
            return [], 0, "No .txt chat export found in zip."
        for txt_name in txt_names:
            text = decode_bytes(zf.read(txt_name))
            messages = parse_whatsapp_messages(text)
            source_name = zip_path.name
            notes.append(f"{txt_name}: {len(messages)} messages")
            seen_hashes = set()
            for msg in messages:
                if not looks_like_profile(msg.body):
                    continue
                total_candidates += 1
                profile = extract_profile(
                    msg.body,
                    source_name=source_name,
                    sender=msg.sender,
                    date=f"{msg.date} {msg.time}",
                )
                if not profile:
                    continue
                if profile["import_hash"] in seen_hashes:
                    continue
                seen_hashes.add(profile["import_hash"])
                profiles.append(profile)
    return profiles, total_candidates, "; ".join(notes)


def import_zip_to_db(zip_path: str | Path, created_by_user_id: int | None = None) -> dict:
    profiles, total_candidates, notes = profiles_from_zip(zip_path)
    inserted = duplicates = skipped = 0
    filename = Path(zip_path).name
    with db() as conn:
        for p in profiles:
            existing = conn.execute("SELECT id FROM profiles WHERE import_hash = ?", (p["import_hash"],)).fetchone()
            if existing:
                duplicates += 1
                continue
            reference_code = f"NP-{p['import_hash'][:8].upper()}"
            try:
                conn.execute(
                    """
                    INSERT INTO profiles (
                        reference_code, status, profile_type, full_name, age, height, city, district, country,
                        marital_status, education, profession, family_background, faith_notes, expectations,
                        bio_summary, contact_details, raw_text, source_name, source_sender, source_message_at,
                        import_hash, created_by_user_id
                    ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reference_code,
                        p.get("profile_type"),
                        p.get("full_name"),
                        p.get("age"),
                        p.get("height"),
                        p.get("city"),
                        p.get("district"),
                        p.get("country"),
                        p.get("marital_status"),
                        p.get("education"),
                        p.get("profession"),
                        p.get("family_background"),
                        p.get("faith_notes"),
                        p.get("expectations"),
                        p.get("bio_summary"),
                        p.get("contact_details"),
                        p.get("raw_text"),
                        p.get("source_name"),
                        p.get("source_sender"),
                        p.get("source_message_at"),
                        p.get("import_hash"),
                        created_by_user_id,
                    ),
                )
                inserted += 1
            except Exception:
                skipped += 1
        batch_id = conn.execute(
            """
            INSERT INTO import_batches
            (filename, source_name, total_candidates, inserted, duplicates, skipped, notes, created_by_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (filename, filename, total_candidates, inserted, duplicates, skipped, notes, created_by_user_id),
        ).lastrowid
    return {
        "batch_id": batch_id,
        "filename": filename,
        "total_candidates": total_candidates,
        "parsed_profiles": len(profiles),
        "inserted": inserted,
        "duplicates": duplicates,
        "skipped": skipped,
        "notes": notes,
    }
