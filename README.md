# Nikah Profiles — subscription web app

A slick FastAPI + SQLite starter app for turning Sri Lankan WhatsApp marriage biodata exports into searchable profile cards.

## What it includes

- Public landing page and searchable profile directory
- User registration/login
- User-submitted profiles with image upload
- Admin review queue before anything goes live
- Admin WhatsApp zip importer for `_chat.txt` exports
- Duplicate-safe importing using a normalised hash of each biodata message
- Contact details locked behind a `$5/month` subscription
- Stripe Checkout + webhook hooks for production billing
- Local demo subscription mode for testing without Stripe

## Important privacy note

This code does **not** include the real profiles from the uploaded WhatsApp zips. It gives you the importer so you can upload zips from the admin dashboard after you run the app.

Because marriage biodata can contain sensitive personal information, use this only for profiles submitted with permission. The app includes an 18+ consent checkbox and admin review, but you should also add your own privacy policy, terms, takedown process and consent workflow before launching publicly.

## Quick start

```bash
cd nikah_profiles_app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000
```

## Create the admin user

Set your admin email in `.env`:

```text
ADMIN_EMAIL="your@email.com"
```

Then register with that same email in the app. That account becomes an admin automatically.

## Import WhatsApp zips

Option 1: Use the admin dashboard:

```text
/admin → Upload WhatsApp export zip → Scan and import new profiles
```

Option 2: Command line:

```bash
python scripts/import_zip.py "/path/to/WhatsApp Chat.zip"
```

New imports are saved as `pending`. Approve them from `/admin` before they appear publicly.

## Stripe subscription setup

Create a recurring Stripe Price for USD 5/month, then add these to `.env`:

```text
DEMO_MODE="false"
STRIPE_SECRET_KEY="sk_live_..."
STRIPE_PRICE_ID="price_..."
STRIPE_WEBHOOK_SECRET="whsec_..."
BASE_URL="https://your-domain.com"
```

Point your Stripe webhook to:

```text
https://your-domain.com/stripe/webhook
```

The app handles:

- `checkout.session.completed`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `customer.subscription.paused`

## Suggested production checklist

- Deploy behind HTTPS
- Set a strong `APP_SECRET`
- Disable `DEMO_MODE`
- Use a managed database instead of local SQLite for scale
- Add email verification and password reset
- Add takedown/report buttons
- Add stricter moderation before publishing contact details
- Review local laws around consent, privacy and handling sensitive biodata

## Project structure

```text
app/
  main.py          # routes, auth, billing, admin dashboard
  parser.py        # WhatsApp zip parsing + deduplication
  database.py      # SQLite schema and helpers
  auth.py          # password hashing and session helpers
  settings.py      # environment settings
  templates/       # Jinja pages
  static/          # styling
scripts/
  import_zip.py    # CLI importer
uploads/           # runtime uploads and imported zips
```
