#!/usr/bin/env python3
"""Ευρετήριο κειμένου των PDF του Τιπούκειτου, για αναζήτηση μέσα στις σημειώσεις.

- Βρίσκει όλα τα PDF που έχουν ανέβει ως συνημμένα (όχι εξωτερικούς συνδέσμους).
- Κρατά ΜΟΝΟ όσα βλέπουν ΚΑΙ οι τρεις ρόλοι φοιτητών (Viewer, Viewer_2, Viewer_3):
  ό,τι είναι κλειδωμένο για κάποιον από αυτούς δεν μπαίνει ποτέ στο ευρετήριο.
- Βγάζει το κείμενο ανά σελίδα με το pdftotext (poppler). Τα σκαναρισμένα PDF
  (φωτογραφίες σελίδων) διαβάζονται με OCR (tesseract: νέα + πολυτονικά ελληνικά).
  Το OCR είναι αργό: το αποτέλεσμα ξαναχρησιμοποιείται από το προηγούμενο ευρετήριο
  όσο το PDF δεν αλλάζει, και κάθε βράδυ συνεχίζει από όπου σταμάτησε (όριο χρόνου).
- Ανεβάζει το ευρετήριο (json.gz) στο αρχείο #312 της σελίδας «Σύστημα → Ευρετήριο».
"""
import base64
import concurrent.futures
import glob
import html
import re
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("BS_URL", "https://tipoukeitostheo.gr").rstrip("/")
AUTH = "Token %s:%s" % (os.environ["BS_TOKEN_ID"], os.environ["BS_TOKEN_SECRET"])
INDEX_ATTACHMENT = 312
PAGE_ID = 147      # σελίδα «Ευρετήριο» (μόνο ρόλοι φοιτητών)
STUDENT_ROLES = (3, 5, 6)      # Viewer, Viewer_2, Viewer_3
SKIP_BOOKS = ("systima", "sizitiseis")
OCR_LANGS = "ell+grc+eng"
OCR_MINUTES = float(os.environ.get("OCR_MINUTES", "80"))   # χρόνος OCR ανά εκτέλεση
OCR_CHUNK = 24                                            # σελίδες ανά γύρο


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


def pdf_page_count(path):
    res = subprocess.run(["pdfinfo", path], capture_output=True, timeout=60)
    m = re.search(rb"^Pages:\s+(\d+)", res.stdout, re.M)
    return int(m.group(1)) if m else 0


DIAG = {"errors": [], "ocr": []}


def ocr_one(img):
    res = subprocess.run(["tesseract", img, "stdout", "-l", OCR_LANGS, "--psm", "3"],
                         capture_output=True, timeout=300)
    if res.returncode != 0 and len(DIAG["errors"]) < 5:
        DIAG["errors"].append(res.stderr.decode("utf-8", "replace")[-300:])
    t = res.stdout.decode("utf-8", "replace")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)          # λέξεις κομμένες στο τέλος γραμμής
    return " ".join(t.split())


def ocr_pdf(data, start, deadline):
    """OCR από τη σελίδα start· σταματά στο όριο χρόνου. Επιστρέφει (σελίδες, επόμενη σελίδα ή 0, σύνολο)."""
    out = []
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "f.pdf")
        with open(pdf, "wb") as f:
            f.write(data)
        total = pdf_page_count(pdf)
        page = max(1, start)
        workers = max(2, os.cpu_count() or 2)
        while page <= total:
            if time.time() > deadline:
                return out, page, total
            last = min(total, page + OCR_CHUNK - 1)
            for old in glob.glob(os.path.join(d, "p-*.png")):
                os.remove(old)
            subprocess.run(["pdftoppm", "-r", "200", "-gray", "-png", "-f", str(page), "-l", str(last),
                            pdf, os.path.join(d, "p")], capture_output=True, timeout=900)
            imgs = sorted(glob.glob(os.path.join(d, "p-*.png")),
                          key=lambda x: int(re.search(r"-(\d+)\.png$", x).group(1)))
            with concurrent.futures.ThreadPoolExecutor(workers) as ex:
                texts = list(ex.map(ocr_one, imgs))
            for img, t in zip(imgs, texts):
                n = int(re.search(r"-(\d+)\.png$", img).group(1))
                if len(t) >= 30:
                    out.append((n, t))
            page = last + 1
    return out, 0, total


