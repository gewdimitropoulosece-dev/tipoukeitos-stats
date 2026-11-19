#!/usr/bin/env python3
"""Αντίγραφο ασφαλείας του Τιπούκειτου (BookStack) μέσω του API.

Τι κρατά:
- ράφια (εξάμηνα), βιβλία (μαθήματα), κεφάλαια, σελίδες (HTML + markdown), ετικέτες,
- ΟΛΑ τα συνημμένα αρχεία (PDF κ.λπ.) και τις εικόνες της συλλογής,
- κάθε βιβλίο και σε ενιαίο, ευανάγνωστο HTML (export),
- χρήστες (όνομα, email, ρόλοι — χωρίς κωδικούς), ρόλους, δικαιώματα περιεχομένου, κάδο ανακύκλωσης.
Τα σχόλια (μηνύματα του τσατ) δεν διατίθενται από το API αυτής της έκδοσης του BookStack.

Το αρχείο κρυπτογραφείται ΠΡΙΝ φύγει από τον server του GitHub: με τυχαίο κλειδί AES-256,
που κρυπτογραφείται με το δημόσιο κλειδί backup_public.pem. Ανοίγει ΜΟΝΟ με το ιδιωτικό κλειδί,
που υπάρχει μόνο στο Mac του διαχειριστή (φάκελος «αντίγραφα-ασφαλείας»).
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BS_URL", "https://tipoukeitostheo.gr").rstrip("/")
AUTH = "Token %s:%s" % (os.environ["BS_TOKEN_ID"], os.environ["BS_TOKEN_SECRET"])
OUT = os.environ.get("OUT_DIR", "out")
# Δημόσιο κλειδί (ΔΕΝ είναι μυστικό: μόνο κρυπτογραφεί). Το ιδιωτικό είναι μόνο στο Mac του διαχειριστή.
PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAw6x3Nx9B7D4EdKSWlm+E
/R/lN8p8m53FHi2TDaw/yc2EMDW6nu3oP9L2rvnh98HuRZMjwiKr2z3/G8HVjl94
Dwdl8MflXsoDNaiNa3cr82qlvYpOB9nX10D7mhkwb0l58sRGjdVKjaUywLJfIpgx
wZ1AEIiRZFUilOHS5pOLpbJvr2y++Mss/LpPchwMdM5qBwPQNRjehgdJ6tKenqPH
UNe6IZxjs+VxLVJRnbGbspzPLdi6SziaLfMZTi5xo0iMOV1xKYukfLt/8dRxorRn
I9oXyLFLQ6is0wwJM2euvQG5XFgDgGVkFdoVdZ6U3VjXNvWPBnAWVwEQkpijJh/m
5pTjksTiDaXayUnpK1K8tm8vovWCXTTUftn1KOCdWafbeRDH3bH1vzcZ7hP/H2cC
+X/Y72mSDRBYRZF/aQShNdf1cjSImBYlWuQQvecUgzCcLwRlxkv9+apa9suv++64
pAfb+rUZFCps8uQZBRTjeqnICrAu8cryS2qSeu/US5EU11oOKmco3phywv7kCXUi
WZXK0zx3tIhPQG2MvyuT3+73Nu0xE2QRuhHGcsncYoi4F5KeYhDno+4i2oxewXga
T0ytLnSja6hu4CQ0PPtm1dNu/7JzUuAkqrRBb1gL5PpIY+pZE8qwC5/D25GPgeb7
PENA0yao3UtXxR5lDDBgTFECAwEAAQ==
-----END PUBLIC KEY-----
"""
STAMP = time.strftime("%Y-%m-%d")
ROOT = os.path.join(OUT, "tipoukeitos-" + STAMP)
_last = [0.0]


def req(path, raw=False, tries=6):
    """GET στο API με ρυθμό ~2,5 αιτήματα/δευτ. (όριο BookStack: 180/λεπτό) και επαναλήψεις."""
    url = path if path.startswith("http") else BASE + path
    for i in range(tries):
        wait = 0.42 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = urllib.request.Request(url, headers={"Authorization": AUTH, "Accept": "application/json",
                                                      "User-Agent": "tipoukeitos-backup"})
            with urllib.request.urlopen(r, timeout=300) as resp:
                data = resp.read()
                return data if raw else json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After") or 60))
                continue
            if e.code in (404, 403):
                raise
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def listing(path):
    out, offset = [], 0
    while True:
        sep = "&" if "?" in path else "?"
        page = req("%s%scount=500&offset=%d" % (path, sep, offset))
        out += page.get("data", [])
        offset += 500
        if offset >= page.get("total", 0):
            return out


def save(rel, obj):
    p = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def save_bytes(rel, data):
    p = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def safe(name):
    return re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("_")[:120] or "file"


