# -*- coding: utf-8 -*-
"""
PONN.IO OFFLINE DOWNLOADER
- Interfaz en español.
- Conserva el material de aprendizaje de inglés y sus traducciones al español.
- Los audiolibros se descargan únicamente si el índice declara lang=en.
- Los chunks del lector guardan ambos idiomas (en/es) porque el español es la traducción.
- Captura respuestas JSON de fetch/XHR del propio sitio y del endpoint de chunks.
- Descarga audios del lector y sus timings desde reader-audio/audio.json.
- Filtra reader.html por sourceLang de reader-index.json y por sufijos de idioma en IDs.
- Rota TrueProxies -> ScrapeOps -> Oxylabs ante errores de proxy/cuota.
- No modifica ni automatiza cuentas, ni intenta saltarse restricciones de acceso.
"""
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
# PROXIES RESIDENCIALES EN ORDEN DE USO
# 1) TrueProxies  2) ScrapeOps  3) Oxylabs
# Las credenciales fueron proporcionadas por el usuario.
# TrueProxies se rota preventivamente a los 48 min porque el acceso
# gratuito indicado por el usuario dura aproximadamente 50 min.
# ============================================================
PROXY_CONFIGS = {
    "trueproxies": {
        "server": "http://resi-ipv4-6.gw.tprx.io:8080",
        "username": "63lz3an8ulxa-rotate",
        "password": "k73TU0KeofRVd5feE3mi",
    },
    "scrapeops": {
        "server": "http://residential-proxy.scrapeops.io:8181",
        "username": "scrapeops",
        "password": "73ec1186-54e5-4821-8ef3-50ee272dff96",
    },
    "oxylabs": {
        "server": "http://pr.oxylabs.io:7777",
        "username": "customer-resi_b62ad9f3",
        "password": "86f1P~3acINB",
    },
}
PROXY_ORDER = ("trueproxies", "scrapeops", "oxylabs")
TRUEPROXIES_MAX_SECONDS = 59 * 60
PROXY_DISABLED = set()
PROXY_STARTED_AT = {}
ACTIVE_PROXY_NAME = "trueproxies"
PAGE_PROXY_NAMES = {}

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
    "https://ponn.io/mw/lg-manifest-1264ceac.json",
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
        "resource_hosts": {"getbookchunk-ab3uqz2diq-ew.a.run.app"},
        "allowed_paths": ["/es/", "/reader.html", "/audiobooks/", "/games/", "/sandbox/"],
        "exclude_paths": [
            # Se conserva toda la navegación española y sus páginas de inglés.
            # Solo se excluyen secciones cuyo idioma de aprendizaje no es inglés.
            "/en/",
            "/es/serbio/", "/es/aleman/", "/es/frances/", "/es/italiano/",
            "/es/portugues/", "/es/portugues-brasileno/", "/es/ruso/",
            "/es/japones/", "/es/chino/", "/es/coreano/", "/es/arabe/",
            "/es/turco/", "/es/polaco/", "/es/holandes/", "/es/sueco/",
            "/es/noruego/", "/es/danes/", "/es/finlandes/", "/es/griego/",
            "/es/hebreo/", "/es/hindi/", "/es/checo/", "/es/rumano/",
            "/es/georgiano/", "/es/hungaro/", "/es/ucraniano/",
        ],
        "expand_load_more": True,
        "max_load_more_clicks": 100,
        "use_proxy": True,
        "max_total_mb": 50000,
    },
]

CONTEXTS = {
    "proxy": None, "direct": None,
    "trueproxies": None, "scrapeops": None, "oxylabs": None,
}
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
        "bytes_trueproxies": 0,
        "bytes_scrapeops": 0,
        "bytes_oxylabs": 0,
        "bytes_direct": 0,
        "requests_proxy": 0,
        "requests_trueproxies": 0,
        "requests_scrapeops": 0,
        "requests_oxylabs": 0,
        "requests_direct": 0,
    }


def format_mb(byte_count):
    return round(byte_count / 1024 / 1024, 2)


def register_transfer(bytes_count, via):
    tracker = CURRENT_TRACKER["ref"]
    if not tracker:
        return
    if via in PROXY_ORDER:
        tracker["bytes_" + via] += bytes_count
        tracker["requests_" + via] += 1
        tracker["bytes_proxy"] += bytes_count
        tracker["requests_proxy"] += 1
    elif via == "proxy":
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



