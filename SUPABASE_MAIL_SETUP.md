# FORGE Landing API, Supabase, and Mail Setup

This project now has a real public landing-form API:

```text
POST /api/landing/apply
```

The endpoint validates the form, stores every lead in local SQLite, sends or logs a confirmation email, and mirrors the lead into Supabase when Supabase environment variables are configured.

## 1. Create the Supabase table

Run this in the Supabase SQL editor:

```sql
create table if not exists public.landing_applications (
  id text primary key,
  name text not null,
  city text not null,
  email text not null,
  instagram text default '',
  idea text not null,
  primary_skill text not null,
  looking_for text default '',
  source text default 'landing',
  created_at bigint not null
);

alter table public.landing_applications enable row level security;
```

If inserts only happen from this Python backend, do not add a public insert policy. Use `SUPABASE_SERVICE_ROLE_KEY` on the server only.

## 2. Configure the backend

In PowerShell:

```powershell
cd C:\Users\ADMIN\Downloads\self

$env:SUPABASE_URL="https://YOUR_PROJECT_REF.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY="YOUR_SERVICE_ROLE_KEY"

$env:GMAIL_USER="forgeindiaoff@gmail.com"
$env:GMAIL_APP_PASSWORD="YOUR_GMAIL_APP_PASSWORD"
$env:ADMIN_NOTIFY_EMAIL="admin@forgeindia.site"

# Optional: lock browser calls to your domain instead of "*"
$env:CORS_ORIGIN="https://forgeindia.site"

python app.py
```

Gmail needs an app password, not your normal Gmail password. If Gmail variables are missing, emails are still written to the SQLite `outbox` table for testing.

## 3. Use the landing page

Open:

```text
http://127.0.0.1:8000/landing.html
```

If you host `landing.html` separately, set this before the form script runs:

```html
<script>
  window.FORGE_API_BASE = 'https://api.forgeindia.site';
</script>
```

## 4. Frontend API code

This is the submit code already wired into `landing.html`:

```js
const payload = Object.fromEntries(new FormData(form).entries());
payload.source = 'forge-landing';

const response = await fetch(`${API_BASE}/api/landing/apply`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload)
});
```

## 5. Supabase Edge Function mail option

If you prefer Supabase to send mail through Resend, create an Edge Function:

```bash
supabase functions new send-forge-mail
```

`supabase/functions/send-forge-mail/index.ts`:

```ts
const RESEND_API_KEY = Deno.env.get('RESEND_API_KEY')!;

Deno.serve(async (request) => {
  if (request.method === 'OPTIONS') {
    return new Response(null, {
      status: 204,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
      },
    });
  }

  const { to, subject, html } = await request.json();

  const resend = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${RESEND_API_KEY}`,
    },
    body: JSON.stringify({
      from: 'FORGE <hello@forgeindia.site>',
      to,
      subject,
      html,
    }),
  });

  const data = await resend.json();

  return new Response(JSON.stringify(data), {
    status: resend.ok ? 200 : 502,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
    },
  });
});
```

Deploy:

```bash
supabase secrets set RESEND_API_KEY=re_xxxxx
supabase functions deploy send-forge-mail --no-verify-jwt
```

Then call:

```js
await fetch('https://YOUR_PROJECT_REF.supabase.co/functions/v1/send-forge-mail', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
  },
  body: JSON.stringify({
    to: 'founder@example.com',
    subject: 'FORGE application received',
    html: '<p>We received your FORGE application.</p>',
  }),
});
```

Keep `SUPABASE_SERVICE_ROLE_KEY` and `RESEND_API_KEY` out of browser code.
