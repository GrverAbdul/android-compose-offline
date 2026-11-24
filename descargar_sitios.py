import os
import asyncio
import re
import json
import hashlib
import time
from pathlib import Path
from functools import lru_cache
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote, parse_qs
from bs4 import BeautifulSoup

from patchright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================
# PROXY (ScrapeOps - Residencial)
# ============================================================
PROXY_CONFIG = {
    "server": "http://residential-proxy.scrapeops.io:8181",
    "username": "scrapeops",
    "password": "7d7df815-de06-4ba8-9693-ab8cacefe03a",
}

CATALOG_SEEDS = [
    "https://ponn.io/books/mapping.json",
    "https://ponn.io/books/reader-index.json",
    "https://ponn.io/books/levels.json",
    "https://ponn.io/books/chapter-summary/index.json",
    "https://ponn.io/books/covers-lang.json",
    "https://ponn.io/audiobooks/data/index.json",
    "https://ponn.io/audiobooks/data/intro.json",
    "https://ponn.io/audiobooks/data/intro1.json",
    "https://ponn.io/audiobooks/data/intro-parts.json",
    "https://ponn.io/audiobooks/data/locales.json",
    "https://ponn.io/audiobooks/data/titles-es.json",
    "https://ponn.io/audiobooks/data/desc-ids/es.json",
    "https://ponn.io/audiobooks/data/desc/es.json",
    "https://ponn.io/sandbox/reader-audio/audio.json",
    "https://ponn.io/mw/lesson-tasks.css?v=2",
    "https://ponn.io/mw/lesson-games.css?v=1104p",
    "https://ponn.io/mw/lesson-games-es-f6f9e708.js?v=1104p",
    "https://ponn.io/mw/lg-manifest-1264ceac.json",
]

SITES = [
    {
        "name": "Ponn.io - Inglés (UI español)",
        "start": "https://ponn.io/es/",
        "starts": ["https://ponn.io/es/"],
        "hosts": {"ponn.io"},
        "resource_hosts": set(),
        "allowed_paths": ["/es/", "/reader.html", "/audiobooks/", "/games/", "/sandbox/"],
        "exclude_paths": [
            "/en/",
            "/es/serbio/", "/es/aleman/", "/es/frances/", "/es/italiano/",
            "/es/portugues/", "/es/ruso/", "/es/japones/", "/es/chino/",
            "/es/coreano/", "/es/arabe/", "/es/turco/", "/es/polaco/",
            "/es/holandes/", "/es/sueco/", "/es/noruego/", "/es/danes/",
            "/es/finlandes/", "/es/griego/", "/es/hebreo/", "/es/hindi/",
            "/es/ingles/video/", "/es/ingles/cursos/",
        ],
        "expand_load_more": True,
        "max_load_more_clicks": 100,
        "use_proxy": True,
        "max_total_mb": 50000,
    },
]

CONTEXTS = {"proxy": None, "direct": None}
CURRENT_TRACKER = {"ref": None}

ROOT = Path("salida")
METADATA = ROOT / "_metadata"
URL_MAP_FILE = METADATA / "url-map.json"
PAGE_MAP_FILE = METADATA / "page-map.json"

MAX_PAGES_PER_SITE = 50000
MAX_DEPTH = 35
NAVIGATION_TIMEOUT = 45000
NETWORK_IDLE_TIMEOUT = 2500
WAIT_AFTER_LOAD_MS = 300
PAGE_WORKERS = 2
NAVIGATION_RETRIES = 2
MAX_PAGE_ATTEMPTS = 3
RESOURCE_BODY_RETRIES = 2
RETRY_DELAY_MS = 800
LAZY_SCROLL_STEP_MS = 80
MAX_LAZY_SCROLL_STEPS = 30
MAX_RESOURCE_SIZE = 700 * 1024 * 1024
CHUNK_LIMIT = 100
MAX_CHUNK_ITERATIONS = 200
CHUNK_CONCURRENCY = 4
MAPS_SAVE_INTERVAL_SEC = 300
MAPS_SAVE_EVERY_PAGES = 500
MAX_RUNTIME_SEC = 5 * 3600  # 5h (margen sobre timeout de GitHub)
STALL_FAILS_THRESHOLD = 30
RECREATE_CTX_EVERY_PAGES = 1000

IGNORED_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "blob:", "intent:", "about:")

NON_PAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".mjs", ".json", ".xml", ".txt", ".pdf",
    ".mp4", ".webm", ".mov", ".mp3", ".wav", ".ogg",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".sql", ".db",
    ".apk", ".exe", ".msi", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".bin", ".webmanifest"
}

HTML_MIME = {"text/html", "application/xhtml+xml"}

MIME_EXTENSIONS = {
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/x-javascript": ".js",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
}

url_to_file = {}
file_to_url = {}
page_urls = set()
asset_urls = set()
resource_locks = {}
local_path_owners = {}
errors = []
visited_by_site = {}
site_storage = {}

MAPS_LOCK = asyncio.Lock()
LAST_MAPS_SAVE = {"time": time.time(), "pages": 0}
START_TIME = time.time()


def dedup_key(url):
    if "#/b/" in url:
        return url
    return clean_url(url)


def create_site_tracker(site):
    limit_mb = site.get("max_total_mb", 4096)
    return {
        "limite_mb": limit_mb,
        "limite_bytes": int(limit_mb * 1024 * 1024),
        "bytes_guardados": 0,
        "archivos": {},
        "limite_alcanzado": False,
        "bytes_proxy": 0,
        "bytes_direct": 0,
        "requests_proxy": 0,
        "requests_direct": 0,
    }