AUDIOBOOK_INDEX_URL = "https://ponn.io/audiobooks/data/index.json"
AUDIOBOOK_IDS_EN = set()
AUDIOBOOK_INDEX_LOADED = False

READER_AUDIO_INDEX_URL = "https://ponn.io/sandbox/reader-audio/audio.json"
READER_AUDIO_INDEX = {}
READER_AUDIO_INDEX_LOADED = False

# Idiomas observados en reader-index.json, covers-lang.json,
# chapter-summary/index.json y levels.json del material enviado.
# También se incluyen códigos que aparecen en las rutas de PONN.
ENGLISH_LANGUAGE_CODES = {"en", "eng", "english"}
KNOWN_LANGUAGE_CODES = {
    "al", "am", "ar", "az", "br", "cn", "cs", "da", "de", "en", "es",
    "el", "fi", "fr", "ge", "gr", "he", "hi", "hu", "id", "it", "ja", "jp",
    "ka", "ko", "kz", "lt", "lv", "nl", "no", "pl", "pt", "pt-br",
    "ro", "rs", "ru", "sr", "sv", "tr", "ua", "vi", "zh"
}
NON_ENGLISH_SUFFIXES = KNOWN_LANGUAGE_CODES - ENGLISH_LANGUAGE_CODES
READER_SOURCE_LANG_BY_ID = {}
READER_SOURCE_INDEX_LOADED = False
READER_SOURCE_INDEX_URL = "https://ponn.io/books/reader-index.json"
LEVEL_SUFFIXES = {"a1", "a2", "b1", "b2", "c1", "c2"}


def reader_id_looks_non_english(book_id):
    """True si el catálogo declara idioma original no inglés o el ID lo marca.

    reader-index.json es la fuente prioritaria: su sourceLang describe el
    idioma del texto original, mientras que lang=es de reader.html describe
    la traducción/interfaz y no debe confundirse con el idioma del libro.
    Para IDs de niveles que no aparecen en reader-index se usa el sufijo
    explícito, por ejemplo _de_a2, _it_b1, _pl o _summary_de.
    """
    value = str(book_id or "").strip().lower()
    if not value:
        return False

    source_lang = READER_SOURCE_LANG_BY_ID.get(value)
    if source_lang:
        normalized = str(source_lang).strip().lower().replace("_", "-")
        if normalized not in ENGLISH_LANGUAGE_CODES:
            return True
        return False

    # Variantes simplificadas: book_id_de_a2 / book_id_en_b1.
    match = re.search(r"_([a-z]{2,3}(?:-[a-z]{2,4})?)_(a1|a2|b1|b2|c1|c2)$", value)
    if match:
        code = match.group(1)
        return code in KNOWN_LANGUAGE_CODES and code not in ENGLISH_LANGUAGE_CODES

    # IDs con sufijo de idioma: ..._summary_de / ..._by_author_it.
    match = re.search(r"_([a-z]{2,3}(?:-[a-z]{2,4})?)$", value)
    if match:
        code = match.group(1)
        return code in KNOWN_LANGUAGE_CODES and code not in ENGLISH_LANGUAGE_CODES

    return False

def collect_english_audiobook_ids(obj):
    """Los IDs son las claves del índice; cada registro declara su lang."""
    if not isinstance(obj, dict):
        return set()
    return {
        str(book_id)
        for book_id, record in obj.items()
        if isinstance(record, dict)
        and str(record.get("lang", "")).strip().lower() in {"en", "eng", "english"}
    }


