import base64
import hashlib
import hmac
import json
import os
import secrets
import smtplib
import sqlite3
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "forgeindia.sqlite3"
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "127.0.0.1")
CANONICAL_HOST = (os.environ.get("CANONICAL_HOST") or "forgeindia.site").strip()


def now():
    return int(time.time())


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def one(cur):
    r = cur.fetchone()
    return dict(r) if r else None


def uid(prefix):
    return f"{prefix}_{secrets.token_urlsafe(10).replace('-', '').replace('_', '')}"


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000)
    return salt, digest.hex()


def check_password(password, salt, digest):
    _, got = password_hash(password, salt)
    return hmac.compare_digest(got, digest)


def create_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
        (token, user_id, now()),
    )
    return token


def send_email(conn, to_email, subject, body):
    status = "queued"
    error = ""
    gmail_user = (os.environ.get("GMAIL_USER") or "").strip()
    gmail_pass = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    sender_name = (os.environ.get("MAIL_FROM_NAME") or "FORGE India").strip()
    resend_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    resend_from = (os.environ.get("MAIL_FROM_EMAIL") or "").strip()
    if resend_key and resend_from:
        try:
            payload = json.dumps(
                {
                    "from": resend_from,
                    "to": [to_email],
                    "subject": subject,
                    "text": body,
                }
            ).encode()
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "forge-india/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as res:
                res.read()
            status = "sent"
        except urllib.error.HTTPError as exc:
            status = "failed"
            details = exc.read().decode(errors="ignore")
            error = f"Resend HTTP {exc.code}: {details}"[:500]
            print(f"MAIL_FAILED to={to_email} provider=resend subject={subject!r} error={error}", flush=True)
        except Exception as exc:
            status = "failed"
            error = f"Resend error: {str(exc)}"[:500]
            print(f"MAIL_FAILED to={to_email} provider=resend subject={subject!r} error={error}", flush=True)
    elif resend_key and not resend_from:
        status = "failed"
        error = "RESEND_API_KEY is set but MAIL_FROM_EMAIL is missing"
        print(f"MAIL_FAILED to={to_email} provider=resend error={error}", flush=True)
    if status == "queued" and gmail_user and gmail_pass:
        try:
            msg = EmailMessage()
            msg["From"] = formataddr((sender_name, gmail_user))
            msg["Reply-To"] = gmail_user
            msg["To"] = to_email
            msg["Subject"] = subject
            msg.set_content(body)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
                smtp.login(gmail_user, gmail_pass)
                smtp.send_message(msg)
            status = "sent"
        except Exception as exc:
            status = "failed"
            error = str(exc)[:500]
            print(f"MAIL_FAILED to={to_email} subject={subject!r} error={error}", flush=True)
    elif status == "queued" and not resend_key:
        status = "demo_logged"
        missing = ",".join(k for k, v in {"GMAIL_USER": gmail_user, "GMAIL_APP_PASSWORD": gmail_pass}.items() if not v)
        print(f"MAIL_DEMO_LOGGED to={to_email} missing={missing}", flush=True)
    conn.execute(
        "INSERT INTO outbox(id,to_email,subject,body,status,error,created_at) VALUES(?,?,?,?,?,?,?)",
        (uid("mail"), to_email, subject, body, status, error, now()),
    )
    print(f"MAIL_STATUS to={to_email} status={status}", flush=True)
    return status


def supabase_insert(table, row):
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        return {"enabled": False, "status": "not_configured"}

    req = urllib.request.Request(
        f"{url}/rest/v1/{table}",
        data=json.dumps(row).encode(),
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        body = res.read().decode()
        return {
            "enabled": True,
            "status": res.status,
            "data": json.loads(body) if body else None,
        }


def notify(conn, user_id, title, body):
    conn.execute(
        "INSERT INTO notifications(id,user_id,title,body,is_read,created_at) VALUES(?,?,?,?,0,?)",
        (uid("noti"), user_id, title, body, now()),
    )


def user_score(conn, user_id):
    user = one(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)))
    if not user:
        return 0
    fields = ["city", "bio", "domain", "skills", "stage", "looking_for", "photo"]
    score = 20
    score += sum(8 for f in fields if user.get(f))
    apps = one(conn.execute("SELECT COUNT(*) c FROM applications WHERE user_id=?", (user_id,)))["c"]
    msgs = one(conn.execute("SELECT COUNT(*) c FROM messages WHERE user_id=?", (user_id,)))["c"]
    accepted = one(
        conn.execute(
            "SELECT COUNT(*) c FROM connections WHERE (from_user=? OR to_user=?) AND status='accepted'",
            (user_id, user_id),
        )
    )["c"]
    score += min(apps * 3, 18) + min(msgs, 14) + min(accepted * 6, 24)
    if user["premium"]:
        score += 8
    return min(score, 100)