def format_mb(byte_count):
    return round(byte_count / 1024 / 1024, 2)


def register_transfer(bytes_count, via):
    tracker = CURRENT_TRACKER["ref"]
    if not tracker:
        return
    if via == "proxy":
        tracker["bytes_proxy"] += bytes_count
        tracker["requests_proxy"] += 1
    elif via == "direct":
        tracker["bytes_direct"] += bytes_count
        tracker["requests_direct"] += 1


def save_limited_file(path, data, tracker, description="archivo"):
    path = Path(path)
    key = str(path)
    new_size = len(data)
    previous_size = tracker["archivos"].get(key, 0)
    difference = new_size - previous_size
    if difference > 0:
        available = tracker["limite_bytes"] - tracker["bytes_guardados"]
        if difference > available:
            tracker["limite_alcanzado"] = True
            print(f"\n[ LIMITE ALCANZADO ] {description}")
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    tracker["archivos"][key] = new_size
    tracker["bytes_guardados"] += difference
    return True


def clean_url_with_hash(url):
    if not url:
        return ""
    try:
        parts = urlsplit(str(url).strip())
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.hostname:
            return ""
        hostname = parts.hostname.lower()
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = parts.port
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            hostname = f"{hostname}:{port}"
        userinfo = ""
        if "@" in parts.netloc:
            userinfo = parts.netloc.rsplit("@", 1)[0] + "@"
        netloc = userinfo + hostname
        path = parts.path or "/"

        def normalize_escape(match):
            value = int(match.group(1), 16)
            char = chr(value)
            if ("A" <= char <= "Z" or "a" <= char <= "z" or "0" <= char <= "9" or char in "-._~"):
                return char
            return "%" + match.group(1).upper()

        path = re.sub(r"%([0-9a-fA-F]{2})", normalize_escape, path)
        query = re.sub(r"%([0-9a-fA-F]{2})", lambda m: "%" + m.group(1).upper(), parts.query)
        return urlunsplit((scheme, netloc, path, query, parts.fragment))
    except Exception:
        return ""


def clean_url(url):
    result = clean_url_with_hash(url)
    if "#" in result:
        return result.split("#", 1)[0]
    return result


def is_internal(url, site):
    try:
        return urlsplit(url).hostname in site["hosts"]
    except Exception:
        return False


def is_allowed_page(url, site):
    if not is_internal(url, site):
        return False
    try:
        parts = urlsplit(url)
        path = parts.path or "/"
        for prefix in site.get("exclude_paths", []):
            prefix = str(prefix).rstrip("/") or "/"
            if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
                return False
        if path == "/reader.html":
            qs = parse_qs(parts.query)
            lang = qs.get("lang", [None])[0]
            if lang != "es":
                return False
        allowed_paths = site.get("allowed_paths")
        if not allowed_paths:
            return True
        for prefix in allowed_paths:
            prefix = str(prefix).rstrip("/") or "/"
            if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
                return True
        return False
    except Exception:
        return False


def is_allowed_resource(url, site):
    try:
        host = urlsplit(url).hostname
        return host in site["hosts"] or host in site.get("resource_hosts", set())
    except Exception:
        return False


def query_suffix(query):
    if not query:
        return ""
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
    return "__q_" + digest


def looks_like_html(data):
    if not data:
        return False
    sample = data[:4096].lstrip().lower()
    signatures = (b"<!doctype html", b"<html", b"<head", b"<body", b"<title", b"<meta", b"<?xml")
    return any(sample.startswith(x) for x in signatures)


def looks_like_css(data):
    if not data:
        return False
    sample = data[:2048].lstrip().lower()
    return b"{" in sample and (b"body" in sample or b"html" in sample or b"@" in sample or b":" in sample)


def detect_extension(url, content_type, data):
    mime = (content_type or "").split(";")[0].strip().lower()
    path_ext = Path(urlsplit(url).path).suffix.lower()
    if looks_like_html(data): return ".html"
    if data.startswith(b"%PDF-"): return ".pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return ".png"
    if data.startswith(b"\xff\xd8\xff"): return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")): return ".gif"
    if data.startswith(b"PK\x03\x04"):
        return path_ext if path_ext in (".zip", ".docx", ".xlsx", ".pptx", ".apk") else ".zip"
    if len(data) >= 12 and data[4:8] == b"ftyp": return ".mp4"
    if data.startswith(b"wOFF"): return ".woff"
    if data.startswith(b"wOF2"): return ".woff2"
    if data.startswith(b"\x00\x01\x00\x00"): return ".ttf"
    if mime in MIME_EXTENSIONS: return MIME_EXTENSIONS[mime]
    if mime == "text/css" or looks_like_css(data): return ".css"
    if path_ext: return path_ext
    return ".bin"


def sanitize_path_component(value, max_length=150):
    value = str(value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = value.rstrip(" .")
    if value in ("", ".", ".."):
        value = "_"
    stem = value.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{i}" for i in range(1, 10))
    reserved.update(f"LPT{i}" for i in range(1, 10))
    if stem in reserved:
        value = "_" + value
    if len(value) > max_length:
        suffix = Path(value).suffix
        digest = hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:10]
        keep = max(1, max_length - len(suffix) - len(digest) - 2)
        value = value[:keep] + "__" + digest + suffix
    return value


def reserve_local_path(url, path):
    url = clean_url_with_hash(url)
    path = Path(path)
    original = path
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    counter = 0
    while True:
        key = str(path).replace("\\", "/").casefold()
        owner = local_path_owners.get(key)
        occupied_by_other = owner is not None and owner != url
        occupied_without_map = path.exists() and owner != url
        if not occupied_by_other and not occupied_without_map:
            local_path_owners[key] = url
            return path
        counter += 1
        marker = "__" + digest + (f"_{counter}" if counter > 1 else "")
        path = original.with_name(original.stem + marker + original.suffix)