def extract_reader_records(obj):
    """Recoge book IDs y lengua cuando el catálogo la declara explícitamente."""
    records = {}
    def walk(node):
        if isinstance(node, dict):
            lang = node.get("lang") or node.get("language") or node.get("sourceLang")
            bid = node.get("bookId") or node.get("book_id") or node.get("id")
            if isinstance(bid, str) and isinstance(lang, str):
                records[bid] = lang.strip().lower()
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    walk(value)
    walk(obj)
    return records

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
            ui_lang = qs.get("lang", [None])[0]
            book_id = qs.get("id", [""])[0]
            if ui_lang != "es" or not book_id:
                return False
            if reader_id_looks_non_english(book_id):
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
# ROTACIÓN DE PROXIES
# ============================================================
def get_proxy_order():
    """Devuelve proxies activos; TrueProxies se desactiva antes de los 50 min."""
    global ACTIVE_PROXY_NAME
    now = time.monotonic()
    started = PROXY_STARTED_AT.get("trueproxies")
    if (started is not None and "trueproxies" not in PROXY_DISABLED
            and now - started >= TRUEPROXIES_MAX_SECONDS):
        PROXY_DISABLED.add("trueproxies")
        print("[Proxy] TrueProxies: rotación preventiva (48 min).")

    available = [name for name in PROXY_ORDER if name not in PROXY_DISABLED and CONTEXTS.get(name)]
    if available:
        if ACTIVE_PROXY_NAME not in available:
            ACTIVE_PROXY_NAME = available[0]
        CONTEXTS["proxy"] = CONTEXTS[ACTIVE_PROXY_NAME]
    return available


def mark_proxy_unavailable(name, reason):
    """Desactiva un proveedor durante esta ejecución tras un error de proxy/cuota."""
    global ACTIVE_PROXY_NAME
    if name not in PROXY_ORDER or name in PROXY_DISABLED:
        return
    PROXY_DISABLED.add(name)
    remaining = [item for item in PROXY_ORDER if item not in PROXY_DISABLED and CONTEXTS.get(item)]
    if remaining:
        ACTIVE_PROXY_NAME = remaining[0]
        CONTEXTS["proxy"] = CONTEXTS[ACTIVE_PROXY_NAME]
        print(f"[Proxy] {name} no disponible ({reason}). Siguiente: {ACTIVE_PROXY_NAME}.")
    else:
        CONTEXTS["proxy"] = None
        print(f"[Proxy] {name} no disponible ({reason}). Sin proxies restantes.")


def proxy_error_indicated(status, body="", exception_text=""):
    """Distingue errores de proxy/cuota de errores normales del sitio de destino."""
    text = (str(body) + " " + str(exception_text)).lower()
    if status in (402, 407):
        return True
    indicators = (
        "proxy authentication", "proxy auth", "insufficient balance",
        "out of balance", "no balance", "quota exceeded", "quota limit",
        "bandwidth limit", "traffic limit", "traffic exceeded",
        "subscription expired", "account disabled", "customer disabled",
        "no credit", "credits exhausted", "limit reached", "limit exceeded",
        "daily limit", "usage limit", "traffic exhausted", "proxy error",
    )
    return any(token in text for token in indicators)


def is_proxy_connection_exception(exc):
    text = str(exc).lower()
    indicators = (
        "proxy", "tunnel", "econnreset", "connection refused",
        "connection reset", "name_not_resolved", "err_tunnel",
        "net::err_proxy", "407", "authentication", "timeout", "timed out",
    )
    return any(token in text for token in indicators)


async def ensure_page_uses_active_proxy(page, attach_resource_listener):
    """Recrea la página en el contexto del proveedor activo cuando hubo rotación."""
    available = get_proxy_order()
    proxy_name = available[0] if available else "direct"
    if not CONTEXTS.get(proxy_name):
        return page
    current_name = PAGE_PROXY_NAMES.get(id(page))
    if current_name == proxy_name:
        return page
    try:
        await page.close()
    except Exception:
        pass
    context = CONTEXTS[proxy_name]
    new_page = await context.new_page()
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
    context.set_default_timeout(15000)
    PAGE_PROXY_NAMES[id(new_page)] = proxy_name
    attach_resource_listener(new_page, proxy_name)
    return new_page


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

    proxy_order = get_proxy_order()
    order = (["direct"] + proxy_order) if prefer_direct else (proxy_order + ["direct"])

    for ctx_name in order:
        ctx = CONTEXTS.get(ctx_name)
        if not ctx:
            continue
        if ctx_name in PROXY_ORDER:
            PROXY_STARTED_AT.setdefault(ctx_name, time.monotonic())
        try:
            response = await ctx.request.get(url, timeout=60000)
            if response.ok:
                body = await response.body()
                if body:
                    register_transfer(len(body), ctx_name)
                    return body, ctx_name
                continue

            try:
                response_text = await response.text()
            except Exception:
                response_text = ""

            if ctx_name in PROXY_ORDER and proxy_error_indicated(response.status, response_text):
                mark_proxy_unavailable(ctx_name, f"HTTP {response.status}/cuota")
                continue

            # 403/404/429 u otros errores del destino no se interpretan como
            # agotamiento del proxy: no desactivamos el proveedor por ello.
            if response.status in (403, 404, 410, 429):
                if ctx_name == "direct":
                    continue
                return None, None
        except Exception as exc:
            if ctx_name in PROXY_ORDER and is_proxy_connection_exception(exc):
                mark_proxy_unavailable(ctx_name, "error de conexión de proxy")
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