def previous_ocr():
    """OCR από το προηγούμενο ευρετήριο: {attachment id: (doc, [σελίδες])}."""
    try:
        full = api("/api/attachments/%d" % INDEX_ATTACHMENT)
        old = json.loads(gzip.decompress(base64.b64decode(full.get("content") or "")).decode("utf-8"))
    except Exception as e:
        print("WARN previous index", type(e).__name__, file=sys.stderr)
        return {}
    keep = {}
    for i, d in enumerate(old.get("d", [])):
        if d.get("o") and d.get("a"):
            keep[d["a"]] = (d, [])
    by_index = {i: d["a"] for i, d in enumerate(old.get("d", [])) if d.get("o") and d.get("a")}
    for row in old.get("p", []):
        a = by_index.get(row[0])
        if a:
            keep[a][1].append((row[1], row[2]))
    return keep


# ---------- Σχολή (theo.auth.gr): ανακοινώσεις και προγράμματα, για το AI ----------
SCHOOL = "https://theo.auth.gr"
SCHOOL_CAT = 31
DAYS = ("ΔΕΥΤΕΡΑ", "ΤΡΙΤΗ", "ΤΕΤΑΡΤΗ", "ΠΕΜΠΤΗ", "ΠΑΡΑΣΚΕΥΗ", "ΣΑΒΒΑΤΟ")


def http(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (tipoukeitos-index)"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def strip_html(h):
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>|</tr>", "\n", h, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", h))


def schedule_chunks(text):
    """Πρόγραμμα (pdftotext -layout): ανά ημέρα, κάθε γραμμή «ώρα | μάθημα | διδάσκων | αίθουσα»."""
    lines = [re.sub(r" {3,}", " | ", l.strip()) for l in text.splitlines()]
    lines = [l for l in lines if l and not l.startswith("Column")]
    chunks, cur, head = [], [], ""
    for l in lines:
        up = l.replace(" ", "")
        if any(up.startswith(d) for d in DAYS) and len(l) < 25:
            if cur:
                chunks.append((head, cur))
            head, cur = l, []
        else:
            cur.append(l)
    if cur:
        chunks.append((head, cur))
    out = []
    for head, ls in chunks:
        part = []
        for l in ls:
            part.append(l)
            if sum(len(x) for x in part) > 1300:
                out.append((head, part)); part = []
        if part:
            out.append((head, part))
    return [((h + ": ") if h else "") + " \n".join(ls) for h, ls in out]