def safe_local_path(url, force_html=False, content_type=None):
    parts = urlsplit(url)
    host = sanitize_path_component(parts.hostname or "desconocido", 150)
    web_path = unquote(parts.path or "/")
    if web_path.endswith("/"):
        web_path += "index"
    components = [sanitize_path_component(part) for part in web_path.lstrip("/").split("/") if part]
    relative = Path(host).joinpath(*components) if components else Path(host) / "index"
    mime = (content_type or "").split(";")[0].strip().lower()
    suffix = relative.suffix.lower()
    if force_html or mime in HTML_MIME:
        if suffix in ("", ".bin", ".php", ".asp", ".aspx", ".jsp"):
            relative = relative.with_suffix(".html")
    elif not suffix:
        extension = MIME_EXTENSIONS.get(mime, ".bin")
        relative = relative.with_suffix(extension)
    if parts.query:
        relative = relative.with_name(relative.stem + query_suffix(parts.query) + relative.suffix)
    if parts.fragment:
        frag_hash = hashlib.sha1(parts.fragment.encode("utf-8")).hexdigest()[:10]
        relative = relative.with_name(relative.stem + "__h_" + frag_hash + relative.suffix)
    return reserve_local_path(url, ROOT / relative)


def register_url(url, path, is_page=False):
    url = clean_url_with_hash(url)
    if not url:
        return
    path = Path(path)
    url_to_file[url] = path
    file_to_url[str(path)] = url
    local_path_owners[str(path).replace("\\", "/").casefold()] = url
    if is_page:
        page_urls.add(url)


def normalize_existing_paths():
    if not ROOT.exists():
        return
    entries = sorted([item for item in ROOT.rglob("*") if item != ROOT], key=lambda item: len(item.parts), reverse=True)
    moved = {}
    for old in entries:
        if not old.exists(): continue
        safe_name = sanitize_path_component(old.name)
        if safe_name == old.name: continue
        new = old.with_name(safe_name)
        if new.exists():
            digest = hashlib.sha1(str(old).encode("utf-8")).hexdigest()[:10]
            new = old.with_name(old.stem + "__" + digest + old.suffix)
        try:
            old.rename(new)
            moved[str(old)] = new
        except Exception as exc:
            errors.append(f"NORMALIZAR RUTA | {old} | {exc}")
    if moved:
        for url, path in list(url_to_file.items()):
            old_text = str(path)
            replacement = moved.get(old_text)
            if replacement is None:
                for old_text_key, new_path in moved.items():
                    try:
                        relative = Path(old_text).relative_to(Path(old_text_key))
                        replacement = new_path / relative
                        break
                    except ValueError:
                        continue
            if replacement is not None:
                url_to_file[url] = replacement
        file_to_url.clear()
        local_path_owners.clear()
        for url, path in url_to_file.items():
            if path.exists():
                file_to_url[str(path)] = url
                local_path_owners[str(path).replace("\\", "/").casefold()] = url


def audit_artifact_paths():
    invalid = []
    collisions = {}
    seen = {}
    forbidden = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{i}" for i in range(1, 10))
    reserved.update(f"LPT{i}" for i in range(1, 10))
    for item in ROOT.rglob("*"):
        relative = item.relative_to(ROOT)
        for component in relative.parts:
            component_stem = component.split(".", 1)[0].upper()
            if (forbidden.search(component) or component.endswith((" ", ".")) or component in ("", ".", "..") or component_stem in reserved):
                invalid.append(str(relative))
                break
        key = str(relative).replace("\\", "/").casefold()
        if key in seen and seen[key] != str(relative):
            collisions.setdefault(key, [seen[key]]).append(str(relative))
        else:
            seen[key] = str(relative)
    print("Auditoría: inválidos =", len(invalid), "| colisiones =", len(collisions))
    if invalid:
        for item in invalid[:30]:
            errors.append(f"RUTA NO PORTABLE | {item}")
    return not invalid and not collisions


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def save_maps_now():
    url_map = {}
    for url, path in url_to_file.items():
        try:
            url_map[url] = str(Path(path).relative_to(ROOT)).replace(os.sep, "/")
        except ValueError:
            continue
    save_json(URL_MAP_FILE, url_map)
    save_json(PAGE_MAP_FILE, sorted(page_urls))


async def maybe_save_maps(pages_saved):
    async with MAPS_LOCK:
        now = time.time()
        elapsed = now - LAST_MAPS_SAVE["time"]
        pages_since = pages_saved - LAST_MAPS_SAVE["pages"]
        if elapsed >= MAPS_SAVE_INTERVAL_SEC or pages_since >= MAPS_SAVE_EVERY_PAGES:
            save_maps_now()
            LAST_MAPS_SAVE["time"] = now
            LAST_MAPS_SAVE["pages"] = pages_saved
            print(f"      [Mapas] Guardado intermedio (páginas={pages_saved})")


def load_previous_maps():
    if URL_MAP_FILE.exists():
        try:
            data = json.loads(URL_MAP_FILE.read_text(encoding="utf-8"))
            for url, relative in data.items():
                path = ROOT / relative
                if path.exists():
                    register_url(url, path)
            print("URLs recuperadas:", len(url_to_file))
        except Exception as exc:
            print("Error recuperando mapa:", exc)
    if PAGE_MAP_FILE.exists():
        try:
            data = json.loads(PAGE_MAP_FILE.read_text(encoding="utf-8"))
            for url in data:
                page_urls.add(clean_url_with_hash(url))
        except Exception as exc:
            print("Error recuperando páginas:", exc)