async def load_english_audiobook_index(tracker):
    global AUDIOBOOK_INDEX_LOADED, AUDIOBOOK_IDS_EN
    if AUDIOBOOK_INDEX_LOADED:
        return AUDIOBOOK_IDS_EN
    data, via = await fetch_bytes(AUDIOBOOK_INDEX_URL, prefer_direct=False)
    if not data:
        errors.append("ÍNDICE AUDIOBOOK | no se pudo descargar")
        AUDIOBOOK_INDEX_LOADED = True
        return set()
    save_fetched(data, AUDIOBOOK_INDEX_URL, tracker, content_type="application/json")
    try:
        parsed = json.loads(data)
        AUDIOBOOK_IDS_EN = collect_english_audiobook_ids(parsed)
        print(f"   [Audio] Índice: {len(AUDIOBOOK_IDS_EN)} registros en inglés ({via})")
    except Exception as exc:
        errors.append(f"ÍNDICE AUDIOBOOK PARSE | {exc}")
    AUDIOBOOK_INDEX_LOADED = True
    return AUDIOBOOK_IDS_EN

async def load_reader_source_language_index(tracker):
    """Carga reader-index.json y crea un mapa ID -> idioma original."""
    global READER_SOURCE_INDEX_LOADED, READER_SOURCE_LANG_BY_ID
    if READER_SOURCE_INDEX_LOADED:
        return READER_SOURCE_LANG_BY_ID

    data, via = await fetch_bytes(READER_SOURCE_INDEX_URL, prefer_direct=False)
    if not data:
        errors.append("READER INDEX | no se pudo descargar; se aplicará el filtro por sufijos")
        READER_SOURCE_INDEX_LOADED = True
        return READER_SOURCE_LANG_BY_ID

    save_fetched(data, READER_SOURCE_INDEX_URL, tracker, content_type="application/json")
    try:
        parsed = json.loads(data)
        if not isinstance(parsed, list):
            raise ValueError("reader-index.json no contiene una lista")

        source_map = {}
        for record in parsed:
            if not isinstance(record, dict):
                continue
            book_id = record.get("id")
            source_lang = record.get("sourceLang")
            if isinstance(book_id, str) and isinstance(source_lang, str) and source_lang.strip():
                source_map[book_id.strip().lower()] = source_lang.strip().lower()

        READER_SOURCE_LANG_BY_ID = source_map
        non_english = sum(1 for lang in source_map.values() if lang not in ENGLISH_LANGUAGE_CODES)
        english = sum(1 for lang in source_map.values() if lang in ENGLISH_LANGUAGE_CODES)
        print(f"   [Reader Index] {len(source_map)} idiomas originales declarados ({via}) | inglés={english} | otros={non_english}")
    except Exception as exc:
        errors.append(f"READER INDEX PARSE | {exc}")
        READER_SOURCE_LANG_BY_ID = {}

    READER_SOURCE_INDEX_LOADED = True
    return READER_SOURCE_LANG_BY_ID


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
    total_seconds = sum(int(ch.get("seconds", 0) or 0) for ch in chapters)
    print(f"      [Audio] {book_id}: {len(chapters)} capítulos | {total_seconds // 3600}h{(total_seconds % 3600) // 60:02d}m")
    for ch in chapters:
        file = ch.get("file")
        if not file:
            continue
        mp3_url = urljoin(base_url.rstrip("/") + "/", file)
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


