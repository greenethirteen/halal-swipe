from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import quote

try:
    import stripe
except ImportError:  # Stripe is optional until billing is configured.
    stripe = None
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_user, hash_password, has_active_subscription, is_admin, role_for_email, verify_password
from .database import all_rows, db, execute, init_db, one
from .parser import EMAIL_RE, PHONE_RE, first_phone, import_zip_to_db, normalised_hash, remove_contact_from_summary, to_wa_number
from .settings import get_settings

load_dotenv()
settings = get_settings()
if stripe:
    stripe.api_key = settings.stripe_secret_key or None

app = FastAPI(title=settings.app_name)
app.add_middleware(SessionMiddleware, secret_key=settings.app_secret, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")
templates = Jinja2Templates(directory="app/templates")


@app.middleware("http")
async def no_store_html(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.on_event("startup")
def startup() -> None:
    init_db()


def flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def render(request: Request, template: str, context: dict | None = None) -> HTMLResponse:
    context = context or {}
    user = current_user(request)
    context.update(
        {
            "request": request,
            "user": user,
            "is_admin": is_admin(user),
            "is_subscribed": has_active_subscription(user),
            "settings": settings,
            "flash": request.session.pop("flash", None),
        }
    )
    return templates.TemplateResponse(template, context)


def require_login(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_admin(request: Request) -> dict:
    user = require_login(request)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def allowed_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}


WHATSAPP_INTRO = "Assalamualaikum. I came across your profile regarding marriage."
PUBLIC_TEXT_FIELDS = {
    "full_name",
    "city",
    "district",
    "country",
    "marital_status",
    "education",
    "profession",
    "family_background",
    "faith_notes",
    "expectations",
    "bio_summary",
    "raw_text",
}
PLACEHOLDER_VALUES = {"", "not specified", "upon request", "available upon request", "n/a", "na", "none", "null", "nil", "-"}
QUALIFICATION_RE = re.compile(
    r"\b(?:o/l|a/l|acca|hnd|hnda|bsc|ba|bcom|llb|mbbs|msc|mba|phd|diplomas?|dip\.?|degrees?|"
    r"graduate|undergraduate|bachelor|master|certificate|nvq)\b",
    re.IGNORECASE,
)
PUBLIC_FIELD_NOISE_RE = re.compile(
    r"\b(?:father|mother|sibling|siblings|expected\s+(?:bride|groom)|bride\s+details|groom\s+details|"
    r"contact|whatsapp|phone|attached:)\b",
    re.IGNORECASE,
)

# --- Browse filter helpers ----------------------------------------------------
# Qualification buckets, checked in order (first match wins).
QUALIFICATION_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Master's / Postgrad", ["msc", "m.sc", "mba", "m.a", "mphil", "master", "postgrad", "pgd", "phd", "doctorate"]),
    ("Professional (ACCA, CIMA…)", ["acca", "cima", "cma", "aat", "charter", "icasl", "cfa"]),
    ("Medical (MBBS, Nursing…)", ["mbbs", "bds", "md ", "nursing", "nurse", "pharmac", "dental"]),
    ("Bachelor's degree", ["bsc", "b.sc", "ba ", "b.a", "bcom", "b.com", "bba", "bit", "bed", "b.ed", "llb", "bachelor", "degree", "hons", "honours", "graduate"]),
    ("Diploma / HND", ["diploma", "hnd", "hnda", "dip.", "dip ", "higher national", "certificate", "nvq"]),
    ("School (O/L, A/L)", ["o/l", "a/l", "o level", "a level", "ol ", "al ", "g.c.e", "gce", "school", "grade"]),
]
# Job/field buckets, checked in order (specific before the generic "business").
JOB_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Healthcare / Medical", ["doctor", "physician", "nurse ", "registered nurse", "mbbs", "physio", "medical", "pharmac", "dental", "surgeon", "clinic", "therapist", "pathology", "health", " gp ", "midwife"]),
    ("Accounting / Finance", ["account", "financ", "audit", "bank", "actuar", "tax", "bookkeep", "controller"]),
    ("IT / Software", ["software", "developer", "programmer", " it ", "information tech", "qa ", "quality assurance", "data ", "network", "system", "devops", "web ", "tech "]),
    ("Engineering", ["engineer", "engineering", "surveyor"]),
    ("Education / Teaching", ["teacher", "teaching", "lecturer", "tutor", "educat", "academ", "demonstrator", "quran", "quraan", "ustad", "moulavi"]),
    ("Design / Creative", ["design", "architect", "creative", "graphic", "interior"]),
    ("Aviation", ["pilot", "cabin crew", "aviation", "airline"]),
    ("Hospitality / Food", ["chef", "cook", "restaurant", "hotel", "catering", "barista", "waiter", "kitchen", "hospitality"]),
    ("Skilled trade / Technical", ["technician", "fabricator", "mechanic", "electrician", "plumber", "welder", "driver", "tailor", "carpenter", "machinist", "fitter", "labour"]),
    ("Student", ["student", "undergraduate", "studying", "following a", "trainee", "intern"]),
    ("Not working / Homemaker", ["not working", "housewife", "homemaker", "unemployed"]),
    ("Business / Management", ["business", "manager", "management", "executive", "coordinator", "consultant", "officer", "administ", "marketing", "sales", "human resource", "purchase", "supply", "procurement", "operations", "entrepreneur", "self employed", "clerk", "assistant", "analyst", "cashier", "receptionist", "customer service", "supervisor", "chairman", "director", "shop", "grocery", "boutique", "trader", "trading", "store", "private sector", "company"]),
]
ABROAD_KEYWORDS = [
    "qatar", "doha", "uae", "u.a.e", "dubai", "abu dhabi", "sharjah", "saudi", "ksa", "riyadh", "jeddah",
    "middle east", "gulf", "kuwait", "bahrain", "oman", "muscat", "australia", "sydney", "melbourne",
    "uk", "u.k", "united kingdom", "london", "england", "britain", "scotland", "usa", "u.s.a",
    "united states", "america", "canada", "toronto", "germany", "france", "italy", "europe", "norway",
    "sweden", "switzerland", "netherlands", "new zealand", "singapore", "malaysia", "japan", "korea",
    "maldives", "south africa", "ireland", "spain", "denmark", "finland", "austria", "belgium",
]
ABROAD_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in ABROAD_KEYWORDS) + r")\b", re.IGNORECASE)
PLACE_BAD_WORDS_RE = re.compile(
    r"\b(?:age|limit|family|residing|reside|qualification|qualified|mannered|oriented|salary|height|"
    r"seeking|looking|education|diploma|degree|officer|company|agency|background|hearted|respectable|"
    r"currently|street|road|no)\b",
    re.IGNORECASE,
)