# ============================================================
# FETCH HÍBRIDO CON CACHE Y CONTADORES
# ============================================================
async def fetch_bytes(url, prefer_direct=True):
    existing = url_to_file.get(url)
    if existing and Path(existing).is_file():
        try:
            return Path(existing).read_bytes(), "cached"
        except Exception:
            pass

    order = ["direct", "proxy"] if prefer_direct else ["proxy", "direct"]
    for ctx_name in order:
        ctx = CONTEXTS.get(ctx_name)
        if not ctx:
            continue
        try:
            response = await ctx.request.get(url, timeout=60000)
            if response.ok:
                body = await response.body()
                if body:
                    if ctx_name in ("proxy", "direct"):
                        register_transfer(len(body), ctx_name)
                    return body, ctx_name
        except Exception:
            continue
    return None, None


def save_fetched(data, url, tracker, content_type=None, description=None):
    if not data:
        return None
    ext = detect_extension(url, content_type, data)
    path = safe_local_path(url, content_type=content_type)
    if path.suffix.lower() != ext:
        path = reserve_local_path(url, path.with_suffix(ext))
    if save_limited_file(path, data, tracker, description=description or url):
        register_url(url, path)
        asset_urls.add(url)
        return path
    return None


# ============================================================
# VALIDACIÓN DE CHUNKS (detección de "falso vacío")
# ============================================================
def chunk_has_data(data_bytes, type_):
    if not data_bytes:
        return False
    try:
        obj = json.loads(data_bytes)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    if type_ == "toc":
        return obj.get("realMax", 0) > 0
    for k, v in obj.items():
        if isinstance(v, list) and len(v) > 0:
            return True
    return False


# ============================================================
# AUDIOLIBROS
# ============================================================
async def process_audiobook(site, tracker, book_id):
    json_url = f"https://ponn.io/audiobooks/data/books/{book_id}.json"
    data, ctx_used = await fetch_bytes(json_url, prefer_direct=False)
    if not data:
        errors.append(f"AUDIOBOOK JSON | {book_id} | no descargado")
        return 0

    save_fetched(data, json_url, tracker, content_type="application/json")
    print(f"      [Audio] JSON ({ctx_used}): {book_id}")

    try:
        meta = json.loads(data)
    except Exception:
        errors.append(f"AUDIOBOOK JSON PARSE | {book_id}")
        return 0

    base_url = meta.get("baseUrl", "")
    chapters = meta.get("chapters", [])
    if not base_url or not chapters:
        errors.append(f"AUDIOBOOK SIN CAPÍTULOS | {book_id}")
        return 0

    count = 0
    for ch in chapters:
        file = ch.get("file")
        if not file:
            continue
        mp3_url = base_url + file
        mp3_data, _ = await fetch_bytes(mp3_url, prefer_direct=True)
        if mp3_data:
            p = save_fetched(mp3_data, mp3_url, tracker, content_type="audio/mpeg")
            if p:
                count += 1
        else:
            errors.append(f"MP3 FALLO | {mp3_url}")

    cover_url = f"https://ponn.io/sandbox/audiolab/covers-sq/{book_id}.jpg"
    cover_data, _ = await fetch_bytes(cover_url, prefer_direct=False)
    if cover_data:
        save_fetched(cover_data, cover_url, tracker, content_type="image/jpeg")

    print(f"      [Audio] {book_id}: {count}/{len(chapters)} MP3")
    return count


# ============================================================
# READERS CON CONCURRENCIA ×4
# ============================================================
async def fetch_and_save_chunk(url, book_id, lang, type_, from_, semaphore, tracker):
    async with semaphore:
        data, _ = await fetch_bytes(url, prefer_direct=True)
        if not data:
            errors.append(f"CHUNK VACÍO | {book_id} | {lang} | {type_} | from={from_}")
            return 0
        if not chunk_has_data(data, type_):
            errors.append(f"CHUNK SIN DATOS | {book_id} | {lang} | {type_} | from={from_}")
            return 0
        p = save_fetched(data, url, tracker, content_type="application/json")
        return 1 if p else 0


async def process_reader(site, tracker, book_id):
    if not book_id:
        return 0

    toc_url = f"https://getbookchunk-ab3uqz2diq-ew.a.run.app/?bookId={book_id}&lang=es&type=toc&from=1&access=free"
    toc_data, _ = await fetch_bytes(toc_url, prefer_direct=True)
    real_max = 0
    if toc_data:
        save_fetched(toc_data, toc_url, tracker, content_type="application/json")
        try:
            toc = json.loads(toc_data)
            real_max = toc.get("realMax", 0)
            chapters_count = len(toc.get("chapters", []))
            if real_max == 0:
                errors.append(f"READER SIN TEXTO (realMax=0) | {book_id}")
                return 0
        except Exception as e:
            errors.append(f"READER TOC PARSE | {book_id} | {e}")
            return 0
    if real_max <= 0:
        return 0

    print(f"      [Reader] {book_id}: realMax={real_max}")

    semaphore = asyncio.Semaphore(CHUNK_CONCURRENCY)
    tasks = []
    for lang in ["en", "es"]:
        for type_ in ["sentences", "phrases"]:
            start = 1
            iterations = 0
            while start <= real_max and iterations < MAX_CHUNK_ITERATIONS:
                url = (f"https://getbookchunk-ab3uqz2diq-ew.a.run.app/?"
                       f"bookId={book_id}&lang={lang}&type={type_}"
                       f"&from={start}&limit={CHUNK_LIMIT}&access=free")
                tasks.append(fetch_and_save_chunk(url, book_id, lang, type_, start, semaphore, tracker))
                start += CHUNK_LIMIT
                iterations += 1

    results = await asyncio.gather(*tasks, return_exceptions=True)
    total_chunks = sum(1 for r in results if isinstance(r, int) and r > 0)

    # reader-audio partN (secuencial, son pocos)
    n = 1
    parts_ok = 0
    while n <= 100:
        url = f"https://ponn.io/sandbox/reader-audio/acc/{book_id}/part{n}.json"
        data, _ = await fetch_bytes(url, prefer_direct=True)
        if not data or len(data) < 10:
            break
        if save_fetched(data, url, tracker, content_type="application/json"):
            parts_ok += 1
        n += 1

    print(f"      [Reader] {book_id}: {total_chunks} chunks + {parts_ok} parts")
    return total_chunks


