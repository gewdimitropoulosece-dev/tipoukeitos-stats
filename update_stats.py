#!/usr/bin/env python3
"""Ενημερώνει τα στατιστικά του Τιπούκειτου (ΜΟΝΟ αριθμούς) στο αρχείο #311 του site.

Διαβάζει από το API του BookStack πότε ήταν ενεργός τελευταία φορά κάθε χρήστης
και αποθηκεύει: μέλη, ενεργούς την τελευταία ώρα, ενεργούς σήμερα και εβδομάδα.
Ονόματα και email δεν αποθηκεύονται και δεν τυπώνονται πουθενά.

Επιπλέον, για να φαίνεται αμέσως το νέο υλικό σε όλους:
- "c": πότε άλλαξε τελευταία φορά υλικό (σελίδα ή αρχείο) — οι browsers ανανεώνουν τότε τη λίστα τους·
- "fresh": σελίδες με νέο αρχείο ή νέες σελίδες των τελευταίων 14 ημερών (για την ένδειξη «νέο»),
  ΜΟΝΟ όσες βλέπουν όλοι οι ρόλοι φοιτητών.
"""
import json
import os
import time
import uuid
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = os.environ.get("BS_URL", "https://tipoukeitostheo.gr").rstrip("/")
AUTH = "Token %s:%s" % (os.environ["BS_TOKEN_ID"], os.environ["BS_TOKEN_SECRET"])
ATTACHMENT_ID = 311   # tk-stats.json
PAGE_ID = 131         # σελίδα «Κώδικας» στο βιβλίο «Σύστημα»
ONLINE_MINUTES = 60   # «ενεργοί την τελευταία ώρα»
FRESH_DAYS = 14       # «νέο» για 14 ημέρες
STUDENT_ROLES = (3, 5, 6)
SKIP_BOOKS = ("systima", "sizitiseis")


def api(path, data=None, content_type=None):
    req = urllib.request.Request(BASE + path, data=data, headers={
        "Authorization": AUTH, "Accept": "application/json", "User-Agent": "tipoukeitos-stats"})
    if content_type:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


_perm = {}
def can_view(role_id, chain, role_default):
    for kind, eid in chain:
        key = (kind, eid)
        if key not in _perm:
            _perm[key] = api("/api/content-permissions/%s/%d" % (kind, eid))
        p = _perm[key]
        for rp in p.get("role_permissions") or []:
            if rp.get("role_id") == role_id:
                return bool(rp.get("view"))
        fb = p.get("fallback_permissions") or {}
        if fb.get("inheriting") is False:
            return bool(fb.get("view"))
    return role_default[role_id]


def content_info(now):
    """Τελευταία αλλαγή υλικού και σελίδες με νέο υλικό (μόνο ορατές στους φοιτητές)."""
    role_default = {r: "page-view-all" in (api("/api/roles/%d" % r).get("permissions") or []) for r in STUDENT_ROLES}
    cutoff = now - timedelta(days=FRESH_DAYS)
    pages, latest, fresh = {}, None, []

    def page(pid):
        if pid not in pages:
            try:
                pages[pid] = api("/api/pages/%d" % pid)
            except Exception:
                pages[pid] = {}
        return pages[pid]

    def visible(pg):
        chain = [("page", pg["id"])]
        if pg.get("chapter_id"):
            chain.append(("chapter", pg["chapter_id"]))
        chain.append(("book", pg["book_id"]))
        return all(can_view(r, chain, role_default) for r in STUDENT_ROLES)

    def note(pg, ts, is_new):
        nonlocal latest
        if not pg or pg.get("draft") or (pg.get("book_slug") or "") in SKIP_BOOKS:
            return
        if ts and (latest is None or ts > latest):
            latest = ts
        if is_new and visible(pg):
            path = "/books/%s/page/%s" % (pg["book_slug"], pg["slug"])
            if path not in fresh:
                fresh.append(path)

    for pg in api("/api/pages?sort=-updated_at&count=100").get("data", []):
        pages.setdefault(pg["id"], pg)
        note(pg, parse(pg.get("updated_at")), (parse(pg.get("created_at")) or now) > cutoff)
    for at in api("/api/attachments?sort=-created_at&count=100").get("data", []):
        created = parse(at.get("created_at"))
        if at.get("uploaded_to"):
            note(page(at["uploaded_to"]), parse(at.get("updated_at")) or created, bool(created and created > cutoff))
    return (int(latest.timestamp() * 1000) if latest else 0), fresh[:80]


users, offset = [], 0
while True:
    page = api("/api/users?count=500&offset=%d" % offset)
    users += page.get("data", [])
    offset += 500
    if offset >= page.get("total", 0):
        break
# Ο εσωτερικός λογαριασμός «Guest» του BookStack δεν είναι μέλος
users = [u for u in users if (u.get("slug") or "") != "guest"]

now = datetime.now(timezone.utc)
midnight = datetime.now(ZoneInfo("Europe/Athens")).replace(hour=0, minute=0, second=0, microsecond=0)
acts = [parse(u.get("last_activity_at")) for u in users]
stats = {
    "members": len(users),
    "online": sum(1 for a in acts if a and a > now - timedelta(minutes=ONLINE_MINUTES)),
    "today": sum(1 for a in acts if a and a >= midnight),
    "week": sum(1 for a in acts if a and a > now - timedelta(days=7)),
    "t": int(time.time() * 1000),
}
try:
    stats["c"], stats["fresh"] = content_info(now)
except Exception as e:  # τα στατιστικά συνεχίζουν κι αν αποτύχει αυτό
    print("WARN content", type(e).__name__)

# Αντικατάσταση του αρχείου #311 (multipart με _method=PUT, όπως ορίζει το API)
b = uuid.uuid4().hex
def field(name, value):
    return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, name, value)).encode("utf-8")
body = (field("_method", "PUT") + field("uploaded_to", str(PAGE_ID)) + field("name", "tk-stats.json")
        + ('--%s\r\nContent-Disposition: form-data; name="file"; filename="tk-stats.json"\r\n'
           'Content-Type: application/json\r\n\r\n' % b).encode("utf-8")
        + json.dumps(stats).encode("utf-8") + ("\r\n--%s--\r\n" % b).encode("utf-8"))
api("/api/attachments/%d" % ATTACHMENT_ID, data=body, content_type="multipart/form-data; boundary=" + b)
print("OK", {k: stats[k] for k in ("members", "online", "today", "week")}, "fresh:", len(stats.get("fresh", [])))
