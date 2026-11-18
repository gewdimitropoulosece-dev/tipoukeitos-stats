#!/usr/bin/env python3
"""Ενημερώνει τα στατιστικά του Τιπούκειτου (ΜΟΝΟ αριθμούς) στο αρχείο #311 του site.

Διαβάζει από το API του BookStack πότε ήταν ενεργός τελευταία φορά κάθε χρήστης
και αποθηκεύει: μέλη, ενεργούς την τελευταία ώρα, ενεργούς σήμερα και εβδομάδα.
Ονόματα και email δεν αποθηκεύονται και δεν τυπώνονται πουθενά.
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

# Αντικατάσταση του αρχείου #311 (multipart με _method=PUT, όπως ορίζει το API)
b = uuid.uuid4().hex
def field(name, value):
    return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, name, value)).encode("utf-8")
body = (field("_method", "PUT") + field("uploaded_to", str(PAGE_ID)) + field("name", "tk-stats.json")
        + ('--%s\r\nContent-Disposition: form-data; name="file"; filename="tk-stats.json"\r\n'
           'Content-Type: application/json\r\n\r\n' % b).encode("utf-8")
        + json.dumps(stats).encode("utf-8") + ("\r\n--%s--\r\n" % b).encode("utf-8"))
api("/api/attachments/%d" % ATTACHMENT_ID, data=body, content_type="multipart/form-data; boundary=" + b)
print("OK", {k: stats[k] for k in ("members", "online", "today", "week")})