# ============================================================
# LISTENER DE RECURSOS
# ============================================================
async def save_response_resource(response, site, tracker):
    url = ""
    try:
        if tracker["limite_alcanzado"]: return
        resource_type = response.request.resource_type
        if resource_type in ("document", "websocket"): return
        if response.status not in (200, 206): return
        url = clean_url(response.url)
        if not url or not is_allowed_resource(url, site): return
        allowed = {"stylesheet", "script", "image", "font", "manifest", "media"}
        if resource_type not in allowed: return
        lock = resource_locks.setdefault(url, asyncio.Lock())
        async with lock:
            existing_file = url_to_file.get(url)
            if existing_file and Path(existing_file).is_file(): return
            headers = response.headers
            content_type = headers.get("content-type", "")
            length = headers.get("content-length")
            if length:
                try:
                    if int(length) > MAX_RESOURCE_SIZE:
                        return
                except ValueError:
                    pass
            data = None
            for body_attempt in range(1, RESOURCE_BODY_RETRIES + 1):
                try:
                    data = await response.body()
                    if data: break
                except Exception:
                    if body_attempt >= RESOURCE_BODY_RETRIES: raise
                    await asyncio.sleep(0.25 * body_attempt)
            if not data or len(data) > MAX_RESOURCE_SIZE or looks_like_html(data): return
            register_transfer(len(data), "proxy")
            save_fetched(data, url, tracker, content_type=content_type)
    except Exception as exc:
        errors.append(f"RECURSO | {url or response.url} | {exc}")


async def extract_page_links(page, current_url, site):
    results = []
    try:
        elements = await page.locator(
            "a[href], area[href], iframe[src], frame[src], source[src], video[src], audio[src], link[href]"
        ).evaluate_all("""
            elements => elements.map(el => ({
                href: el.href || el.getAttribute('href') || '',
                src: el.src || el.getAttribute('src') || ''
            }))
        """)
        for item in elements:
            value = item.get("href") or item.get("src")
            if not value: continue
            value = value.strip()
            if value.startswith(IGNORED_SCHEMES): continue
            absolute_raw = urljoin(current_url, value)
            preserve = False
            try:
                p = urlsplit(absolute_raw)
                if p.hostname in site["hosts"] and p.path == "/audiobooks/" and p.fragment:
                    preserve = True
            except Exception:
                pass
            absolute = clean_url_with_hash(absolute_raw) if preserve else clean_url(absolute_raw)
            if not absolute: continue
            if not is_allowed_page(absolute, site): continue
            results.append(absolute)
    except Exception as exc:
        errors.append(f"EXTRACCION | {current_url} | {exc}")
    return list(dict.fromkeys(results))


def is_probable_page(url):
    extension = Path(urlsplit(url).path).suffix.lower()
    return extension not in NON_PAGE_EXTENSIONS


async def expand_load_more(page, site):
    print(f"   [Expand] Expandiendo 'Cargar más'...")
    clicks = 0
    without_changes = 0
    max_clicks = site.get("max_load_more_clicks", 100)
    selector_boton = "button, a, [role='button']"
    while clicks < max_clicks:
        try:
            boton = page.locator(selector_boton).filter(
                has_text=re.compile(r"(load\s*more|cargar\s*más|ver\s*más|show\s*more)", re.IGNORECASE)
            ).first
            if await boton.count() == 0:
                break
            if not await boton.is_visible() or not await boton.is_enabled():
                break
            previous_links = set(
                await page.locator("a[href]").evaluate_all("elements => elements.map(el => el.href).filter(Boolean)")
            )
            await boton.click()
            clicks += 1
            await page.wait_for_timeout(1500)
            current_links = set(
                await page.locator("a[href]").evaluate_all("elements => elements.map(el => el.href).filter(Boolean)")
            )
            if current_links - previous_links:
                without_changes = 0
            else:
                without_changes += 1
                if without_changes >= 2:
                    break
        except Exception:
            break
    print(f"   [Expand] Terminado. Clics: {clicks}")
    return clicks


# ============================================================
# WATCHDOG: recrear contextos y comprobar tiempo
# ============================================================
async def recreate_contexts(browser, base_kwargs):
    print("   [Watchdog] Recreando contextos (limpieza de conexiones)...")
    try:
        if CONTEXTS["proxy"]:
            await CONTEXTS["proxy"].close()
    except Exception:
        pass
    try:
        if CONTEXTS["direct"]:
            await CONTEXTS["direct"].close()
    except Exception:
        pass
    CONTEXTS["proxy"] = await browser.new_context(proxy=PROXY_CONFIG, **base_kwargs)
    CONTEXTS["proxy"].set_default_navigation_timeout(NAVIGATION_TIMEOUT)
    CONTEXTS["proxy"].set_default_timeout(15000)
    CONTEXTS["direct"] = await browser.new_context(**base_kwargs)
    print("   [Watchdog] Contextos recreados.")