async def load_reader_audio_index(tracker):
    """Carga el índice de audio sincronizado del lector."""
    global READER_AUDIO_INDEX_LOADED, READER_AUDIO_INDEX
    if READER_AUDIO_INDEX_LOADED:
        return READER_AUDIO_INDEX

    data, via = await fetch_bytes(READER_AUDIO_INDEX_URL, prefer_direct=False)
    if not data:
        errors.append("ÍNDICE READER AUDIO | no se pudo descargar")
        READER_AUDIO_INDEX_LOADED = True
        return {}

    save_fetched(data, READER_AUDIO_INDEX_URL, tracker, content_type="application/json")
    try:
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError("el índice no contiene un objeto JSON")
        READER_AUDIO_INDEX = parsed
        print(f"   [Reader Audio] Índice: {len(parsed)} libros ({via})")
    except Exception as exc:
        errors.append(f"ÍNDICE READER AUDIO PARSE | {exc}")
        READER_AUDIO_INDEX = {}
    READER_AUDIO_INDEX_LOADED = True
    return READER_AUDIO_INDEX


async def process_reader_audio(tracker, book_id):
    """Descarga MP3 y JSON de sincronización declarados por audio.json."""
    if not book_id or reader_id_looks_non_english(book_id):
        return 0, 0

    audio_index = await load_reader_audio_index(tracker)
    book = audio_index.get(book_id)
    if not isinstance(book, dict):
        return 0, 0

    parts = book.get("parts", [])
    if not isinstance(parts, list) or not parts:
        errors.append(f"READER AUDIO SIN PARTES | {book_id}")
        return 0, 0

    mp3_ok = 0
    timings_ok = 0
    print(f"      [Reader Audio] {book_id}: {len(parts)} partes | voz: {book.get('voice', 'n/d')}")

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            continue

        audio_ref = part.get("audio")
        timings_ref = part.get("timings")

        if isinstance(audio_ref, str) and audio_ref.strip():
            audio_url = urljoin(READER_AUDIO_INDEX_URL, audio_ref.strip())
            audio_data, audio_via = await fetch_bytes(audio_url, prefer_direct=True)
            if audio_data:
                saved = save_fetched(audio_data, audio_url, tracker, content_type="audio/mpeg")
                if saved:
                    mp3_ok += 1
                    print(f"         [MP3] Parte {part_number}/{len(parts)} ({audio_via})")
            else:
                errors.append(f"READER AUDIO MP3 FALLO | {book_id} | parte={part_number} | {audio_url}")

        if isinstance(timings_ref, str) and timings_ref.strip():
            timings_url = urljoin(READER_AUDIO_INDEX_URL, timings_ref.strip())
            timings_data, timings_via = await fetch_bytes(timings_url, prefer_direct=False)
            if timings_data:
                try:
                    parsed_timings = json.loads(timings_data)
                    if not isinstance(parsed_timings, list):
                        raise ValueError("el JSON de tiempos no es una lista")
                except Exception as exc:
                    errors.append(f"READER AUDIO TIMINGS INVÁLIDO | {book_id} | parte={part_number} | {exc}")
                    continue
                saved = save_fetched(timings_data, timings_url, tracker, content_type="application/json")
                if saved:
                    timings_ok += 1
            else:
                errors.append(f"READER AUDIO TIMINGS FALLO | {book_id} | parte={part_number} | {timings_url}")

    print(f"      [Reader Audio] {book_id}: MP3 {mp3_ok}/{len(parts)} | timings {timings_ok}/{len(parts)}")
    return mp3_ok, timings_ok


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
    if reader_id_looks_non_english(book_id):
        print(f"      [Reader] Omitido por idioma original no inglés: {book_id}")
        return 0

    # Descargar el audio del índice incluso si el TOC de texto no responde.
    audio_mp3_ok, audio_timings_ok = await process_reader_audio(tracker, book_id)

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

    print(f"      [Reader] {book_id}: {total_chunks} chunks + "
          f"{audio_mp3_ok} MP3 + {audio_timings_ok} timings")
    return total_chunks