def main():
    started = time.time()
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(ROOT)
    stats, problems = {}, []

    def each(kind, items, fn):
        n = 0
        for it in items:
            try:
                fn(it)
                n += 1
            except Exception as e:
                problems.append("%s %s: %s" % (kind, it.get("id"), type(e).__name__))
        stats[kind] = n

    shelves = listing("/api/shelves")
    save("shelves.json", shelves)
    each("shelves", shelves, lambda s: save("shelves/%d.json" % s["id"], req("/api/shelves/%d" % s["id"])))

    books = listing("/api/books")
    save("books.json", books)

    def book(b):
        save("books/%d.json" % b["id"], req("/api/books/%d" % b["id"]))
        html = req("/api/books/%d/export/html" % b["id"], raw=True)
        save_bytes("books-html/%d-%s.html" % (b["id"], safe(b.get("slug") or "")), html)
    each("books", books, book)

    chapters = listing("/api/chapters")
    save("chapters.json", chapters)
    each("chapters", chapters, lambda c: save("chapters/%d.json" % c["id"], req("/api/chapters/%d" % c["id"])))

    pages = listing("/api/pages")
    save("pages.json", pages)
    each("pages", pages, lambda p: save("pages/%d.json" % p["id"], req("/api/pages/%d" % p["id"])))

    atts = listing("/api/attachments")
    save("attachments.json", atts)

    def attachment(a):
        full = req("/api/attachments/%d" % a["id"])
        content = full.pop("content", None)
        save("attachments/%d.json" % a["id"], full)
        if not a.get("external") and content:
            ext = (a.get("extension") or "bin").lower()
            save_bytes("attachments/files/%d-%s.%s" % (a["id"], safe(re.sub(r"\.%s$" % re.escape(ext), "", a.get("name") or "", flags=re.I)), ext),
                       base64.b64decode(content))
    each("attachments", atts, attachment)

    imgs = listing("/api/image-gallery")
    save("images.json", imgs)

    def image(im):
        url = im.get("url") or ""
        if not url:
            return
        save_bytes("images/%d-%s" % (im["id"], safe(os.path.basename(url.split("?")[0]))), req(url, raw=True))
    each("images", imgs, image)

    users = listing("/api/users")
    each("users", users, lambda u: save("users/%d.json" % u["id"], req("/api/users/%d" % u["id"])))
    roles = listing("/api/roles")
    each("roles", roles, lambda r: save("roles/%d.json" % r["id"], req("/api/roles/%d" % r["id"])))

    perms = {}
    for kind, items in (("bookshelf", shelves), ("book", books), ("chapter", chapters), ("page", pages)):
        for it in items:
            try:
                perms["%s/%d" % (kind, it["id"])] = req("/api/content-permissions/%s/%d" % (kind, it["id"]))
            except Exception as e:
                problems.append("perm %s/%s: %s" % (kind, it.get("id"), type(e).__name__))
    save("content-permissions.json", perms)
    stats["permissions"] = len(perms)
    try:
        save("recycle-bin.json", listing("/api/recycle-bin"))
    except Exception as e:
        problems.append("recycle-bin: %s" % type(e).__name__)

    manifest = {"site": BASE, "date": STAMP, "counts": stats, "problems": problems,
                "note": "Τα σχόλια/μηνύματα τσατ δεν περιλαμβάνονται (δεν τα δίνει το API του BookStack v23)."}
    save("MANIFEST.json", manifest)

    # Συμπίεση + κρυπτογράφηση (AES-256 με τυχαίο κλειδί· το κλειδί με RSA-OAEP στο δημόσιο κλειδί)
    tar_path = os.path.join(OUT, "backup.tar.gz")
    with tarfile.open(tar_path, "w:gz") as t:
        t.add(ROOT, arcname=os.path.basename(ROOT))
    key_path = os.path.join(OUT, "key.bin")
    with open(key_path, "wb") as f:
        f.write(os.urandom(32))
    final = os.path.join(OUT, "final")
    os.makedirs(final)
    enc = os.path.join(final, "tipoukeitos-%s.tar.gz.enc" % STAMP)
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                    "-in", tar_path, "-out", enc, "-pass", "file:" + key_path], check=True)
    pub_path = os.path.join(OUT, "public.pem")
    with open(pub_path, "w") as f:
        f.write(PUBLIC_KEY)
    subprocess.run(["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", pub_path,
                    "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
                    "-in", key_path, "-out", os.path.join(final, "tipoukeitos-%s.key.enc" % STAMP)], check=True)
    size = os.path.getsize(enc)
    # Καθαρισμός: τίποτα μη κρυπτογραφημένο δεν μένει (ούτε για το artifact)
    os.remove(key_path)
    os.remove(tar_path)
    shutil.rmtree(ROOT)
    with open(os.path.join(final, "ΠΕΡΙΕΧΟΜΕΝΑ.txt"), "w", encoding="utf-8") as f:
        f.write("Αντίγραφο ασφαλείας Τιπούκειτου %s (κρυπτογραφημένο)\n" % STAMP)
        for k, v in stats.items():
            f.write("%s: %d\n" % (k, v))
        f.write("προβλήματα: %d\n" % len(problems))
    print("OK backup %s size:%.1fMB counts:%s problems:%d time:%ds" % (
        STAMP, size / 1048576, json.dumps(stats, ensure_ascii=False), len(problems), time.time() - started))
    for p in problems[:30]:
        print("WARN", p, file=sys.stderr)


if __name__ == "__main__":
    main()