def runtime_exceeded():
    return (time.time() - START_TIME) > MAX_RUNTIME_SEC


# ============================================================
# CRAWL
# ============================================================
async def crawl_site(browser, site, base_kwargs):
    print("\n" + "=" * 65)
    print("SITIO:", site["name"])
    print("=" * 65)

    tracker = create_site_tracker(site)
    site_storage[site["name"]] = tracker
    CURRENT_TRACKER["ref"] = tracker

    print("   [Catálogo] Descargando JSONs iniciales...")
    for seed_url in CATALOG_SEEDS:
        data, ctx_used = await fetch_bytes(seed_url, prefer_direct=False)
        if data:
            save_fetched(data, seed_url, tracker)
            print(f"   [Catálogo] {seed_url.split('/')[-1]} ({ctx_used})")

    ctx_proxy = CONTEXTS["proxy"]
    ctx_proxy.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
    ctx_proxy.set_default_timeout(15000)

    starts = site.get("starts", [site["start"]])
    initial_urls = list(dict.fromkeys(clean_url(url) for url in starts if clean_url(url)))

    queue = asyncio.Queue()
    for url in initial_urls:
        queue.put_nowait((url, 0, 1))

    queued = set(initial_urls)
    in_progress = set()
    visited = set()
    failed_pages = {}
    retry_count = 0
    pages_saved = 0
    resource_tasks = set()
    consecutive_fails = {"count": 0}
    pages_since_ctx_recreate = {"count": 0}

    pages = [await ctx_proxy.new_page() for _ in range(PAGE_WORKERS)]

    def attach_resource_listener(page):
        def on_response(response):
            task = asyncio.create_task(save_response_resource(response, site, tracker))
            resource_tasks.add(task)
            task.add_done_callback(resource_tasks.discard)
        page.on("response", on_response)

    for page in pages:
        attach_resource_listener(page)

    async def worker(worker_id, page):
        nonlocal pages_saved, retry_count
        while True:
            if runtime_exceeded():
                print("   [Watchdog] Tiempo máximo alcanzado, deteniendo worker.")
                return
            item = await queue.get()
            current_url = ""
            depth = 0
            attempt = 1
            eligible_for_retry = False
            success = False
            claimed = False
            key = None

            try:
                if item is None:
                    return
                current_url, depth, attempt = item
                current_url = clean_url_with_hash(current_url)
                key = dedup_key(current_url)

                if not current_url or key in in_progress:
                    continue
                if key in visited and "#/b/" not in current_url:
                    continue
                if depth > MAX_DEPTH:
                    continue
                if pages_saved >= MAX_PAGES_PER_SITE or tracker["limite_alcanzado"]:
                    continue
                if not is_allowed_page(current_url, site) or not is_probable_page(current_url):
                    continue

                in_progress.add(key)
                claimed = True
                eligible_for_retry = True

                print(f"[{site['name']}] [W{worker_id}] ({pages_saved+1}) Intento {attempt}: {current_url}")

                # FLASHCARDS
                if current_url.startswith("https://ponn.io/es/flashcards"):
                    for tab in ["sets", "mine", "practice"]:
                        try:
                            await page.goto(f"https://ponn.io/es/flashcards/?learn=en#{tab}",
                                            wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                            await page.wait_for_timeout(2500)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=3000)
                            except PlaywrightTimeoutError:
                                pass
                            html = await page.content()
                            if looks_like_html(html.encode("utf-8")):
                                local_file = ROOT / "ponn.io" / "es" / "flashcards" / f"index__{tab}.html"
                                save_limited_file(local_file, html.encode("utf-8"), tracker, description=f"flashcards_{tab}")
                                register_url(f"https://ponn.io/es/flashcards/?learn=en#{tab}", local_file, is_page=True)
                                pages_saved += 1
                                print(f"   [Flashcards] Guardada: {tab}")
                        except Exception as e:
                            errors.append(f"FLASHCARDS {tab} | {e}")
                    visited.add(current_url)
                    visited.add(clean_url(current_url))
                    success = True
                    consecutive_fails["count"] = 0
                    continue

                navigation_timed_out = False
                response = None
                previous_browser_url = clean_url(page.url)
                has_hash = "#" in current_url
                base_url = current_url.split("#", 1)[0] if has_hash else current_url

                for nav_attempt in range(1, NAVIGATION_RETRIES + 1):
                    try:
                        response = await page.goto(current_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                    except PlaywrightTimeoutError:
                        if nav_attempt < NAVIGATION_RETRIES:
                            await page.wait_for_timeout(RETRY_DELAY_MS)
                            continue
                        navigation_timed_out = True
                        response = None
                        break
                    if (response is not None and response.status in (429, 500, 502, 503, 504) and nav_attempt < NAVIGATION_RETRIES):
                        await page.wait_for_timeout(RETRY_DELAY_MS * nav_attempt)
                        continue
                    break

                if response is not None and response.status >= 400:
                    errors.append(f"HTTP {response.status} | {current_url}")
                    consecutive_fails["count"] += 1
                    continue
                if response is None:
                    consecutive_fails["count"] += 1
                    continue

                try:
                    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT)
                except PlaywrightTimeoutError:
                    pass

                if has_hash and previous_browser_url == base_url:
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass

                if "/audiobooks/" in current_url and "#/b/" in current_url:
                    book_id = current_url.split("#/b/")[1].split("&")[0].split("?")[0]
                    await process_audiobook(site, tracker, book_id)

                if "/reader.html" in current_url:
                    qs = parse_qs(urlsplit(current_url).query)
                    book_id = qs.get("id", [""])[0]
                    if book_id:
                        await process_reader(site, tracker, book_id)

                try:
                    await page.evaluate("""async (maxSteps, delay) => {
                        const pause = ms => new Promise(r => setTimeout(r, ms));
                        const h = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
                        const step = Math.max(window.innerHeight, Math.ceil(h / maxSteps));
                        for (let y = 0, n = 0; y < h && n < maxSteps; y += step, n++) {
                            window.scrollTo(0, y);
                            await pause(delay);
                        }
                        window.scrollTo(0, 0);
                    }""", MAX_LAZY_SCROLL_STEPS, LAZY_SCROLL_STEP_MS)
                    await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
                except Exception:
                    pass

                if site.get("expand_load_more", False):
                    await expand_load_more(page, site)

                final_url = clean_url_with_hash(page.url) or current_url
                html = await page.content()
                html_bytes = html.encode("utf-8", errors="replace")
                if not looks_like_html(html_bytes):
                    continue

                local_file = safe_local_path(final_url, force_html=True, content_type="text/html")
                if save_limited_file(local_file, html_bytes, tracker, description=final_url):
                    register_url(current_url, local_file, is_page=True)
                    register_url(final_url, local_file, is_page=True)
                    visited.add(key)
                    visited.add(clean_url(current_url))
                    pages_saved += 1
                    success = True
                    consecutive_fails["count"] = 0
                    pages_since_ctx_recreate["count"] += 1
                    print(f"   Guardada: {pages_saved} | disco: {format_mb(tracker['bytes_guardados'])} MB | "
                          f"proxy: {format_mb(tracker['bytes_proxy'])} MB | directo: {format_mb(tracker['bytes_direct'])} MB")
                    await maybe_save_maps(pages_saved)

                links = await extract_page_links(page, final_url, site)
                for link in links:
                    link_key = dedup_key(link)
                    if link_key in queued or link_key in visited or link_key in in_progress:
                        continue
                    if is_probable_page(link):
                        queued.add(link_key)
                        queue.put_nowait((link, depth + 1, 1))

            except PlaywrightTimeoutError:
                errors.append(f"TIMEOUT | {current_url}")
                consecutive_fails["count"] += 1
            except Exception as exc:
                errors.append(f"PAGINA | {current_url} | {exc}")
                consecutive_fails["count"] += 1
            finally:
                if claimed and key:
                    in_progress.discard(key)
                if eligible_for_retry and not success and current_url:
                    if attempt < MAX_PAGE_ATTEMPTS and not tracker["limite_alcanzado"]:
                        retry_count += 1
                        await asyncio.sleep(RETRY_DELAY_MS * attempt / 1000)
                        queue.put_nowait((current_url, depth, attempt + 1))
                    else:
                        failed_pages[current_url] = attempt
                queue.task_done()

    workers = [asyncio.create_task(worker(i + 1, p)) for i, p in enumerate(pages)]
    await queue.join()
    for _ in workers:
        queue.put_nowait(None)
    await asyncio.gather(*workers, return_exceptions=True)

    while resource_tasks:
        pending = list(resource_tasks)
        await asyncio.gather(*pending, return_exceptions=True)

    for page in pages:
        try: await page.close()
        except Exception: pass

    visited_by_site[site["name"]] = {
        "paginas_guardadas": pages_saved,
        "urls_descubiertas": len(queued),
        "paginas_fallidas_definitivas": len(failed_pages),
        "reintentos_de_pagina": retry_count,
        "almacenamiento_MB": format_mb(tracker["bytes_guardados"]),
        "proxy_MB": format_mb(tracker["bytes_proxy"]),
        "directo_MB": format_mb(tracker["bytes_direct"]),
        "requests_proxy": tracker["requests_proxy"],
        "requests_direct": tracker["requests_direct"],
        "limite_MB": tracker["limite_mb"],
        "limite_alcanzado": tracker["limite_alcanzado"],
    }

    print("\n" + "-" * 55)
    print("FINALIZADO:", site["name"])
    print("Páginas guardadas:", pages_saved)
    print("Disco:", format_mb(tracker["bytes_guardados"]), "MB")
    print("Proxy:", format_mb(tracker["bytes_proxy"]), "MB |", tracker["requests_proxy"], "req")
    print("Directo:", format_mb(tracker["bytes_direct"]), "MB |", tracker["requests_direct"], "req")
    print("-" * 55)