# ============================================================
# LISTENER DE RECURSOS
# ============================================================
async def save_response_resource(response, site, tracker, via="proxy"):
    url = ""
    try:
        if tracker["limite_alcanzado"]: return
        resource_type = response.request.resource_type
        if resource_type in ("document", "websocket"): return
        if response.status not in (200, 206): return
        url = clean_url(response.url)
        if not url or not is_allowed_resource(url, site): return
        allowed = {"stylesheet", "script", "image", "font", "manifest", "media", "xhr", "fetch"}
        if resource_type not in allowed: return
        lock = resource_locks.setdefault(url, asyncio.Lock())
        async with lock:
            existing_file = url_to_file.get(url)
            if existing_file and Path(existing_file).is_file(): return
            headers = response.headers
            content_type = headers.get("content-type", "")
            mime = content_type.split(";", 1)[0].strip().lower()
            if resource_type in {"xhr", "fetch"}:
                # Solo respuestas estructuradas útiles; ignora telemetría, píxeles y texto arbitrario.
                if not (mime in {"application/json", "text/json", "application/ld+json", "text/plain"}):
                    return
                if "getbookchunk-ab3uqz2diq-ew.a.run.app" not in url and urlsplit(url).hostname not in site["hosts"]:
                    return
                if any(token in url.lower() for token in ("/cdn-cgi/rum", "google-analytics", "collect?", "telemetry")):
                    return
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
            register_transfer(len(data), via)
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
    for name in list(PROXY_ORDER) + ["direct"]:
        try:
            if CONTEXTS.get(name):
                await CONTEXTS[name].close()
        except Exception:
            pass
        if name in PROXY_ORDER:
            if name not in PROXY_DISABLED:
                CONTEXTS[name] = await browser.new_context(proxy=PROXY_CONFIGS[name], **base_kwargs)
                CONTEXTS[name].set_default_navigation_timeout(NAVIGATION_TIMEOUT)
                CONTEXTS[name].set_default_timeout(15000)
        else:
            CONTEXTS[name] = await browser.new_context(**base_kwargs)
    active = get_proxy_order()
    CONTEXTS["proxy"] = CONTEXTS[active[0]] if active else None
    print("   [Watchdog] Contextos recreados. Proxy activo:", active[0] if active else "ninguno")



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
            save_fetched(data, seed_url, tracker, content_type="application/json" if seed_url.lower().split("?")[0].endswith(".json") else None)
            print(f"   [Catálogo] {seed_url.split('/')[-1]} ({ctx_used})")

    # reader-index.json contiene sourceLang: idioma del texto original.
    # Debe cargarse antes de filtrar enlaces /reader.html?id=...&lang=es.
    await load_reader_source_language_index(tracker)
    await load_english_audiobook_index(tracker)

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

    available_proxies = get_proxy_order()
    initial_proxy = available_proxies[0] if available_proxies else "direct"
    if initial_proxy in PROXY_ORDER:
        PROXY_STARTED_AT.setdefault(initial_proxy, time.monotonic())
    else:
        print("[Proxy] Sin proxies residenciales restantes; navegación directa de respaldo.")
    ctx_proxy = CONTEXTS[initial_proxy]
    ctx_proxy.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
    ctx_proxy.set_default_timeout(15000)

    pages = [await ctx_proxy.new_page() for _ in range(PAGE_WORKERS)]

    def attach_resource_listener(page, proxy_name):
        PAGE_PROXY_NAMES[id(page)] = proxy_name
        def on_response(response):
            task = asyncio.create_task(save_response_resource(response, site, tracker, via=proxy_name))
            resource_tasks.add(task)
            task.add_done_callback(resource_tasks.discard)
        page.on("response", on_response)

    for page in pages:
        attach_resource_listener(page, initial_proxy)

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
                page = await ensure_page_uses_active_proxy(page, attach_resource_listener)

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
                    page = await ensure_page_uses_active_proxy(page, attach_resource_listener)
                    page_proxy = PAGE_PROXY_NAMES.get(id(page), ACTIVE_PROXY_NAME)
                    try:
                        response = await page.goto(current_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                    except PlaywrightTimeoutError:
                        if nav_attempt < NAVIGATION_RETRIES:
                            await page.wait_for_timeout(RETRY_DELAY_MS)
                            continue
                        navigation_timed_out = True
                        mark_proxy_unavailable(page_proxy, "timeout persistente de navegación")
                        page = await ensure_page_uses_active_proxy(page, attach_resource_listener)
                        response = None
                        break
                    if response is not None and response.status >= 400:
                        try:
                            response_body = await response.text()
                        except Exception:
                            response_body = ""
                        if proxy_error_indicated(response.status, response_body):
                            mark_proxy_unavailable(page_proxy, f"HTTP {response.status}/cuota en navegación")
                            page = await ensure_page_uses_active_proxy(page, attach_resource_listener)
                            if nav_attempt < NAVIGATION_RETRIES:
                                continue
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
                    book_id = current_url.split("#/b/")[1].split("&")[0].split("?")[0].strip()
                    english_ids = await load_english_audiobook_index(tracker)
                    if book_id in english_ids:
                        await process_audiobook(site, tracker, book_id)
                    else:
                        print(f"      [Audio] Omitido por idioma no confirmado como inglés: {book_id}")

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
                page_proxy = PAGE_PROXY_NAMES.get(id(page), ACTIVE_PROXY_NAME)
                if is_proxy_connection_exception(exc):
                    mark_proxy_unavailable(page_proxy, "error de conexión en navegación")
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
        "trueproxies_MB": format_mb(tracker["bytes_trueproxies"]),
        "scrapeops_MB": format_mb(tracker["bytes_scrapeops"]),
        "oxylabs_MB": format_mb(tracker["bytes_oxylabs"]),
        "directo_MB": format_mb(tracker["bytes_direct"]),
        "requests_proxy": tracker["requests_proxy"],
        "requests_trueproxies": tracker["requests_trueproxies"],
        "requests_scrapeops": tracker["requests_scrapeops"],
        "requests_oxylabs": tracker["requests_oxylabs"],
        "requests_direct": tracker["requests_direct"],
        "limite_MB": tracker["limite_mb"],
        "limite_alcanzado": tracker["limite_alcanzado"],
    }

    print("\n" + "-" * 55)
    print("FINALIZADO:", site["name"])
    print("Páginas guardadas:", pages_saved)
    print("Disco:", format_mb(tracker["bytes_guardados"]), "MB")
    print("Proxy total:", format_mb(tracker["bytes_proxy"]), "MB |", tracker["requests_proxy"], "req")
    for provider in PROXY_ORDER:
        print(f"{provider}:", format_mb(tracker["bytes_" + provider]), "MB |", tracker["requests_" + provider], "req")
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


