#!/usr/bin/env python3
"""Ευρετήριο κειμένου των PDF του Τιπούκειτου, για αναζήτηση μέσα στις σημειώσεις.

- Βρίσκει όλα τα PDF που έχουν ανέβει ως συνημμένα (όχι εξωτερικούς συνδέσμους).
- Κρατά ΜΟΝΟ όσα βλέπουν ΚΑΙ οι τρεις ρόλοι φοιτητών (Viewer, Viewer_2, Viewer_3):
  ό,τι είναι κλειδωμένο για κάποιον από αυτούς δεν μπαίνει ποτέ στο ευρετήριο.
- Βγάζει το κείμενο ανά σελίδα με το pdftotext (poppler). Τα σκαναρισμένα PDF
  (φωτογραφίες σελίδων) δεν έχουν κείμενο και παραλείπονται.
- Ανεβάζει το ευρετήριο (json.gz) στο αρχείο #312 της σελίδας «Σύστημα → Ευρετήριο».
"""
import base64
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

BASE = os.environ.get("BS_URL", "https://tipoukeitostheo.gr").rstrip("/")
AUTH = "Token %s:%s" % (os.environ["BS_TOKEN_ID"], os.environ["BS_TOKEN_SECRET"])
INDEX_ATTACHMENT = 312
PAGE_ID = 147      # σελίδα «Ευρετήριο» (μόνο ρόλοι φοιτητών)
STUDENT_ROLES = (3, 5, 6)      # Viewer, Viewer_2, Viewer_3
SKIP_BOOKS = ("systima", "sizitiseis")


def api(path, data=None, content_type=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(BASE + path, data=data, headers={
                "Authorization": AUTH, "Accept": "application/json", "User-Agent": "tipoukeitos-index"})
            if content_type:
                req.add_header("Content-Type", content_type)
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def listing(path):
    out, offset = [], 0
    while True:
        sep = "&" if "?" in path else "?"
        page = api("%s%scount=500&offset=%d" % (path, sep, offset))
        out += page.get("data", [])
        offset += 500
        if offset >= page.get("total", 0):
            return out


# ---------- Δικαιώματα: βλέπουν το στοιχείο όλοι οι ρόλοι φοιτητών; ----------
_perm_cache = {}
def entity_perms(kind, eid):
    key = (kind, eid)
    if key not in _perm_cache:
        _perm_cache[key] = api("/api/content-permissions/%s/%d" % (kind, eid))
    return _perm_cache[key]


_role_default = {}
def role_can_view_by_default(role_id):
    if role_id not in _role_default:
        r = api("/api/roles/%d" % role_id)
        perms = set(r.get("permissions") or [])
        _role_default[role_id] = "page-view-all" in perms
    return _role_default[role_id]


def can_view(role_id, chain):
    """chain: [(kind, id), ...] από τη σελίδα προς τα πάνω (σελίδα, κεφάλαιο, βιβλίο)."""
    for kind, eid in chain:
        p = entity_perms(kind, eid)
        for rp in p.get("role_permissions") or []:
            if rp.get("role_id") == role_id:
                return bool(rp.get("view"))
        fb = p.get("fallback_permissions") or {}
        if fb.get("inheriting") is False:
            return bool(fb.get("view"))
    return role_can_view_by_default(role_id)


def pdf_pages(data):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        res = subprocess.run(["pdftotext", "-enc", "UTF-8", "-q", f.name, "-"],
                             capture_output=True, timeout=300)
    text = res.stdout.decode("utf-8", "replace")
    return [" ".join(p.split()) for p in text.split("\f")]


def main():
    started = time.time()
    attachments = [a for a in listing("/api/attachments")
                   if not a.get("external") and (a.get("extension") or "").lower() == "pdf"]
    pages, books = {}, {}
    docs, rows = [], []
    skipped_locked = skipped_scanned = failed = 0
    for a in attachments:
        pid = a.get("uploaded_to")
        if not pid:
            continue
        try:
            if pid not in pages:
                pages[pid] = api("/api/pages/%d" % pid)
            pg = pages[pid]
            book_slug = pg.get("book_slug") or ""
            if book_slug in SKIP_BOOKS or pg.get("draft"):
                continue
            chain = [("page", pid)]
            if pg.get("chapter_id"):
                chain.append(("chapter", pg["chapter_id"]))
            chain.append(("book", pg["book_id"]))
            if not all(can_view(r, chain) for r in STUDENT_ROLES):
                skipped_locked += 1
                continue
            if pg["book_id"] not in books:
                books[pg["book_id"]] = api("/api/books/%d" % pg["book_id"]).get("name", "")
            full = api("/api/attachments/%d" % a["id"])
            data = base64.b64decode(full.get("content") or "")
            texts = pdf_pages(data)
            useful = [(i + 1, t) for i, t in enumerate(texts) if len(t) >= 30]
            if not useful:
                skipped_scanned += 1
                continue
            di = len(docs)
            docs.append({"a": a["id"], "n": a.get("name", ""),
                         "u": "/books/%s/page/%s" % (book_slug, pg.get("slug", "")),
                         "c": books[pg["book_id"]]})
            rows += [[di, n, t] for n, t in useful]
        except Exception as e:  # ένα χαλασμένο αρχείο δεν σταματά όλη τη δουλειά
            failed += 1
            print("WARN attachment", a.get("id"), type(e).__name__, file=sys.stderr)

    blob = gzip.compress(json.dumps({"v": 1, "t": int(time.time() * 1000), "d": docs, "p": rows},
                                    ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
    b = uuid.uuid4().hex
    def field(name, value):
        return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, name, value)).encode("utf-8")
    body = (field("_method", "PUT") + field("uploaded_to", str(PAGE_ID)) + field("name", "tk-pdfindex.json.gz")
            + ('--%s\r\nContent-Disposition: form-data; name="file"; filename="tk-pdfindex.json.gz"\r\n'
               'Content-Type: application/gzip\r\n\r\n' % b).encode("utf-8")
            + blob + ("\r\n--%s--\r\n" % b).encode("utf-8"))
    api("/api/attachments/%d" % INDEX_ATTACHMENT, data=body, content_type="multipart/form-data; boundary=" + b)
    print("OK pdf:%d indexed:%d pages:%d locked:%d scanned:%d failed:%d size:%.1fMB time:%ds" % (
        len(attachments), len(docs), len(rows), skipped_locked, skipped_scanned, failed,
        len(blob) / 1048576, time.time() - started))


if __name__ == "__main__":
    main()