# ============================================================
# REESCRITURA, ÍNDICE, INFORME
# ============================================================
@lru_cache(maxsize=500000)
def find_local_target(url):
    url = clean_url_with_hash(url)
    if not url: return None
    candidates = [url]
    base = url.split("#", 1)[0]
    if base != url: candidates.append(base)
    parts = urlsplit(url)
    without_query = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
    if without_query not in candidates: candidates.append(without_query)
    path = parts.path or "/"
    if path.endswith("/"):
        candidates.append(urlunsplit((parts.scheme, parts.netloc, path + "index.html", "", parts.fragment)))
    elif not Path(path).suffix:
        candidates.append(urlunsplit((parts.scheme, parts.netloc, path + ".html", "", parts.fragment)))
    for candidate in candidates:
        target = url_to_file.get(clean_url_with_hash(candidate))
        if target and Path(target).exists(): return Path(target)
    for candidate in candidates:
        cp = urlsplit(clean_url_with_hash(candidate))
        host = cp.hostname or ""
        relative = unquote(cp.path or "/").lstrip("/")
        if not relative or relative.endswith("/"): relative += "index.html"
        local = ROOT / host / relative
        if local.exists() and local.is_file(): return local
    return None


def relative_target(source_file, target_file):
    return os.path.relpath(Path(target_file), start=Path(source_file).parent).replace(os.sep, "/")


