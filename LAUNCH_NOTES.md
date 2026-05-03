# FORGE India Launch Notes

## Run locally

```powershell
cd C:\Users\ADMIN\Downloads\self
python app.py
```

Open:

```text
http://127.0.0.1:8000
```

Admin login:

```text
admin@forgeindia.site
forge2026
```

## Real Razorpay mode

Set these environment variables before running the server:

```powershell
$env:RAZORPAY_KEY_ID="rzp_live_xxxxx"
$env:RAZORPAY_KEY_SECRET="xxxxx"
python app.py
```

Without those keys, payments run in demo-confirm mode but still store payment rows and update application/premium status.

## Gmail confirmation mail

Use a Gmail app password, not your normal Gmail password:

```powershell
$env:GMAIL_USER="forgeindiaoff@gmail.com"
$env:GMAIL_APP_PASSWORD="your-gmail-app-password"
python app.py
```

Without these values, every email is written to the admin email outbox table so you can still verify the flow.

## What is working

- Founder signup/login with compulsory photo upload.
- Auto-updating landing stats for founders, communities, opportunities, and applications.
- Admin opportunity creation with type, link, direct/external apply mode, free/paid fee, GPS, reminder date, deadline, and requirements.
- Direct application form stored in SQLite.
- External official-link application tracking with proof checkbox.
- Paid application final step through Razorpay live mode or demo-confirm mode.
- FORGE Pro Rs. 49 checkout and premium-only room access.
- Gmail/app confirmation mail or local outbox logging.
- Community rooms with chat and joining counts.
- Co-founder matching with request/skip and accept/reject network flow.
- Automatic profile score/ranking based on profile completeness, applications, chat, connections, and premium.
- Free rule-based advisor that replies with the user's name and does not use paid AI APIs.