def rewrite_audio_json_references():
    """Reescribe referencias del audio para que apunten a archivos guardados localmente."""
    rewritten = 0

    # Elegir la ruta local de cada lector, respetando sus query strings.
    reader_page_by_id = {}
    for original_url, local_path in list(url_to_file.items()):
        try:
            parts = urlsplit(original_url)
            if parts.hostname == "ponn.io" and parts.path == "/reader.html":
                book_id = parse_qs(parts.query).get("id", [""])[0]
                if book_id:
                    reader_page_by_id[book_id] = Path(local_path)
        except Exception:
            pass
    default_reader_page = ROOT / "ponn.io" / "reader.html"

    reader_index_path = url_to_file.get(clean_url(READER_AUDIO_INDEX_URL))
    if reader_index_path and Path(reader_index_path).is_file():
        try:
            index_data = json.loads(Path(reader_index_path).read_text(encoding="utf-8"))
            if isinstance(index_data, dict):
                for book_id, book in index_data.items():
                    if not isinstance(book, dict):
                        continue
                    page_path = reader_page_by_id.get(book_id, default_reader_page)
                    for part in book.get("parts", []):
                        if not isinstance(part, dict):
                            continue
                        audio_ref = part.get("audio")
                        if isinstance(audio_ref, str):
                            audio_url = urljoin(READER_AUDIO_INDEX_URL, audio_ref)
                            target = find_local_target(audio_url)
                            if target:
                                part["audio"] = relative_target(page_path, target)
                                rewritten += 1
                        timings_ref = part.get("timings")
                        if isinstance(timings_ref, str):
                            timings_url = urljoin(READER_AUDIO_INDEX_URL, timings_ref)
                            target = find_local_target(timings_url)
                            if target:
                                part["timings"] = relative_target(page_path, target)
                                rewritten += 1
                save_json(Path(reader_index_path), index_data)
        except Exception as exc:
            errors.append(f"REESCRITURA READER AUDIO JSON | {exc}")

    # Reescribir las portadas del índice de audiolibros.
    audiobook_page = url_to_file.get(clean_url("https://ponn.io/audiobooks/"), ROOT / "ponn.io" / "audiobooks" / "index.html")
    audiobook_index_path = url_to_file.get(clean_url(AUDIOBOOK_INDEX_URL))
    if audiobook_index_path and Path(audiobook_index_path).is_file():
        try:
            index_data = json.loads(Path(audiobook_index_path).read_text(encoding="utf-8"))
            if isinstance(index_data, dict):
                for book in index_data.values():
                    if isinstance(book, dict) and isinstance(book.get("cover"), str):
                        cover_url = urljoin("https://ponn.io/", book["cover"])
                        target = find_local_target(cover_url)
                        if target:
                            book["cover"] = relative_target(audiobook_page, target)
                            rewritten += 1
                save_json(Path(audiobook_index_path), index_data)
        except Exception as exc:
            errors.append(f"REESCRITURA AUDIOBOOK INDEX JSON | {exc}")

    # Los JSON individuales de /audiobooks/ contienen baseUrl + chapters[].file.
    book_json_items = []
    for original_url, local_path in list(url_to_file.items()):
        try:
            parts = urlsplit(original_url)
            if (parts.hostname == "ponn.io" and
                    parts.path.startswith("/audiobooks/data/books/") and
                    parts.path.endswith(".json") and Path(local_path).is_file()):
                book_json_items.append((original_url, Path(local_path)))
        except Exception:
            pass

    for original_url, local_path in book_json_items:
        try:
            meta = json.loads(local_path.read_text(encoding="utf-8"))
            base_url = meta.get("baseUrl", "")
            chapters = meta.get("chapters", [])
            if not isinstance(chapters, list):
                continue
            for chapter in chapters:
                if not isinstance(chapter, dict) or not chapter.get("file"):
                    continue
                mp3_url = urljoin(base_url.rstrip("/") + "/", str(chapter["file"]))
                target = find_local_target(mp3_url)
                if target:
                    chapter["file"] = relative_target(audiobook_page, target)
                    rewritten += 1
                else:
                    # No falsear disponibilidad: se conserva URL completa para el capítulo no descargado.
                    chapter["file"] = mp3_url
            # Cada chapter.file queda con ruta local o URL absoluta de respaldo.
            meta["baseUrl"] = ""
            save_json(local_path, meta)
        except Exception as exc:
            errors.append(f"REESCRITURA AUDIOBOOK JSON | {original_url} | {exc}")

    print(f"   [Audio offline] Referencias locales reescritas: {rewritten}")


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
        "reader_index_ids_con_sourceLang": len(READER_SOURCE_LANG_BY_ID),
        "reader_index_idiomas_originales": sorted(set(READER_SOURCE_LANG_BY_ID.values())),
    }
    Path("informe-descarga.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("errores-descarga.txt").write_text("\n".join(errors), encoding="utf-8")
    print("\n========== INFORME ==========")
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    METADATA.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("DESCARGADOR PONN — UI español + contenido original inglés")
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

        for provider in PROXY_ORDER:
            CONTEXTS[provider] = await browser.new_context(proxy=PROXY_CONFIGS[provider], **base_kwargs)
            CONTEXTS[provider].set_default_navigation_timeout(NAVIGATION_TIMEOUT)
            CONTEXTS[provider].set_default_timeout(15000)
            print(f"[Contexto] proxy residencial {provider} configurado")
        CONTEXTS["proxy"] = CONTEXTS["trueproxies"]
        CONTEXTS["direct"] = await browser.new_context(**base_kwargs)
        print("[Contexto] directo de respaldo")

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
    print("\n===== REESCRITURA DE REFERENCIAS DE AUDIO JSON =====")
    rewrite_audio_json_references()
    print("\n===== ÍNDICE OFFLINE =====")
    create_offline_index()
    audit_artifact_paths()
    save_maps_now()
    create_report()
    print("\n===== FIN =====")


if __name__ == "__main__":
    asyncio.run(main())