def rewrite_html():
    for html_file in ROOT.rglob("*.html"):
        if "_metadata" in html_file.parts: continue
        source_url = file_to_url.get(str(html_file))
        if not source_url: continue
        try:
            soup = BeautifulSoup(html_file.read_text(encoding="utf-8", errors="replace"), "html.parser")
            for tag in soup.find_all(True):
                for attr in ("href", "src", "poster", "action", "data-src", "data-lazy-src", "data-original", "data-href", "data-url"):
                    value = tag.get(attr)
                    if not isinstance(value, str) or not value.strip(): continue
                    value = value.strip()
                    if value.startswith(IGNORED_SCHEMES) or value.startswith("#"): continue
                    absolute_with_fragment = urljoin(source_url, value)
                    parsed = urlsplit(absolute_with_fragment)
                    absolute = clean_url_with_hash(absolute_with_fragment)
                    if not absolute: continue
                    target = find_local_target(absolute)
                    if target:
                        local = relative_target(html_file, target)
                        if parsed.fragment: local += "#" + parsed.fragment
                        tag[attr] = local
                if tag.has_attr("srcset"):
                    rewritten = []
                    for item in str(tag["srcset"]).split(","):
                        bits = item.strip().split()
                        if not bits: continue
                        absolute = clean_url(urljoin(source_url, bits[0]))
                        target = find_local_target(absolute) if absolute else None
                        if target: bits[0] = relative_target(html_file, target)
                        rewritten.append(" ".join(bits))
                    tag["srcset"] = ", ".join(rewritten)
            html_file.write_text(str(soup), encoding="utf-8")
        except Exception as exc:
            errors.append(f"REESCRITURA HTML | {html_file} | {exc}")


def rewrite_css():
    url_pattern = re.compile(r"url\(\s*([\"']?)(.*?)\1\s*\)", re.IGNORECASE)
    for css_file in ROOT.rglob("*.css"):
        if "_metadata" in css_file.parts: continue
        source_url = file_to_url.get(str(css_file))
        if not source_url: continue
        try:
            content = css_file.read_text(encoding="utf-8", errors="replace")
            def replace_url(match):
                raw = match.group(2).strip()
                if not raw or raw.startswith(("data:", "blob:", "#")): return match.group(0)
                absolute = clean_url(urljoin(source_url, raw))
                target = find_local_target(absolute) if absolute else None
                if not target: return match.group(0)
                return "url(" + match.group(1) + relative_target(css_file, target) + match.group(1) + ")"
            content = url_pattern.sub(replace_url, content)
            css_file.write_text(content, encoding="utf-8")
        except Exception as exc:
            errors.append(f"REESCRITURA CSS | {css_file} | {exc}")


def create_offline_index():
    links = []
    for site in SITES:
        target = find_local_target(site["start"])
        if target and target.exists():
            href = os.path.relpath(target, ROOT).replace(os.sep, "/")
            links.append(f'<li><a href="{href}">{site["name"]}</a></li>')
        else:
            links.append(f'<li>{site["name"]} — no encontrada</li>')
    html = f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Offline</title>
<style>body{{font-family:Arial;max-width:900px;margin:40px auto;padding:20px;background:#f3f5f7}}
main{{background:#fff;padding:28px;border-radius:12px}}a{{color:#145db8}}</style></head>
<body><main><h1>Documentación offline</h1><ul>{"".join(links)}</ul></main></body></html>'''
    (ROOT / "INICIO_OFFLINE.html").write_text(html, encoding="utf-8")


def create_report():
    files = [p for p in ROOT.rglob("*") if p.is_file()]
    total_bytes = sum(p.stat().st_size for p in files)
    report = {
        "sitios": [{"name": s["name"], "start": s["start"]} for s in SITES],
        "paginas_por_sitio": visited_by_site,
        "archivos_generados": len(files),
        "tamano_total_MB": round(total_bytes / 1024 / 1024, 2),
        "errores": len(errors),
    }
    Path("informe-descarga.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("errores-descarga.txt").write_text("\n".join(errors), encoding="utf-8")
    print("\n========== INFORME ==========")
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    METADATA.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("DESCARGADOR HÍBRIDO — proxy + directo, con watchdog")
    print("=" * 65)

    load_previous_maps()
    normalize_existing_paths()

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            print("[Patchright] Chrome.")
        except Exception:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            print("[Patchright] Chromium parcheado.")

        base_kwargs = dict(
            ignore_https_errors=True,
            viewport={"width": 1920, "height": 1080},
            locale="es-ES",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            extra_http_headers={
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

        CONTEXTS["proxy"] = await browser.new_context(proxy=PROXY_CONFIG, **base_kwargs)
        print("[Contexto] proxy:", PROXY_CONFIG["server"])
        CONTEXTS["direct"] = await browser.new_context(**base_kwargs)
        print("[Contexto] directo")

        try:
            for site in SITES:
                await crawl_site(browser, site, base_kwargs)
        finally:
            await browser.close()

    print("\n===== GUARDANDO MAPAS FINALES =====")
    save_maps_now()
    print("\n===== REESCRITURA HTML =====")
    rewrite_html()
    print("\n===== REESCRITURA CSS =====")
    rewrite_css()
    print("\n===== ÍNDICE OFFLINE =====")
    create_offline_index()
    audit_artifact_paths()
    save_maps_now()
    create_report()
    print("\n===== FIN =====")


if __name__ == "__main__":
    asyncio.run(main())