def categorize(value: object, categories: list[tuple[str, list[str]]]) -> str:
    text = f" {str(value or '').lower()} "
    for label, keywords in categories:
        if any(kw in text for kw in keywords):
            return label
    return ""


def clean_place(value: object) -> str:
    """Return a tidy place name, or '' if the value looks like noise/an address."""
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    s = re.sub(r"^[\[\(\{]+|[\]\)\}\.,;:\s]+$", "", s).strip()
    if not s or len(s) > 30:
        return ""
    if re.search(r"\d", s) or re.search(r"[.;:/]", s):
        return ""
    if re.search(r"[؀-ۿ]", s) or PLACE_BAD_WORDS_RE.search(s):
        return ""
    if len([p for p in re.split(r"\s*,\s*|\s+", s) if p]) > 4:
        return ""
    return s


def normalize_marital_status(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    low = text.lower()
    if low in PLACEHOLDER_VALUES or low in {"unknown"}:
        return None
    if re.search(r"\b(?:widow|widower|widowed)\b", low):
        return "Widowed"
    if re.search(r"\b(?:separated|seperated)\b", low):
        return "Separated"
    if re.search(r"\b(?:annulled|annulment)\b", low):
        return "Annulled"
    if re.search(r"\b(?:divorc|devorc|devos|devoce|divoce|divors|divos|divoc|dovorc)\w*\b", low):
        return "Divorced"
    if re.search(r"\b(?:never\s*married|unmarried|single|not\s*married)\b", low):
        return "Never married"
    if re.fullmatch(r"married", low):
        return "Married"
    return None


def is_abroad(profile: dict) -> bool:
    # The cleaned data carries a real current country, so trust it first.
    country = str(profile.get("country") or "").strip().lower()
    if country and "sri lanka" not in country:
        return True
    # Fall back to scanning text for a foreign mention (e.g. job "in the UAE").
    blob = " ".join(str(profile.get(f) or "") for f in ("city", "district", "profession", "bio_summary"))
    return bool(ABROAD_RE.search(blob))


def contact_view_count(user_id: int) -> int:
    row = one("SELECT COUNT(*) AS c FROM contact_views WHERE user_id = ?", (user_id,))
    return int(row["c"]) if row else 0


def strip_public_contact_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = PHONE_RE.sub("[contact hidden]", value)
    text = EMAIL_RE.sub("[email hidden]", text)
    text = re.sub(r"\[[^\]]*\d{4}[^\]]*\]\s*[^:]+:\s*", "", text)
    text = re.sub(r"<attached:[^>]+>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[\w .'-]+\.(?:pdf|jpe?g|png|webp)\b", "", text, flags=re.IGNORECASE)
    text = text.replace("[Passed away]", "Passed away").replace("[passed away]", "passed away")
    return text


def public_pieces(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    text = strip_public_contact_text(value)
    text = re.sub(r"^&\s*Profession:?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^[^\w]*(?:educational\s*&\s*professional\s+qualifications?|occupational\s+background)\s*:?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text.replace("\n", "; ")).strip(" ;,")
    return [p.strip(" ;,•▪") for p in re.split(r"\s*[;|]\s*", text) if p.strip(" ;,•▪")]


def short_join(pieces: list[str], limit: int) -> str | None:
    out = []
    for piece in pieces:
        if not piece or piece in out:
            continue
        out.append(piece)
        if len("; ".join(out)) >= limit:
            break
    text = "; ".join(out).strip()
    return text[:limit].rstrip(" ;,") if text else None


def compact_public_education(value: object) -> str | None:
    clean = [p for p in public_pieces(value) if not PUBLIC_FIELD_NOISE_RE.search(p)]
    qualifications = [p for p in clean if QUALIFICATION_RE.search(p)]
    # Prefer recognised qualifications (drops noise in long messy blobs), but
    # fall back to the clean value so tidy entries like AAT / CIMA / Islamic
    # studies still show instead of disappearing.
    pieces = qualifications or [p for p in clean if len(p) <= 60]
    return short_join(pieces[:3], 140)


def compact_public_work(value: object) -> str | None:
    pieces = []
    for piece in public_pieces(value):
        low = piece.lower()
        if PUBLIC_FIELD_NOISE_RE.search(piece):
            continue
        if QUALIFICATION_RE.search(piece):
            continue
        if re.search(r"\b(?:completed\s+o/l|completed\s+a/l|qualification|passed away)\b", low):
            continue
        piece = re.sub(r"^(?:currently\s+)?(?:working\s+)?(?:as\s+)?", "", piece, flags=re.IGNORECASE).strip()
        piece = re.sub(r"^(?:al|a/l)\s+background\s*", "", piece, flags=re.IGNORECASE).strip()
        if piece:
            pieces.append(piece)
    return short_join(pieces[:2], 140)


def compact_public_long_text(value: object, limit: int = 420) -> str | None:
    pieces = [
        p for p in public_pieces(value)
        if not re.search(r"\b(?:contact|whatsapp|phone|attached:|bride\s+details|groom\s+details)\b", p, flags=re.IGNORECASE)
    ]
    return short_join(pieces, limit)


def compact_public_expectations(value: object, profile_type: str | None = None) -> str | None:
    pieces = []
    for piece in public_pieces(value):
        cleaned = re.sub(r"^looking\s+for\s*:?\s*", "", piece, flags=re.IGNORECASE).strip(" :")
        cleaned = re.sub(r"^for\s+(?:a\s+)?", "", cleaned, flags=re.IGNORECASE).strip(" :")
        if cleaned.lower() in {"bride", "groom", "suitable partner", "partner"}:
            continue
        if profile_type and cleaned.lower() == str(profile_type).lower():
            continue
        if re.search(r"\b(?:contact|whatsapp|phone|attached:|bride\s+details|groom\s+details)\b", cleaned, flags=re.IGNORECASE):
            continue
        pieces.append(cleaned)
    return short_join(pieces, 360)


def public_title(profile: dict) -> str:
    title = str(profile.get("full_name") or "").strip()
    low = title.lower()
    generic = {
        "",
        "profile",
        "marriage profile",
        "bride profile",
        "groom profile",
        "bride details",
        "groom details",
        "of the bridal",
    }
    if low in generic or "details" in low:
        profile_type = profile.get("profile_type") or "Profile"
        city = profile.get("city")
        return f"{profile_type} from {city}" if city else f"{profile_type} profile"
    return title


def public_summary(profile: dict) -> str:
    profile_type = str(profile.get("profile_type") or "profile").lower()
    age = profile.get("age")
    city = profile.get("city")
    intro = f"A {age}-year-old {profile_type}" if age else f"A {profile_type}"
    if city:
        intro += f" from {city}"
    bits = [intro + "."]
    if profile.get("education"):
        bits.append(f"Highest qualification: {profile['education']}.")
    if profile.get("profession"):
        bits.append(f"Work: {profile['profession']}.")
    return " ".join(bits)


def public_profile(row) -> dict:
    profile = dict(row)
    for field in PUBLIC_TEXT_FIELDS:
        if field in profile:
            profile[field] = strip_public_contact_text(profile[field])
    profile["marital_status"] = normalize_marital_status(profile.get("marital_status"))
    profile["full_name"] = public_title(profile)
    profile["education"] = compact_public_education(profile.get("education"))
    profile["profession"] = compact_public_work(profile.get("profession"))
    profile["family_background"] = compact_public_long_text(profile.get("family_background")) or profile.get("family_background")
    profile["expectations"] = compact_public_expectations(profile.get("expectations"), profile.get("profile_type"))
    # Prefer the imported narrative bio; only synthesise one when it's missing.
    stored_bio = (profile.get("bio_summary") or "").strip()
    profile["bio_summary"] = stored_bio or public_summary(profile)
    return profile


def has_viewed_contact(user_id: int, profile_id: int) -> bool:
    return bool(one("SELECT 1 FROM contact_views WHERE user_id = ? AND profile_id = ?", (user_id, profile_id)))


def record_contact_view(user_id: int, profile_id: int) -> None:
    execute("INSERT OR IGNORE INTO contact_views (user_id, profile_id) VALUES (?, ?)", (user_id, profile_id))


def contact_gate(request: Request, profile_id: int):
    """Enforce login -> subscription -> free-contact limit.

    Returns (allowed: bool, redirect_response_or_None).
    Records a contact view for free users on first reveal of a profile.
    """
    user = current_user(request)
    if not user:
        return False, redirect(f"/login?next=/profiles/{profile_id}")
    if has_active_subscription(user) or has_viewed_contact(user["id"], profile_id):
        return True, None
    if contact_view_count(user["id"]) >= settings.free_contact_limit:
        flash(
            request,
            f"You've used your {settings.free_contact_limit} free contacts. "
            "Subscribe for $5/month for unlimited access.",
        )
        return False, redirect("/pricing")
    record_contact_view(user["id"], profile_id)
    return True, None


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return redirect("/profiles")


@app.get("/profiles", response_class=HTMLResponse)
def profiles(request: Request) -> HTMLResponse:
    # All filtering happens live in the browser, so just hand over every
    # approved profile (enriched with the metadata the filters key off).
    rows = all_rows(
        """
        SELECT * FROM profiles
        WHERE status = 'approved'
        ORDER BY created_at DESC
        LIMIT 600
        """
    )
    profiles_out: list[dict] = []
    locations: set[str] = set()
    marital_statuses: set[str] = set()
    for row in rows:
        p = public_profile(row)
        p["qual_category"] = categorize(row["education"], QUALIFICATION_CATEGORIES)
        p["job_category"] = categorize(row["profession"], JOB_CATEGORIES)
        p["abroad"] = is_abroad(dict(row))
        # A profile is findable by hometown, current city and (if abroad) country.
        places: list[str] = []
        for raw in (row["city"], row["district"]):
            cp = clean_place(raw)
            if cp and cp not in places:
                places.append(cp)
        country_val = (row["country"] or "").strip()
        if country_val and country_val.lower() != "sri lanka":
            cp = clean_place(country_val)
            if cp and cp not in places:
                places.append(cp)
        p["location"] = places[0] if places else ""
        p["locations"] = places
        locations.update(places)
        marital = (p.get("marital_status") or "").strip()
        if marital and len(marital) <= 24:
            marital_statuses.add(marital)
        search_blob = " ".join(
            str(v or "") for v in (
                p.get("full_name"), p.get("city"), p.get("district"),
                row["education"], row["profession"], p.get("bio_summary"),
                p.get("reference_code"),
            )
        ).lower()
        p["search_blob"] = search_blob
        profiles_out.append(p)

    return render(
        request,
        "profiles.html",
        {
            "profiles": profiles_out,
            "locations": sorted(locations, key=str.lower),
            "marital_statuses": sorted(marital_statuses, key=str.lower),
            "qual_categories": [c[0] for c in QUALIFICATION_CATEGORIES],
            "job_categories": [c[0] for c in JOB_CATEGORIES],
        },
    )


@app.get("/profiles/{profile_id}", response_class=HTMLResponse)
def profile_detail(request: Request, profile_id: int) -> HTMLResponse:
    row = one("SELECT * FROM profiles WHERE id = ? AND status = 'approved'", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    public_row = public_profile(row)
    user = current_user(request)
    has_contact = bool(to_wa_number(first_phone(row["contact_details"])))
    contact_unlocked = bool(
        user and (has_active_subscription(user) or has_viewed_contact(user["id"], profile_id))
    )
    return render(
        request,
        "profile_detail.html",
        {"profile": public_row, "has_contact": has_contact, "contact_unlocked": contact_unlocked},
    )


@app.get("/profiles/{profile_id}/whatsapp")
def profile_whatsapp(request: Request, profile_id: int):
    row = one("SELECT * FROM profiles WHERE id = ? AND status = 'approved'", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    allowed, resp = contact_gate(request, profile_id)
    if not allowed:
        return resp
    wa = to_wa_number(first_phone(row["contact_details"]))
    if not wa:
        flash(request, "No WhatsApp or phone number is available for this profile.")
        return redirect(f"/profiles/{profile_id}")
    return redirect(f"https://wa.me/{wa}?text={quote(WHATSAPP_INTRO)}")


@app.get("/profiles/{profile_id}/call")
def profile_call(request: Request, profile_id: int):
    row = one("SELECT * FROM profiles WHERE id = ? AND status = 'approved'", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    allowed, resp = contact_gate(request, profile_id)
    if not allowed:
        return resp
    phone = first_phone(row["contact_details"])
    wa = to_wa_number(phone)
    if not (phone or wa):
        flash(request, "No phone number is available for this profile.")
        return redirect(f"/profiles/{profile_id}")
    return redirect(f"tel:+{wa}" if wa else f"tel:{phone}")


@app.get("/submit", response_class=HTMLResponse)
def submit_profile_page(request: Request) -> HTMLResponse:
    require_login(request)
    return render(request, "submit.html")


@app.post("/submit")
def submit_profile(
    request: Request,
    profile_type: str = Form(...),
    full_name: str = Form(""),
    age: int | None = Form(None),
    height: str = Form(""),
    city: str = Form(""),
    district: str = Form(""),
    marital_status: str = Form(""),
    education: str = Form(""),
    profession: str = Form(""),
    family_background: str = Form(""),
    faith_notes: str = Form(""),
    expectations: str = Form(""),
    bio_summary: str = Form(""),
    contact_details: str = Form(""),
    consent: str = Form(""),
    image: UploadFile | None = File(None),
):
    user = require_login(request)
    if consent != "yes":
        flash(request, "Please confirm you have permission to submit this profile.")
        return redirect("/submit")
    if age is not None and age < 18:
        flash(request, "Profiles must be for adults aged 18 or above.")
        return redirect("/submit")

    image_path = None
    if image and image.filename:
        if not allowed_image(image.filename):
            flash(request, "Please upload a JPG, PNG or WebP image.")
            return redirect("/submit")
        suffix = Path(image.filename).suffix.lower()
        name = f"profile_images/{uuid.uuid4().hex}{suffix}"
        dest = settings.upload_dir / name
        with dest.open("wb") as out:
            shutil.copyfileobj(image.file, out)
        image_path = name

    raw_text = "\n".join(
        filter(
            None,
            [
                f"Name: {full_name}" if full_name else "",
                f"Age: {age}" if age else "",
                f"Height: {height}" if height else "",
                f"Location: {city}" if city else "",
                f"Education: {education}" if education else "",
                f"Profession: {profession}" if profession else "",
                f"Bio: {bio_summary}" if bio_summary else "",
                f"Expectations: {expectations}" if expectations else "",
            ],
        )
    )
    profile_hash = normalised_hash(raw_text + contact_details + str(user["id"]))
    reference_code = f"NP-{profile_hash[:8].upper()}"
    execute(
        """
        INSERT INTO profiles (
            reference_code, status, profile_type, full_name, age, height, city, district, marital_status,
            education, profession, family_background, faith_notes, expectations, bio_summary, contact_details,
            raw_text, image_path, import_hash, created_by_user_id
        ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reference_code,
            profile_type,
            full_name.strip() or None,
            age,
            height.strip() or None,
            city.strip() or None,
            district.strip() or None,
            marital_status.strip() or None,
            education.strip() or None,
            profession.strip() or None,
            family_background.strip() or None,
            faith_notes.strip() or None,
            expectations.strip() or None,
            remove_contact_from_summary(bio_summary.strip()) or None,
            contact_details.strip() or None,
            raw_text,
            image_path,
            profile_hash,
            user["id"],
        ),
    )
    flash(request, "Profile submitted. It will appear after admin review.")
    return redirect("/profiles")


def safe_next(target: str) -> str:
    """Only allow internal redirects."""
    return target if target.startswith("/") and not target.startswith("//") else "/"


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, next: str = "/account") -> HTMLResponse:
    return render(request, "register.html", {"next": safe_next(next)})


@app.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    next: str = Form("/account"),
):
    email = email.lower().strip()
    if len(password) < 8:
        flash(request, "Please use a password of at least 8 characters.")
        return redirect("/register")
    if one("SELECT id FROM users WHERE email = ?", (email,)):
        flash(request, "That email is already registered. Please log in.")
        return redirect("/login")
    user_id = execute(
        "INSERT INTO users (email, full_name, password_hash, role) VALUES (?, ?, ?, ?)",
        (email, full_name.strip(), hash_password(password), role_for_email(email)),
    )
    request.session["user_id"] = user_id
    flash(request, "Welcome. Your account is ready.")
    return redirect(safe_next(next))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/account") -> HTMLResponse:
    return render(request, "login.html", {"next": safe_next(next)})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/account")):
    row = one("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))
    if not row or not verify_password(password, row["password_hash"]):
        flash(request, "Invalid email or password.")
        return redirect("/login")
    request.session["user_id"] = row["id"]
    flash(request, "Logged in.")
    return redirect(safe_next(next))


@app.get("/account", response_class=HTMLResponse)
def account(request: Request) -> HTMLResponse:
    user = require_login(request)
    my_profiles = all_rows(
        "SELECT * FROM profiles WHERE created_by_user_id = ? ORDER BY created_at DESC", (user["id"],)
    )
    return render(
        request,
        "account.html",
        {
            "my_profiles": my_profiles,
            "contacts_used": contact_view_count(user["id"]),
            "contact_limit": settings.free_contact_limit,
        },
    )


@app.get("/about", response_class=HTMLResponse)
def about(request: Request) -> HTMLResponse:
    return render(request, "about.html")


@app.get("/healthz")
def healthz() -> dict:
    """Diagnostic: which DB engine is live and how many profiles it has."""
    from .database import is_postgres

    try:
        total = one("SELECT COUNT(*) AS c FROM profiles")["c"]
        approved = one("SELECT COUNT(*) AS c FROM profiles WHERE status='approved'")["c"]
        users = one("SELECT COUNT(*) AS c FROM users")["c"]
    except Exception as exc:  # surface DB errors instead of hiding them
        return {"engine": "postgres" if is_postgres() else "sqlite", "error": str(exc)}
    return {
        "engine": "postgres" if is_postgres() else "sqlite",
        "base_url_origin": settings.base_url_origin,
        "google_signin": settings.google_signin_enabled,
        "google_client_id_configured": bool(settings.google_client_id_clean),
        "google_client_id_valid_format": settings.google_client_id_valid_format,
        "stripe_checkout_configured": bool(stripe and settings.stripe_secret_key and settings.stripe_price_id),
        "stripe_webhook_configured": bool(stripe and settings.stripe_webhook_secret),
        "demo_mode": settings.demo_mode,
        "profiles_total": total,
        "profiles_approved": approved,
        "users": users,
    }


@app.post("/auth/google")
async def auth_google(request: Request):
    """Verify a Google Identity Services credential and sign the user in."""
    if not settings.google_client_id_clean:
        flash(request, "Google sign-in is not configured.")
        return redirect("/login")
    if not settings.google_client_id_valid_format:
        flash(request, "Google sign-in is misconfigured.")
        return redirect("/login")
    form = await request.form()
    credential = form.get("credential", "")
    # CSRF: the g_csrf_token cookie must match the posted token (Google double-submit)
    cookie_token = request.cookies.get("g_csrf_token")
    if not cookie_token or cookie_token != form.get("g_csrf_token"):
        flash(request, "Google sign-in failed (security check). Please try again.")
        return redirect("/login")
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token

        info = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), settings.google_client_id_clean
        )
    except Exception:
        flash(request, "Google sign-in failed. Please try again.")
        return redirect("/login")

    email = (info.get("email") or "").lower().strip()
    if not email or not info.get("email_verified"):
        flash(request, "Your Google account email could not be verified.")
        return redirect("/login")

    row = one("SELECT * FROM users WHERE email = ?", (email,))
    if row:
        user_id = row["id"]
    else:
        import secrets as _secrets

        name = info.get("name") or email.split("@")[0]
        user_id = execute(
            "INSERT INTO users (email, full_name, password_hash, role) VALUES (?, ?, ?, ?)",
            (email, name, hash_password(_secrets.token_urlsafe(32)), role_for_email(email)),
        )
    request.session["user_id"] = user_id
    flash(request, "Signed in with Google.")
    return redirect("/account")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/")


@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request) -> HTMLResponse:
    return render(request, "pricing.html")


@app.post("/billing/checkout")
def create_checkout(request: Request):
    user = require_login(request)
    if stripe is None or not settings.stripe_secret_key or not settings.stripe_price_id:
        flash(request, "Stripe is not configured yet. Add STRIPE_SECRET_KEY and STRIPE_PRICE_ID in .env.")
        return redirect("/pricing")
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=user["email"],
        line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
        success_url=f"{settings.base_url_origin}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.base_url_origin}/pricing",
        metadata={"user_id": str(user["id"])}
    )
    return RedirectResponse(session.url, status_code=303)


@app.get("/billing/success")
def billing_success(request: Request, session_id: str = ""):
    user = require_login(request)
    if stripe is not None and settings.stripe_secret_key and session_id:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            metadata = getattr(session, "metadata", None) or {}
            metadata_user_id = str(metadata.get("user_id", ""))
            session_email = (getattr(session, "customer_email", None) or "").lower().strip()
            current_email = (user.get("email") or "").lower().strip()
            if (
                metadata_user_id == str(user["id"])
                and session_email == current_email
                and session.payment_status in {"paid", "no_payment_required"}
            ):
                with db() as conn:
                    conn.execute(
                        "UPDATE users SET subscription_status = ?, stripe_customer_id = ?, stripe_subscription_id = ? WHERE id = ?",
                        (
                            "active",
                            getattr(session, "customer", None),
                            getattr(session, "subscription", None),
                            user["id"],
                        ),
                    )
        except Exception:
            pass
    flash(request, "Subscription updated. You can now view contact details.")
    return redirect("/profiles")


@app.post("/billing/demo-activate")
def demo_activate(request: Request):
    user = require_login(request)
    if not settings.demo_mode:
        raise HTTPException(403, "Demo subscriptions are disabled")
    execute("UPDATE users SET subscription_status = 'active' WHERE id = ?", (user["id"],))
    flash(request, "Demo subscription activated.")
    return redirect("/profiles")


@app.post("/billing/portal")
def billing_portal(request: Request):
    """Open the Stripe customer billing portal to manage/cancel the subscription."""
    user = require_login(request)
    if stripe and settings.stripe_secret_key and user.get("stripe_customer_id"):
        try:
            session = stripe.billing_portal.Session.create(
                customer=user["stripe_customer_id"],
                return_url=f"{settings.base_url_origin}/account",
            )
            return RedirectResponse(session.url, status_code=303)
        except Exception:
            flash(request, "Could not open the billing portal. Please try again later.")
    return redirect("/account")


@app.post("/billing/cancel")
def billing_cancel(request: Request):
    user = require_login(request)
    if stripe and settings.stripe_secret_key and user.get("stripe_subscription_id"):
        try:
            stripe.Subscription.modify(user["stripe_subscription_id"], cancel_at_period_end=True)
            flash(request, "Your subscription will end at the close of the current billing period.")
        except Exception:
            flash(request, "Could not cancel automatically. Please use Manage billing or contact support.")
        return redirect("/account")
    # Demo / local subscription
    execute("UPDATE users SET subscription_status = 'none' WHERE id = ?", (user["id"],))
    flash(request, "Subscription cancelled.")
    return redirect("/account")


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if stripe is None or not settings.stripe_webhook_secret:
        raise HTTPException(400, "Webhook secret not configured")
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, settings.stripe_webhook_secret)
    except Exception as exc:
        raise HTTPException(400, str(exc))

    event_type = event["type"]
    data = event["data"]["object"]
    if event_type == "checkout.session.completed":
        user_id = data.get("metadata", {}).get("user_id")
        if user_id:
            with db() as conn:
                conn.execute(
                    "UPDATE users SET subscription_status = 'active', stripe_customer_id = ?, stripe_subscription_id = ? WHERE id = ?",
                    (data.get("customer"), data.get("subscription"), user_id),
                )
    elif event_type in {"customer.subscription.deleted", "customer.subscription.paused"}:
        sub_id = data.get("id")
        with db() as conn:
            conn.execute("UPDATE users SET subscription_status = 'inactive' WHERE stripe_subscription_id = ?", (sub_id,))
    elif event_type in {"customer.subscription.updated"}:
        sub_id = data.get("id")
        status = data.get("status") or "inactive"
        with db() as conn:
            conn.execute("UPDATE users SET subscription_status = ? WHERE stripe_subscription_id = ?", (status, sub_id))
    return {"ok": True}


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request) -> HTMLResponse:
    require_admin(request)
    stats = one(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved,
          SUM(CASE WHEN status = 'hidden' THEN 1 ELSE 0 END) AS hidden
        FROM profiles
        """
    )
    batches = all_rows("SELECT * FROM import_batches ORDER BY created_at DESC LIMIT 10")
    pending = all_rows("SELECT * FROM profiles WHERE status = 'pending' ORDER BY created_at DESC LIMIT 30")
    return render(request, "admin.html", {"stats": stats, "batches": batches, "pending": pending})


@app.post("/admin/import")
def admin_import_zip(request: Request, zip_file: UploadFile = File(...)):
    user = require_admin(request)
    if not zip_file.filename.lower().endswith(".zip"):
        flash(request, "Please upload a .zip WhatsApp export.")
        return redirect("/admin")
    dest = settings.upload_dir / "imports" / f"{uuid.uuid4().hex}-{Path(zip_file.filename).name}"
    with dest.open("wb") as out:
        shutil.copyfileobj(zip_file.file, out)
    result = import_zip_to_db(dest, created_by_user_id=user["id"])
    flash(
        request,
        f"Import scanned {result['total_candidates']} candidates: {result['inserted']} new, {result['duplicates']} duplicates, {result['skipped']} skipped.",
    )
    return redirect("/admin")


@app.post("/admin/profiles/{profile_id}/approve")
def admin_approve(request: Request, profile_id: int):
    require_admin(request)
    execute("UPDATE profiles SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (profile_id,))
    flash(request, "Profile approved.")
    return redirect("/admin")


@app.post("/admin/profiles/{profile_id}/hide")
def admin_hide(request: Request, profile_id: int):
    require_admin(request)
    execute("UPDATE profiles SET status = 'hidden', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (profile_id,))
    flash(request, "Profile hidden.")
    return redirect("/admin")


@app.get("/admin/profiles/{profile_id}", response_class=HTMLResponse)
def admin_profile_detail(request: Request, profile_id: int) -> HTMLResponse:
    require_admin(request)
    row = one("SELECT * FROM profiles WHERE id = ?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    return render(request, "admin_profile_detail.html", {"profile": row})