def school_docs():
    """Πρόσφατες ανακοινώσεις της Σχολής και τα PDF των προγραμμάτων (εξαμήνου / εξεταστικής)."""
    docs = []
    posts = json.loads(http("%s/wp-json/wp/v2/posts?categories=%d&per_page=40&_fields=date,title,link,content" % (SCHOOL, SCHOOL_CAT)))
    for p in posts:
        title = html.unescape(re.sub(r"<[^>]+>", "", p["title"]["rendered"])).strip()
        date = p["date"][:10]
        body = " ".join(strip_html(p["content"]["rendered"]).split())
        docs.append(({"a": 0, "x": p["link"], "n": title, "u": p["link"], "c": "Ανακοινώσεις Σχολής", "dt": date},
                     [[1, ("Ανακοίνωση της Σχολής (%s): %s. %s" % (date, title, body))[:6000]]]))
        if not re.search(r"πρόγραμμα|προγραμμα", title, re.I) or not re.search(r"εξεταστ|εξαμ|μαθημ|ωρολ", title, re.I):
            continue
        anchors = re.findall(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>(.*?)</a>', p["content"]["rendered"], re.I | re.S)
        for link, label in anchors[:3]:
            label = " ".join(html.unescape(re.sub(r"<[^>]+>", "", label)).replace("(pdf)", "").split())
            try:
                data = http(html.unescape(link), binary=True)
                with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                    f.write(data); f.flush()
                    res = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", "-q", f.name, "-"], capture_output=True, timeout=120)
                txt = res.stdout.decode("utf-8", "replace")
                chunks = schedule_chunks(txt)
                if not chunks:
                    continue
                stem = re.sub(r"[_\-]+", " ", os.path.splitext(os.path.basename(urllib.parse.urlparse(html.unescape(link)).path))[0]).strip()
                name = "%s (ανακοίνωση %s)" % (label if len(label) > 8 else "%s — %s" % (title, stem), date)
                docs.append(({"a": 0, "x": html.unescape(link), "n": name, "u": p["link"], "c": "Προγράμματα Σχολής", "dt": date},
                             [[1, ("%s — %s" % (name, ch))] for ch in chunks]))
            except Exception as e:
                print("WARN school pdf", type(e).__name__, file=sys.stderr)
    return docs


def main():
    started = time.time()
    attachments = [a for a in listing("/api/attachments")
                   if not a.get("external") and (a.get("extension") or "").lower() == "pdf"]
    pages, books = {}, {}
    docs, rows = [], []
    skipped_locked = skipped_scanned = failed = 0
    ocr_docs = ocr_pages = ocr_new = 0
    # Διάγνωση OCR (γράφεται και μέσα στο ευρετήριο, πεδίο "x"): έκδοση, γλώσσες, σφάλματα
    try:
        v = subprocess.run(["tesseract", "--version"], capture_output=True, timeout=30)
        DIAG["tess"] = (v.stdout or v.stderr).decode("utf-8", "replace").splitlines()[0][:80]
        l = subprocess.run(["tesseract", "--list-langs"], capture_output=True, timeout=30)
        DIAG["langs"] = [x for x in (l.stdout or l.stderr).decode("utf-8", "replace").split() if len(x) <= 8][-20:]
    except Exception as e:
        DIAG["tess"] = "ERR " + type(e).__name__
    prev = previous_ocr()
    deadline = started + OCR_MINUTES * 60
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
            extra = {}
            if not useful:
                # Σκαναρισμένο: OCR (ή ό,τι έγινε ήδη σε προηγούμενο βράδυ, αν το PDF δεν άλλαξε)
                up = a.get("updated_at") or full.get("updated_at") or ""
                old = prev.get(a["id"])
                start = 1
                if old and old[0].get("up") == up:
                    useful = list(old[1])
                    start = old[0].get("on") or 0
                if start and time.time() < deadline:
                    try:
                        got, nxt, total = ocr_pdf(data, start, deadline)
                    except Exception as e:
                        DIAG["errors"].append("ocr_pdf %d: %s %s" % (a["id"], type(e).__name__, str(e)[:200]))
                        got, nxt, total = [], 0, 0
                    DIAG["ocr"].append({"a": a["id"], "pages": total, "got": len(got), "chars": sum(len(t) for _, t in got)})
                    # αν το OCR δεν δουλεύει καθόλου (π.χ. λείπει γλώσσα), δεν χάνουμε χρόνο στα υπόλοιπα
                    if not got and DIAG["errors"] and len(DIAG["ocr"]) >= 2 and not any(x["got"] for x in DIAG["ocr"]):
                        deadline = 0
                    useful += got
                    ocr_new += len(got)
                    start = nxt
                    print("OCR attachment %d: +%d pages%s" % (a["id"], len(got), (" (continues at %d/%d)" % (nxt, total)) if nxt else ""))
                if not useful:
                    skipped_scanned += 1
                    continue
                extra = {"o": 1, "up": up}
                if start:
                    extra["on"] = start
                ocr_docs += 1
                ocr_pages += len(useful)
            di = len(docs)
            doc = {"a": a["id"], "n": a.get("name", ""),
                   "u": "/books/%s/page/%s" % (book_slug, pg.get("slug", "")),
                   "c": books[pg["book_id"]]}
            doc.update(extra)
            docs.append(doc)
            rows += [[di, n, t] for n, t in useful]
        except Exception as e:  # ένα χαλασμένο αρχείο δεν σταματά όλη τη δουλειά
            failed += 1
            print("WARN attachment", a.get("id"), type(e).__name__, file=sys.stderr)

    # Σχολή: ανακοινώσεις και προγράμματα (εξωτερικοί σύνδεσμοι)
    school = 0
    try:
        for doc, prow in school_docs():
            di = len(docs)
            docs.append(doc)
            rows += [[di, n, " ".join(t.split()) if doc["c"] != "Προγράμματα Σχολής" else t] for n, t in prow]
            school += 1
    except Exception as e:
        print("WARN school", type(e).__name__, file=sys.stderr)

    blob = gzip.compress(json.dumps({"v": 1, "t": int(time.time() * 1000), "d": docs, "p": rows, "x": DIAG},
                                    ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
    b = uuid.uuid4().hex
    def field(name, value):
        return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, name, value)).encode("utf-8")
    body = (field("_method", "PUT") + field("uploaded_to", str(PAGE_ID)) + field("name", "tk-pdfindex.json.gz")
            + ('--%s\r\nContent-Disposition: form-data; name="file"; filename="tk-pdfindex.json.gz"\r\n'
               'Content-Type: application/gzip\r\n\r\n' % b).encode("utf-8")
            + blob + ("\r\n--%s--\r\n" % b).encode("utf-8"))
    api("/api/attachments/%d" % INDEX_ATTACHMENT, data=body, content_type="multipart/form-data; boundary=" + b)
    print("OK pdf:%d indexed:%d school:%d pages:%d locked:%d scanned:%d failed:%d ocr:%d docs/%d pages (+%d new) size:%.1fMB time:%ds" % (
        len(attachments), len(docs) - school, school, len(rows), skipped_locked, skipped_scanned, failed,
        ocr_docs, ocr_pages, ocr_new, len(blob) / 1048576, time.time() - started))


if __name__ == "__main__":
    main()