def recalc_scores(conn):
    users = rows(conn.execute("SELECT id FROM users"))
    scored = []
    for u in users:
        score = user_score(conn, u["id"])
        scored.append((u["id"], score))
        conn.execute("UPDATE users SET score=? WHERE id=?", (score, u["id"]))
    scored.sort(key=lambda x: x[1], reverse=True)
    for idx, (user_id, _) in enumerate(scored, start=1):
        conn.execute("UPDATE users SET rank=? WHERE id=?", (idx, user_id))


def create_razorpay_order(amount_rupees, receipt):
    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        return {"mode": "demo", "key": "rzp_test_demo", "order_id": uid("demo_order")}

    payload = json.dumps(
        {"amount": int(amount_rupees * 100), "currency": "INR", "receipt": receipt}
    ).encode()
    auth = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/orders",
        data=payload,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        data = json.loads(res.read().decode())
    return {"mode": "live", "key": key_id, "order_id": data["id"]}


def verify_razorpay(order_id, payment_id, signature):
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_secret:
        return True
    msg = f"{order_id}|{payment_id}".encode()
    digest = hmac.new(key_secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature or "")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  salt TEXT NOT NULL,
  pass_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  city TEXT DEFAULT '',
  bio TEXT DEFAULT '',
  domain TEXT DEFAULT '',
  skills TEXT DEFAULT '',
  stage TEXT DEFAULT '',
  looking_for TEXT DEFAULT '',
  photo TEXT DEFAULT '',
  premium INTEGER NOT NULL DEFAULT 0,
  verified INTEGER NOT NULL DEFAULT 1,
  score INTEGER NOT NULL DEFAULT 20,
  rank INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS opportunities(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  org TEXT NOT NULL,
  type TEXT NOT NULL,
  summary TEXT NOT NULL,
  mode TEXT NOT NULL,
  location TEXT DEFAULT '',
  lat TEXT DEFAULT '',
  lng TEXT DEFAULT '',
  fee_type TEXT NOT NULL,
  fee_amount INTEGER NOT NULL DEFAULT 0,
  apply_mode TEXT NOT NULL,
  apply_url TEXT DEFAULT '',
  deadline TEXT DEFAULT '',
  reminder_at TEXT DEFAULT '',
  requirements TEXT DEFAULT '',
  premium_only INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS applications(
  id TEXT PRIMARY KEY,
  opp_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  status TEXT NOT NULL,
  form_json TEXT NOT NULL,
  external_registered INTEGER NOT NULL DEFAULT 0,
  payment_id TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  UNIQUE(opp_id,user_id)
);
CREATE TABLE IF NOT EXISTS payments(
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  purpose TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  amount INTEGER NOT NULL,
  status TEXT NOT NULL,
  razorpay_order_id TEXT DEFAULT '',
  provider_payment_id TEXT DEFAULT '',
  signature TEXT DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  topic TEXT NOT NULL,
  premium_only INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS room_members(room_id TEXT NOT NULL,user_id TEXT NOT NULL,created_at INTEGER NOT NULL,UNIQUE(room_id,user_id));
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS connections(
  id TEXT PRIMARY KEY,
  from_user TEXT NOT NULL,
  to_user TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(from_user,to_user)
);
CREATE TABLE IF NOT EXISTS swipes(
  from_user TEXT NOT NULL,
  to_user TEXT NOT NULL,
  action TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(from_user,to_user)
);
CREATE TABLE IF NOT EXISTS notifications(
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  is_read INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox(
  id TEXT PRIMARY KEY,
  to_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS landing_applications(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  city TEXT NOT NULL,
  email TEXT NOT NULL,
  instagram TEXT DEFAULT '',
  idea TEXT NOT NULL,
  primary_skill TEXT NOT NULL,
  looking_for TEXT DEFAULT '',
  source TEXT DEFAULT 'landing',
  supabase_status TEXT DEFAULT '',
  created_at INTEGER NOT NULL
);
"""


def seed():
    with db() as conn:
        conn.executescript(SCHEMA)
        if one(conn.execute("SELECT COUNT(*) c FROM users"))["c"] == 0:
            for u in [
                ("admin1", "Tenzin Wangchu", "admin@forgeindia.site", "forge2026", "admin", "Chennai", "FORGE India founder. Helping students and early founders execute faster.", "Community,SaaS", "Product,Ops,AI,Partnerships", "Revenue", "Founders to support"),
                ("u1", "Arjun Mehta", "arjun@demo.com", "demo123", "user", "Bangalore", "Building crop disease detection for Indian farmers.", "AgriTech,AI/ML", "Python,React,Computer vision", "MVP built", "Business co-founder"),
                ("u2", "Priya Sharma", "priya@demo.com", "demo123", "user", "Mumbai", "Ex-consultant building finance tools for MSMEs.", "FinTech,SaaS", "GTM,Fundraising,Strategy", "Revenue", "Technical co-founder"),
                ("u3", "Meera Krishnan", "meera@demo.com", "demo123", "user", "Bangalore", "Product builder focused on social commerce.", "Consumer,E-commerce", "Product,Figma,Research", "Validating", "Engineering co-founder"),
            ]:
                salt, ph = password_hash(u[3])
                conn.execute(
                    """INSERT INTO users(id,name,email,salt,pass_hash,role,city,bio,domain,skills,stage,looking_for,photo,premium,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (u[0], u[1], u[2], salt, ph, u[4], u[5], u[6], u[7], u[8], u[9], u[10], "", 1 if u[4] == "admin" else 0, now()),
                )
        if one(conn.execute("SELECT COUNT(*) c FROM rooms"))["c"] == 0:
            for r in [
                ("room_execution", "Startup Execution", "Daily execution, GTM, sales and accountability.", 0),
                ("room_cofounder", "Co-founder Matchmaking", "Warm intros, requests and founder feedback.", 0),
                ("room_hackathons", "Hackathons & Grants", "Apply smarter, build teams and track deadlines.", 0),
                ("room_premium", "FORGE Pro War Room", "24/7 founder support, pitch reviews and competition strategy.", 1),
            ]:
                conn.execute("INSERT INTO rooms(id,name,topic,premium_only) VALUES(?,?,?,?)", r)
        if one(conn.execute("SELECT COUNT(*) c FROM opportunities"))["c"] == 0:
            opps = [
                ("opp_1", "FORGE Founder Meetup - May 2026", "FORGE India", "event", "Rapid intros, co-founder matching and startup execution support.", "online", "Google Meet", "", "", "free", 0, "direct", "", "2026-05-20", "2026-05-18", "Active FORGE account", 0, "admin1"),
                ("opp_2", "Student Startup Hack Sprint", "FORGE India", "hackathon", "48-hour build sprint with mentor feedback, demo day and hiring intros.", "offline", "Chennai", "13.0827", "80.2707", "paid", 99, "direct", "", "2026-05-25", "2026-05-22", "Team or solo, prototype idea required", 0, "admin1"),
                ("opp_3", "Founding Engineer Internship", "Razorpay", "internship", "Track and apply to the official internship opening. FORGE stores your application proof.", "offline", "Bangalore", "12.9716", "77.5946", "free", 0, "external", "https://razorpay.com/jobs", "2026-06-02", "2026-05-30", "Strong CS fundamentals", 0, "admin1"),
            ]
            conn.executemany(
                """INSERT INTO opportunities(id,title,org,type,summary,mode,location,lat,lng,fee_type,fee_amount,apply_mode,apply_url,deadline,reminder_at,requirements,premium_only,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(o + (now(),)) for o in opps],
            )
        conn.commit()
        recalc_scores(conn)


class App(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def respond(self, status, data=None, headers=None):
        payload = json.dumps(data or {}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", os.environ.get("CORS_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", os.environ.get("CORS_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def redirect_render_host(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if CANONICAL_HOST and host.endswith(".onrender.com"):
            self.send_response(308)
            self.send_header("Location", f"https://{CANONICAL_HOST}{self.path}")
            self.end_headers()
            return True
        return False

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def current_user(self, conn):
        auth = self.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "", 1).strip()
        if not token:
            return None
        return one(
            conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
                (token,),
            )
        )

    def require_user(self, conn):
        user = self.current_user(conn)
        if not user:
            self.respond(401, {"error": "Login required"})
            return None
        return user

    def do_GET(self):
        if self.redirect_render_host():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html" or path == "/landing.html":
            page = ROOT / ("index.html" if path == "/index.html" and (ROOT / "index.html").exists() else "landing.html")
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/"):
            with db() as conn:
                user = self.current_user(conn)
                if path == "/api/bootstrap":
                    self.respond(200, self.bootstrap(conn, user))
                    return
                if path == "/api/me":
                    if not user:
                        self.respond(401, {"error": "Login required"})
                    else:
                        self.respond(200, {"user": self.public_user(user, True), **self.bootstrap(conn, user)})
                    return
                if path == "/api/admin":
                    if not user or user["role"] != "admin":
                        self.respond(403, {"error": "Admin only"})
                        return
                    recalc_scores(conn)
                    conn.commit()
                    self.respond(
                        200,
                        {
                            "users": [self.public_user(u, True) for u in rows(conn.execute("SELECT * FROM users ORDER BY rank ASC"))],
                            "applications": rows(conn.execute("""SELECT a.*,o.title,u.name user_name,u.email FROM applications a JOIN opportunities o ON o.id=a.opp_id JOIN users u ON u.id=a.user_id ORDER BY a.created_at DESC""")),
                            "landing_applications": rows(conn.execute("SELECT * FROM landing_applications ORDER BY created_at DESC LIMIT 100")),
                            "outbox": rows(conn.execute("SELECT * FROM outbox ORDER BY created_at DESC LIMIT 50")),
                        },
                    )
                    return
                if path.startswith("/api/rooms/") and path.endswith("/messages"):
                    if not user:
                        self.respond(401, {"error": "Login required"})
                        return
                    room_id = path.split("/")[3]
                    msgs = rows(
                        conn.execute(
                            """SELECT m.*,u.name,u.photo,u.premium FROM messages m JOIN users u ON u.id=m.user_id
                               WHERE room_id=? ORDER BY m.created_at ASC LIMIT 200""",
                            (room_id,),
                        )
                    )
                    self.respond(200, {"messages": msgs})
                    return
                if path == "/api/founders":
                    if not user:
                        self.respond(401, {"error": "Login required"})
                        return
                    founders = []
                    for f in rows(conn.execute("SELECT * FROM users WHERE id<>? AND role<>'admin' ORDER BY score DESC", (user["id"],))):
                        rel = one(conn.execute("SELECT * FROM connections WHERE (from_user=? AND to_user=?) OR (from_user=? AND to_user=?)", (user["id"], f["id"], f["id"], user["id"])))
                        swipe = one(conn.execute("SELECT action FROM swipes WHERE from_user=? AND to_user=?", (user["id"], f["id"])))
                        item = self.public_user(f, True)
                        item["matchScore"] = self.match_score(user, f)
                        item["connectionStatus"] = rel["status"] if rel else ""
                        item["swipe"] = swipe["action"] if swipe else ""
                        founders.append(item)
                    self.respond(200, {"founders": founders})
                    return
                if path == "/api/network":
                    if not user:
                        self.respond(401, {"error": "Login required"})
                        return
                    conns = rows(
                        conn.execute(
                            """SELECT c.*,fu.name from_name,tu.name to_name,fu.photo from_photo,tu.photo to_photo
                               FROM connections c JOIN users fu ON fu.id=c.from_user JOIN users tu ON tu.id=c.to_user
                               WHERE c.from_user=? OR c.to_user=? ORDER BY c.created_at DESC""",
                            (user["id"], user["id"]),
                        )
                    )
                    self.respond(200, {"connections": conns})
                    return
        self.send_error(404)

    def do_POST(self):
        if self.redirect_render_host():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            data = self.read_json()
        except Exception:
            self.respond(400, {"error": "Invalid JSON"})
            return
        with db() as conn:
            try:
                if path == "/api/landing/apply":
                    required = ["name", "city", "email", "idea", "primary_skill"]
                    clean = {k: str(data.get(k, "")).strip() for k in required}
                    email = clean["email"].lower()
                    if any(not clean[k] for k in required) or "@" not in email:
                        self.respond(400, {"error": "Name, city, valid email, idea and skill are required"})
                        return
                    app_id = uid("lead")
                    row = {
                        "id": app_id,
                        "name": clean["name"],
                        "city": clean["city"],
                        "email": email,
                        "instagram": str(data.get("instagram", "")).strip(),
                        "idea": clean["idea"],
                        "primary_skill": clean["primary_skill"],
                        "looking_for": str(data.get("looking_for", "")).strip(),
                        "source": str(data.get("source", "landing")).strip() or "landing",
                        "created_at": now(),
                    }
                    supabase_status = "not_configured"
                    try:
                        supabase_status = str(supabase_insert("landing_applications", row).get("status"))
                    except Exception as exc:
                        supabase_status = f"failed:{str(exc)[:160]}"
                    conn.execute(
                        """INSERT INTO landing_applications(id,name,city,email,instagram,idea,primary_skill,looking_for,source,supabase_status,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            row["id"],
                            row["name"],
                            row["city"],
                            row["email"],
                            row["instagram"],
                            row["idea"],
                            row["primary_skill"],
                            row["looking_for"],
                            row["source"],
                            supabase_status,
                            row["created_at"],
                        ),
                    )
                    mail_status = send_email(
                        conn,
                        email,
                        "Welcome to FORGE India!",
                        f"""Hi {row['name']},

Welcome to FORGE India!

We're excited that you're building something of your own, and honored that you've chosen to join the FORGE India founder community.

You now have access to a growing network of early-stage founders, builders, operators, and startup resources built for people who are serious about turning ideas into real companies.

FORGE India is designed for founders who are actively working on a startup, side project, MVP, or early idea. Whenever you need support in your founder journey, this community is here to help you move faster.

FORGE India offers you:

- Founder community and peer support
- Co-founder discovery and introductions
- Startup events, hackathons, and opportunities
- Practical guidance for building, validating, and launching
- Access to resources for early-stage founders
- A place to share progress, ask questions, and find collaborators

We're glad to have you here.

Build boldly,
FORGE India
""",
                    )
                    admin_email = (os.environ.get("ADMIN_NOTIFY_EMAIL") or "").strip()
                    if admin_email and admin_email.lower() != email:
                        send_email(
                            conn,
                            admin_email,
                            f"New FORGE application: {row['name']}",
                            f"{row['name']} ({email}) from {row['city']} is building: {row['idea']}",
                        )
                    conn.commit()
                    self.respond(200, {"ok": True, "id": app_id, "mail_status": mail_status, "supabase_status": supabase_status})
                    return

                if path == "/api/signup":
                    name = (data.get("name") or "").strip()
                    email = (data.get("email") or "").strip().lower()
                    password = data.get("password") or ""
                    photo = data.get("photo") or ""
                    if not name or not email or len(password) < 6:
                        self.respond(400, {"error": "Name, valid email and 6+ char password required"})
                        return
                    if not photo.startswith("data:image/") or len(photo) < 1200:
                        self.respond(400, {"error": "A clear founder face photo is required"})
                        return
                    if one(conn.execute("SELECT id FROM users WHERE email=?", (email,))):
                        self.respond(409, {"error": "Email already registered"})
                        return
                    salt, ph = password_hash(password)
                    user_id = uid("user")
                    conn.execute(
                        """INSERT INTO users(id,name,email,salt,pass_hash,role,city,bio,domain,skills,stage,looking_for,photo,created_at)
                           VALUES(?,?,?,?,?,'user',?,?,?,?,?,?,?,?)""",
                        (user_id, name, email, salt, ph, data.get("city", ""), data.get("bio", ""), data.get("domain", ""), data.get("skills", ""), data.get("stage", ""), data.get("looking_for", ""), photo, now()),
                    )
                    notify(conn, user_id, "Welcome to FORGE India", "Complete your profile, join a room, and request your first co-founder intro.")
                    send_email(conn, email, "Welcome to FORGE India", f"Hi {name}, your FORGE India account is ready.")
                    recalc_scores(conn)
                    token = create_session(conn, user_id)
                    conn.commit()
                    user = one(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)))
                    self.respond(200, {"token": token, "user": self.public_user(user, True)})
                    return
                if path == "/api/login":
                    email = (data.get("email") or "").strip().lower()
                    password = data.get("password") or ""
                    user = one(conn.execute("SELECT * FROM users WHERE email=?", (email,)))
                    if not user or not check_password(password, user["salt"], user["pass_hash"]):
                        self.respond(401, {"error": "Invalid email or password"})
                        return
                    token = create_session(conn, user["id"])
                    conn.commit()
                    self.respond(200, {"token": token, "user": self.public_user(user, True)})
                    return

                user = self.require_user(conn)
                if not user:
                    return

                if path == "/api/profile":
                    allowed = ["name", "city", "bio", "domain", "skills", "stage", "looking_for", "photo"]
                    updates = {k: str(data.get(k, "")) for k in allowed if k in data}
                    if "photo" in updates and (not updates["photo"].startswith("data:image/") or len(updates["photo"]) < 1200):
                        self.respond(400, {"error": "Upload a clear face photo"})
                        return
                    if updates:
                        sets = ",".join([f"{k}=?" for k in updates])
                        conn.execute(f"UPDATE users SET {sets} WHERE id=?", (*updates.values(), user["id"]))
                    recalc_scores(conn)
                    conn.commit()
                    fresh = one(conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)))
                    self.respond(200, {"user": self.public_user(fresh, True)})
                    return

                if path == "/api/opportunities":
                    if user["role"] != "admin":
                        self.respond(403, {"error": "Admin only"})
                        return
                    opp_id = uid("opp")
                    fee_amount = int(data.get("fee_amount") or 0)
                    conn.execute(
                        """INSERT INTO opportunities(id,title,org,type,summary,mode,location,lat,lng,fee_type,fee_amount,apply_mode,apply_url,deadline,reminder_at,requirements,premium_only,created_by,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            opp_id,
                            data.get("title", "").strip(),
                            data.get("org", "").strip(),
                            data.get("type", "event"),
                            data.get("summary", "").strip(),
                            data.get("mode", "online"),
                            data.get("location", ""),
                            data.get("lat", ""),
                            data.get("lng", ""),
                            data.get("fee_type", "free"),
                            fee_amount,
                            data.get("apply_mode", "direct"),
                            data.get("apply_url", ""),
                            data.get("deadline", ""),
                            data.get("reminder_at", ""),
                            data.get("requirements", ""),
                            1 if data.get("premium_only") else 0,
                            user["id"],
                            now(),
                        ),
                    )
                    conn.commit()
                    self.respond(200, {"opportunity": one(conn.execute("SELECT * FROM opportunities WHERE id=?", (opp_id,)))})
                    return

                if path == "/api/applications":
                    opp = one(conn.execute("SELECT * FROM opportunities WHERE id=?", (data.get("opp_id"),)))
                    if not opp:
                        self.respond(404, {"error": "Opportunity not found"})
                        return
                    if opp["premium_only"] and not user["premium"]:
                        self.respond(402, {"error": "FORGE Pro required"})
                        return
                    form = data.get("form") or {}
                    app_id = uid("app")
                    status = "submitted"
                    payment_id = ""
                    external_registered = 1 if data.get("external_registered") else 0
                    if opp["apply_mode"] == "external" and not external_registered:
                        status = "external_pending"
                    elif opp["fee_type"] == "paid" and opp["fee_amount"] > 0:
                        status = "payment_pending"
                        payment_id, order = self.create_payment(conn, user["id"], "application", app_id, opp["fee_amount"])
                    else:
                        order = None
                    try:
                        conn.execute(
                            "INSERT INTO applications(id,opp_id,user_id,status,form_json,external_registered,payment_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (app_id, opp["id"], user["id"], status, json.dumps(form), external_registered, payment_id, now()),
                        )
                    except sqlite3.IntegrityError:
                        self.respond(409, {"error": "You already applied or registered"})
                        return
                    if status == "submitted":
                        notify(conn, user["id"], "Application submitted", f"Your application for {opp['title']} is stored.")
                        send_email(conn, user["email"], f"Application confirmed: {opp['title']}", f"Hi {user['name']}, your application is confirmed on FORGE India.")
                    recalc_scores(conn)
                    conn.commit()
                    self.respond(200, {"application": one(conn.execute("SELECT * FROM applications WHERE id=?", (app_id,))), "payment": order if status == "payment_pending" else None, "external_url": opp["apply_url"]})
                    return

                if path == "/api/payments/premium":
                    payment_id, order = self.create_payment(conn, user["id"], "premium", user["id"], 49)
                    conn.commit()
                    self.respond(200, {"payment_id": payment_id, "payment": order})
                    return

                if path == "/api/payments/confirm":
                    payment = one(conn.execute("SELECT * FROM payments WHERE id=?", (data.get("payment_id"),)))
                    if not payment or payment["user_id"] != user["id"]:
                        self.respond(404, {"error": "Payment not found"})
                        return
                    ok = verify_razorpay(payment["razorpay_order_id"], data.get("razorpay_payment_id", "demo_payment"), data.get("razorpay_signature", ""))
                    if not ok:
                        self.respond(400, {"error": "Payment signature failed"})
                        return
                    provider_payment_id = data.get("razorpay_payment_id") or uid("demo_pay")
                    conn.execute("UPDATE payments SET status='paid',provider_payment_id=?,signature=? WHERE id=?", (provider_payment_id, data.get("razorpay_signature", ""), payment["id"]))
                    if payment["purpose"] == "premium":
                        conn.execute("UPDATE users SET premium=1 WHERE id=?", (user["id"],))
                        notify(conn, user["id"], "FORGE Pro activated", "Your Rs. 49 FORGE Pro access is live.")
                        send_email(conn, user["email"], "FORGE Pro activated", f"Hi {user['name']}, your FORGE Pro membership is active.")
                    if payment["purpose"] == "application":
                        conn.execute("UPDATE applications SET status='submitted',payment_id=? WHERE id=?", (payment["id"], payment["ref_id"]))
                        app = one(conn.execute("SELECT a.*,o.title FROM applications a JOIN opportunities o ON o.id=a.opp_id WHERE a.id=?", (payment["ref_id"],)))
                        notify(conn, user["id"], "Paid application confirmed", f"Payment received for {app['title']}.")
                        send_email(conn, user["email"], f"Paid application confirmed: {app['title']}", f"Hi {user['name']}, payment and application are confirmed.")
                    recalc_scores(conn)
                    conn.commit()
                    fresh = one(conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)))
                    self.respond(200, {"ok": True, "user": self.public_user(fresh, True)})
                    return

                if path.startswith("/api/rooms/") and path.endswith("/join"):
                    room_id = path.split("/")[3]
                    room = one(conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)))
                    if not room:
                        self.respond(404, {"error": "Room not found"})
                        return
                    if room["premium_only"] and not user["premium"]:
                        self.respond(402, {"error": "FORGE Pro required"})
                        return
                    conn.execute("INSERT OR IGNORE INTO room_members(room_id,user_id,created_at) VALUES(?,?,?)", (room_id, user["id"], now()))
                    conn.commit()
                    self.respond(200, {"ok": True})
                    return

                if path.startswith("/api/rooms/") and path.endswith("/messages"):
                    room_id = path.split("/")[3]
                    body = (data.get("body") or "").strip()
                    if not body:
                        self.respond(400, {"error": "Message required"})
                        return
                    room = one(conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)))
                    if room["premium_only"] and not user["premium"]:
                        self.respond(402, {"error": "FORGE Pro required"})
                        return
                    conn.execute("INSERT OR IGNORE INTO room_members(room_id,user_id,created_at) VALUES(?,?,?)", (room_id, user["id"], now()))
                    conn.execute("INSERT INTO messages(id,room_id,user_id,body,created_at) VALUES(?,?,?,?,?)", (uid("msg"), room_id, user["id"], body, now()))
                    recalc_scores(conn)
                    conn.commit()
                    self.respond(200, {"ok": True})
                    return

                if path == "/api/match":
                    to_user = data.get("to_user")
                    action = data.get("action")
                    if action not in ("request", "skip"):
                        self.respond(400, {"error": "Invalid action"})
                        return
                    target = one(conn.execute("SELECT * FROM users WHERE id=? AND id<>?", (to_user, user["id"])))
                    if not target:
                        self.respond(404, {"error": "Founder not found"})
                        return
                    conn.execute("INSERT OR REPLACE INTO swipes(from_user,to_user,action,created_at) VALUES(?,?,?,?)", (user["id"], to_user, action, now()))
                    if action == "request":
                        try:
                            conn.execute("INSERT INTO connections(id,from_user,to_user,status,created_at) VALUES(?,?,?,?,?)", (uid("conn"), user["id"], to_user, "pending", now()))
                        except sqlite3.IntegrityError:
                            pass
                        notify(conn, to_user, "New co-founder request", f"{user['name']} requested to connect with you.")
                    conn.commit()
                    self.respond(200, {"ok": True})
                    return

                if path.startswith("/api/connections/"):
                    conn_id = path.split("/")[3]
                    action = data.get("action")
                    if action not in ("accepted", "rejected"):
                        self.respond(400, {"error": "Invalid action"})
                        return
                    c = one(conn.execute("SELECT * FROM connections WHERE id=?", (conn_id,)))
                    if not c or c["to_user"] != user["id"]:
                        self.respond(404, {"error": "Request not found"})
                        return
                    conn.execute("UPDATE connections SET status=? WHERE id=?", (action, conn_id))
                    notify(conn, c["from_user"], "Co-founder request update", f"{user['name']} {action} your request.")
                    recalc_scores(conn)
                    conn.commit()
                    self.respond(200, {"ok": True})
                    return

                if path == "/api/advisor":
                    msg = (data.get("message") or "").strip()
                    self.respond(200, {"reply": self.advisor_reply(user, msg)})
                    return
            except urllib.error.URLError as exc:
                self.respond(502, {"error": f"External API failed: {exc}"})
                return
            except Exception as exc:
                self.respond(500, {"error": str(exc)})
                return
        self.send_error(404)

    def create_payment(self, conn, user_id, purpose, ref_id, amount):
        payment_id = uid("pay")
        order = create_razorpay_order(int(amount), payment_id)
        conn.execute(
            "INSERT INTO payments(id,user_id,purpose,ref_id,amount,status,razorpay_order_id,created_at) VALUES(?,?,?,?,?,'created',?,?)",
            (payment_id, user_id, purpose, ref_id, int(amount), order["order_id"], now()),
        )
        order["amount"] = int(amount)
        order["payment_id"] = payment_id
        return payment_id, order

    def public_user(self, u, full=False):
        data = {
            "id": u["id"],
            "name": u["name"],
            "email": u["email"] if full else "",
            "role": u["role"],
            "city": u["city"],
            "bio": u["bio"],
            "domain": u["domain"],
            "skills": u["skills"],
            "stage": u["stage"],
            "looking_for": u["looking_for"],
            "photo": u["photo"],
            "premium": bool(u["premium"]),
            "score": u["score"],
            "rank": u["rank"],
        }
        return data

    def bootstrap(self, conn, user=None):
        recalc_scores(conn)
        stats = {
            "founders": one(conn.execute("SELECT COUNT(*) c FROM users WHERE role='user'"))["c"],
            "communities": one(conn.execute("SELECT COUNT(*) c FROM rooms"))["c"],
            "opportunities": one(conn.execute("SELECT COUNT(*) c FROM opportunities"))["c"],
            "applications": one(conn.execute("SELECT COUNT(*) c FROM applications"))["c"],
        }
        opps = rows(conn.execute("SELECT * FROM opportunities ORDER BY created_at DESC"))
        rooms = rows(
            conn.execute(
                """SELECT r.*,COUNT(m.user_id) members FROM rooms r
                   LEFT JOIN room_members m ON m.room_id=r.id GROUP BY r.id ORDER BY premium_only ASC,name ASC"""
            )
        )
        notifications = []
        if user:
            notifications = rows(conn.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (user["id"],)))
        return {"stats": stats, "opportunities": opps, "rooms": rooms, "notifications": notifications}

    def match_score(self, user, founder):
        mine = set((user["domain"] + "," + user["skills"]).lower().replace(" ", "").split(","))
        theirs = set((founder["domain"] + "," + founder["skills"]).lower().replace(" ", "").split(","))
        mine.discard("")
        theirs.discard("")
        overlap = len(mine & theirs)
        looking_bonus = 12 if user["looking_for"] and user["looking_for"].split(" ")[0].lower() in founder["skills"].lower() else 0
        city_bonus = 8 if user["city"] and user["city"].lower() == founder["city"].lower() else 0
        return min(62 + overlap * 7 + looking_bonus + city_bonus, 98)

    def advisor_reply(self, user, msg):
        name = user["name"].split(" ")[0]
        lower = msg.lower()
        if "premium" in lower or "pro" in lower:
            return f"{name}, FORGE Pro is best used for weekly execution review, pitch feedback, competition targeting, and founder support. Start with one clear goal for the next 7 days."
        if "cofounder" in lower or "co-founder" in lower:
            return f"{name}, request 3 founders who complement your weakest skill, not people identical to you. Send a specific ask: what you are building, traction, and what decision you need help with this week."
        if "hackathon" in lower or "event" in lower or "internship" in lower:
            return f"{name}, apply where your current startup can produce a demo in 48 hours. Use FORGE reminders, store the application, and write a one-line outcome after you apply."
        if "idea" in lower or "startup" in lower:
            return f"{name}, validate demand before building more. Talk to 10 target users, collect one painful quote, and convert it into a landing page promise."
        return f"{name}, here is your execution move: pick one measurable outcome for today, post progress in a community room, and request a founder intro only after your profile score is above 75."


if __name__ == "__main__":
    seed()
    print(f"FORGE India running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), App).serve_forever()
