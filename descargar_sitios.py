import os
import asyncio
import re
import json
import hashlib
from pathlib import Path
from collections import deque
from functools import lru_cache
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

SITES = [
    #{"name": "Codica Basicos", "start": "https://basicos.codica.la/", "hosts": {"basicos.codica.la"}, "max_total_mb": 4096},
    #{"name": "W3Docs", "start": "https://es.w3docs.com/", "hosts": {"es.w3docs.com"}, "max_total_mb": 2048},
    #{"name": "eLearner", "start": "https://elearner.app/es/", "hosts": {"elearner.app"}, "allowed_paths": ["/es/"], "max_total_mb": 4096},
    #{"name": "DevSchool", "start": "https://devschool.es/", "hosts": {"devschool.es"}, "max_total_mb": 4096},
    #{"name": "CodeRacha", "start": "https://carloswilches.com/", "hosts": {"carloswilches.com", "www.carloswilches.com"}, "max_total_mb": 4096},
    #{"name": "GameCode", "start": "https://gamecode.me/", "hosts": {"gamecode.me"}, "resource_hosts": {"assets.gamecode.me", "cdn.jsdelivr.net", "raw.githubusercontent.com"}, "max_total_mb": 4096},
    #{"name": "Aprende Web desde Cero", "start": "https://cacg-code.github.io/web-desde-cero/", "hosts": {"cacg-code.github.io"}, "resource_hosts": {"cdn.jsdelivr.net"}, "max_total_mb": 4096},
    #{"name": "Curso Linux Ciberaula", "start": "https://linux.ciberaula.com/curso/", "hosts": {"linux.ciberaula.com", "www.linux.ciberaula.com"}, "max_total_mb": 4096},
    #{"name": "Tutoriales Programacion Ya", "start": "https://www.tutorialesprogramacionya.com/", "hosts": {"www.tutorialesprogramacionya.com", "tutorialesprogramacionya.com"}, "max_total_mb": 4096},
    #{"name": "Aprende JavaScript", "start": "https://www.aprendejavascript.dev/", "hosts": {"www.aprendejavascript.dev", "aprendejavascript.dev"}, "max_total_mb": 4096},
    #{"name": "Programa de Ciberseguridad Moderna", "start": "https://vladimiracunadev-create.github.io/modern-cybersecurity-program/", "hosts": {"vladimiracunadev-create.github.io"}, "max_total_mb": 4096},
    #{"name": "Apuntes DAM", "start": "https://apuntes-dam.github.io/apuntes-lenguajes/", "hosts": {"apuntes-dam.github.io"}, "max_total_mb": 4096},
    #{"name": "UJACKING", "start": "https://ujacking.github.io/", "hosts": {"ujacking.github.io"}, "max_total_mb": 4096},
    #{"name": "JavaScript.info Español", "start": "https://es.javascript.info/", "hosts": {"es.javascript.info"}, "max_total_mb": 4096},
    #{"name": "Python Práctico", "start": "https://pythonpanama.github.io/pythonpractico/", "hosts": {"pythonpanama.github.io"}, "max_total_mb": 4096},
    #{"name": "Brian27A", "start": "https://brian27a.github.io/", "hosts": {"brian27a.github.io"}, "max_total_mb": 4096},
    #{"name": "Apuntes PSP", "start": "https://champunes.github.io/ApuntesPSP/", "hosts": {"champunes.github.io"}, "max_total_mb": 4096},
    #{"name": "Makigas", "start": "https://www.makigas.es/", "hosts": {"www.makigas.es"}, "max_total_mb": 4096},
    #{"name": "Duobi Cursos", "start": "https://www.duobi.tech/cursos", "hosts": {"www.duobi.tech"}, "max_total_mb": 4096},
    #{"name": "Pro Git Español", "start": "https://git-scm.com/book/es/v2", "hosts": {"git-scm.com"}, "max_total_mb": 4096},
    #{"name": "Full Stack Open Español", "start": "https://fullstackopen.com/es/", "hosts": {"fullstackopen.com"}, "max_total_mb": 4096},
    #{"name": "MDN Web Docs - Español prioritario + inglés complementario", "start": "https://developer.mozilla.org/es/", "starts": ["https://developer.mozilla.org/es/", "https://developer.mozilla.org/en-US/"], "hosts": {"developer.mozilla.org"}, "allowed_paths": ["/es/", "/en-US/"], "language_strategy": "mdn_es_then_en", "max_total_mb": 10000},
    #{"name": "Curso Completo de Ciberseguridad - Antinori Ariel", "start": "https://github.com/antinoriariel/curso_ciberseguridad", "hosts": {"github.com"}, "allowed_paths": ["/antinoriariel/curso_ciberseguridad"], "resource_hosts": {"raw.githubusercontent.com"}, "max_total_mb": 5000},
    {
        "name": "PolyglotReader - Ingles y Espanol",
        "start": "https://polyglotreader.com/",
        "starts": [
            "https://polyglotreader.com/",
            "https://polyglotreader.com/catalog",
            "https://polyglotreader.com/read-spanish-books",
            "https://polyglotreader.com/es/faq"
        ],
        "hosts": {"polyglotreader.com", "www.polyglotreader.com"},
        "max_total_mb": 10000
    },

    {
        "name": "Storyteller - Ingles y Espanol",
        "start": "https://storytellerenglish.com/es/stories/",
        "starts": [
            "https://storytellerenglish.com/es/stories/",
            "https://storytellerenglish.com/en/stories/",
            "https://storytellerenglish.com/es/",
            "https://storytellerenglish.com/en/"
        ],
        "hosts": {"storytellerenglish.com", "www.storytellerenglish.com"},
        "max_total_mb": 10000
    },
    
]

ROOT = Path("salida")
METADATA = ROOT / "_metadata"
URL_MAP_FILE = METADATA / "url-map.json"
PAGE_MAP_FILE = METADATA / "page-map.json"

MAX_PAGES_PER_SITE = 500000
MAX_DEPTH = 35
NAVIGATION_TIMEOUT = 45000
NETWORK_IDLE_TIMEOUT = 2500
WAIT_AFTER_LOAD_MS = 300
PAGE_WORKERS = 6
NAVIGATION_RETRIES = 2
MAX_PAGE_ATTEMPTS = 3
RESOURCE_BODY_RETRIES = 2
RETRY_DELAY_MS = 800
LAZY_SCROLL_STEP_MS = 80
MAX_LAZY_SCROLL_STEPS = 30
MAX_RESOURCE_SIZE = 700 * 1024 * 1024

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
api_responses = []
api_response_locks = {}
errors = []
visited_by_site = {}
site_storage = {}
mdn_language_stats = {}

# Reservas de rutas para impedir colisiones en sistemas de archivos
# que no distinguen mayúsculas/minúsculas (Windows y algunos ZIP/artifacts).
local_path_owners = {}
path_sanitizations = 0
path_collisions = 0


def create_site_tracker(site):
    limit_mb = site.get("max_total_mb", 4096)
    return {
        "limite_mb": limit_mb,
        "limite_bytes": int(limit_mb * 1024 * 1024),
        "bytes_guardados": 0,
        "archivos": {},
        "api_firmas": set(),
        "api_guardadas": 0,
        "api_omitidas": 0,
        "limite_alcanzado": False
    }


def format_mb(byte_count):
    return round(byte_count / 1024 / 1024, 2)


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
            print(
                "\n[ LIMITE ALCANZADO ]"
                f"\nArchivo: {description}"
                f"\nTamaño: {format_mb(new_size)} MB"
                f"\nGuardado: {format_mb(tracker['bytes_guardados'])} MB"
                f"\nLímite: {tracker['limite_mb']} MB"
            )
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    tracker["archivos"][key] = new_size
    tracker["bytes_guardados"] += difference

    return True


def clean_url(url):
    """Normaliza lo seguro sin alterar parámetros de consulta funcionales.

    Elimina fragmentos, uniforma esquema/host y puertos por defecto, y
    normaliza la escritura hexadecimal de escapes URL. No elimina parámetros
    ni cambia mayúsculas de rutas porque algunos sitios distinguen esos valores.
    """
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
        if port and not ((scheme == "http" and port == 80) or
                         (scheme == "https" and port == 443)):
            hostname = f"{hostname}:{port}"

        # Conserva userinfo si la URL lo contiene, aunque normalmente no se usa.
        userinfo = ""
        if "@" in parts.netloc:
            userinfo = parts.netloc.rsplit("@", 1)[0] + "@"
        netloc = userinfo + hostname

        path = parts.path or "/"
        # Decodifica únicamente caracteres URL no reservados (p. ej. %7E -> ~).
        def normalize_escape(match):
            value = int(match.group(1), 16)
            char = chr(value)
            if ("A" <= char <= "Z" or "a" <= char <= "z" or
                    "0" <= char <= "9" or char in "-._~"):
                return char
            return "%" + match.group(1).upper()

        path = re.sub(r"%([0-9a-fA-F]{2})", normalize_escape, path)
        query = re.sub(r"%([0-9a-fA-F]{2})", lambda m: "%" + m.group(1).upper(), parts.query)

        return urlunsplit((scheme, netloc, path, query, ""))

    except Exception:
        return ""


def is_internal(url, site):
    try:
        return urlsplit(url).hostname in site["hosts"]
    except Exception:
        return False


def is_allowed_page(url, site):
    """
    Comprueba si una URL puede descargarse como página HTML.

    Si el sitio tiene allowed_paths, solamente permite las rutas
    que coincidan con esos prefijos.

    Para eLearner se permite /es/, pero se excluyen las demás
    versiones lingüísticas.
    """

    if not is_internal(url, site):
        return False

    try:
        path = urlsplit(url).path or "/"
        for excluded in site.get("exclude_paths", []):
            excluded = excluded.rstrip("/") or "/"
            if path == excluded or path.startswith(excluded + "/"):
                return False
    except Exception:
        return False

    allowed_paths = site.get("allowed_paths")

    if not allowed_paths:
        return True

    try:
        path = urlsplit(url).path or "/"

        for prefix in allowed_paths:
            prefix = prefix.rstrip("/") or "/"

            if prefix == "/":
                return True

            if path == prefix or path.startswith(prefix + "/"):
                return True

        return False

    except Exception:
        return False


def mdn_spanish_counterpart(english_url, html=""):
    """Encuentra la traducción es anunciada por MDN o deriva su ruta equivalente."""
    try:
        parts = urlsplit(clean_url(english_url))
        if parts.hostname != "developer.mozilla.org":
            return None
        path = parts.path or "/"
        if path == "/en-US/":
            return "https://developer.mozilla.org/es/"
        if not path.startswith("/en-US/docs/"):
            return None
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for link in soup.find_all("link", href=True):
                rel = link.get("rel", [])
                rel = [str(x).lower() for x in (rel if isinstance(rel, list) else [rel])]
                hreflang = str(link.get("hreflang", "")).lower()
                if "alternate" in rel and hreflang in ("es", "es-es", "es-mx"):
                    candidate = clean_url(urljoin(english_url, link["href"]))
                    cp = urlsplit(candidate)
                    if cp.hostname == "developer.mozilla.org" and cp.path.startswith("/es/docs/"):
                        return urlunsplit((cp.scheme, cp.netloc, cp.path, "", ""))
        spanish_path = "/es/docs/" + path[len("/en-US/docs/"):]
        return urlunsplit((parts.scheme, parts.netloc, spanish_path, "", ""))
    except Exception:
        return None


async def mdn_translation_status(context, spanish_url, cache, locks):
    """True=traducción española confirmada; False=no existe; None=no verificable."""
    spanish_url = clean_url(spanish_url)
    if not spanish_url:
        return None
    if spanish_url in cache:
        return cache[spanish_url]
    lock = locks.setdefault(spanish_url, asyncio.Lock())
    async with lock:
        if spanish_url in cache:
            return cache[spanish_url]
        result = None
        for attempt in range(1, 3):
            try:
                response = await context.request.get(
                    spanish_url, timeout=NAVIGATION_TIMEOUT,
                    headers={"Accept-Language": "es-ES,es;q=0.9"}
                )
                status = response.status
                if status in (404, 410):
                    result = False
                    break
                if status == 200:
                    body = await response.text()
                    soup = BeautifulSoup(body, "html.parser")
                    lang = str(soup.html.get("lang", "")).lower() if soup.html else ""
                    final_parts = urlsplit(clean_url(response.url))
                    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
                    missing = any(x in title for x in ("page not found", "página no encontrada", "404"))
                    result = bool(lang.startswith("es") and final_parts.path.startswith("/es/") and not missing)
                    break
                if status < 500 and status != 429:
                    result = False
                    break
            except Exception:
                pass
            if attempt < 2:
                await asyncio.sleep(0.5 * attempt)
        cache[spanish_url] = result
        return result


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

    signatures = (
        b"<!doctype html",
        b"<html",
        b"<head",
        b"<body",
        b"<title",
        b"<meta",
        b"<?xml"
    )

    return any(sample.startswith(x) for x in signatures)


def looks_like_css(data):
    if not data:
        return False

    sample = data[:2048].lstrip().lower()

    return b"{" in sample and (
        b"body" in sample
        or b"html" in sample
        or b"@" in sample
        or b":" in sample
    )


def detect_extension(url, content_type, data):
    mime = (content_type or "").split(";")[0].strip().lower()
    path_ext = Path(urlsplit(url).path).suffix.lower()

    if looks_like_html(data):
        return ".html"

    if data.startswith(b"%PDF-"):
        return ".pdf"

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"

    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"

    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"

    if data.startswith(b"PK\x03\x04"):
        return path_ext if path_ext in (
            ".zip", ".docx", ".xlsx", ".pptx", ".apk"
        ) else ".zip"

    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4"

    if data.startswith(b"wOFF"):
        return ".woff"

    if data.startswith(b"wOF2"):
        return ".woff2"

    if data.startswith(b"\x00\x01\x00\x00"):
        return ".ttf"

    if mime in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime]

    if mime == "text/css" or looks_like_css(data):
        return ".css"

    if path_ext:
        return path_ext

    return ".bin"


def sanitize_path_component(component):
    """Convierte un segmento URL en un nombre compatible con Windows/ZIP."""
    global path_sanitizations
    original = str(component)
    value = original

    # Codifica caracteres prohibidos por NTFS y caracteres de control.
    def replace_forbidden(match):
        return "_u" + format(ord(match.group(0)), "04X") + "_"

    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', replace_forbidden, value)
    # Windows no admite puntos ni espacios al final de un componente.
    value = value.rstrip(" .")
    if not value:
        value = "_"

    # Nombres de dispositivo reservados en Windows, incluso con extensión.
    stem = value.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    if stem in reserved:
        value = "_" + value

    # Evita componentes excesivamente largos y conserva una identidad estable.
    if len(value) > 180:
        digest = hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest()[:12]
        value = value[:160] + "__" + digest

    if value != original:
        path_sanitizations += 1
    return value


def _path_key(path):
    """Clave relativa, normalizada y case-insensitive para comparar rutas."""
    try:
        relative = Path(path).relative_to(ROOT)
    except ValueError:
        relative = Path(path)
    return relative.as_posix().casefold()


def _reserve_path(path, url):
    """Reserva una ruta. Si colisiona, añade un hash determinista al archivo."""
    global path_collisions
    path = Path(path)
    key = _path_key(path)
    owner = local_path_owners.get(key)

    if owner is None and not path.exists():
        local_path_owners[key] = url
        return path
    if owner == url:
        return path

    # Si existe físicamente pero no conocemos su URL, tampoco la sobrescribimos.
    digest = hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest()[:12]
    candidate = path.with_name(path.stem + "__" + digest + path.suffix)
    candidate_key = _path_key(candidate)
    counter = 1
    while ((candidate_key in local_path_owners and local_path_owners[candidate_key] != url)
           or (candidate.exists() and local_path_owners.get(candidate_key) != url)):
        candidate = path.with_name(path.stem + "__" + digest + "_" + str(counter) + path.suffix)
        candidate_key = _path_key(candidate)
        counter += 1

    local_path_owners[candidate_key] = url
    path_collisions += 1
    print(f"[RUTA EN COLISIÓN] {path} -> {candidate}")
    return candidate


def safe_local_path(url, force_html=False, content_type=None):
    parts = urlsplit(url)
    normalized_url = clean_url(url) or str(url)

    # Si esta URL ya tiene una ubicación registrada, se conserva exactamente.
    existing = url_to_file.get(normalized_url)
    if existing and Path(existing).exists():
        return Path(existing)

    host = parts.hostname or "desconocido"
    web_path = unquote(parts.path or "/")
    if web_path.endswith("/"):
        web_path += "index"

    raw_parts = [host] + [part for part in web_path.lstrip("/").split("/") if part]
    safe_parts = [sanitize_path_component(part) for part in raw_parts]
    relative = Path(*safe_parts)

    mime = (content_type or "").split(";")[0].strip().lower()
    suffix = relative.suffix.lower()

    if force_html or mime in HTML_MIME:
        if suffix in ("", ".bin", ".php", ".asp", ".aspx", ".jsp"):
            relative = relative.with_suffix(".html")
    elif not suffix:
        extension = MIME_EXTENSIONS.get(mime, ".bin")
        relative = relative.with_suffix(extension)

    if parts.query:
        relative = relative.with_name(
            relative.stem + query_suffix(parts.query) + relative.suffix
        )

    return _reserve_path(ROOT / relative, normalized_url)


def register_url(url, path, is_page=False):
    url = clean_url(url)

    if not url:
        return

    path = Path(path)
    url_to_file[url] = path
    # Una ruta local puede representar varios alias (por ejemplo, una página
    # inglesa que apunta a su traducción española). No sobrescribir el URL
    # canónico utilizado para resolver recursos relativos del archivo.
    file_to_url.setdefault(str(path), url)
    local_path_owners.setdefault(_path_key(path), url)

    if is_page:
        page_urls.add(url)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def load_previous_maps():
    if URL_MAP_FILE.exists():
        try:
            data = json.loads(URL_MAP_FILE.read_text(encoding="utf-8"))

            for url, relative in data.items():
                path = ROOT / relative

                if path.exists():
                    register_url(url, path)

            print("URLs recuperadas del mapa anterior:", len(url_to_file))

        except Exception as exc:
            print("Error recuperando mapa anterior:", exc)

    if PAGE_MAP_FILE.exists():
        try:
            data = json.loads(PAGE_MAP_FILE.read_text(encoding="utf-8"))

            for url in data:
                page_urls.add(clean_url(url))

        except Exception as exc:
            print("Error recuperando mapa de páginas:", exc)


def infer_old_pages():
    if not ROOT.exists():
        return

    known_hosts = set()

    for site in SITES:
        known_hosts.update(site["hosts"])

    for old_file in list(ROOT.rglob("*.bin")):
        if "_metadata" in old_file.parts:
            continue

        try:
            data = old_file.read_bytes()

            if not looks_like_html(data):
                continue

            new_file = old_file.with_suffix(".html")

            if new_file.exists():
                continue

            old_file.rename(new_file)
            print("HTML recuperado:", old_file, "->", new_file)

        except Exception as exc:
            errors.append(f"RECUPERAR BIN | {old_file} | {exc}")

    for html_file in ROOT.rglob("*.html"):
        if "_metadata" in html_file.parts:
            continue

        try:
            relative = html_file.relative_to(ROOT)
        except ValueError:
            continue

        if len(relative.parts) < 2:
            continue

        host = relative.parts[0]

        if host not in known_hosts:
            continue

        web_path = "/" + "/".join(relative.parts[1:])

        if web_path.lower() == "/index.html":
            register_url(
                "https://" + host + "/",
                html_file,
                is_page=True
            )
            register_url(
                "https://" + host + "/index.html",
                html_file,
                is_page=True
            )

        elif web_path.lower().endswith("/index.html"):
            directory = web_path[:-10]

            if not directory.endswith("/"):
                directory += "/"

            register_url(
                "https://" + host + directory,
                html_file,
                is_page=True
            )
            register_url(
                "https://" + host + web_path,
                html_file,
                is_page=True
            )

        else:
            inferred_path = (
                web_path[:-5]
                if web_path.lower().endswith(".html")
                else web_path
            )

            register_url(
                "https://" + host + inferred_path,
                html_file,
                is_page=True
            )
            register_url(
                "https://" + host + inferred_path + ".html",
                html_file,
                is_page=True
            )


def normalize_existing_artifact_paths():
    """Renombra archivos heredados para eliminar nombres inválidos y colisiones."""
    global path_collisions
    if not ROOT.exists():
        return

    files = sorted((p for p in ROOT.rglob("*") if p.is_file()), key=lambda p: p.as_posix().casefold())
    seen = {}
    rebuilt_owners = {}
    renamed = 0

    for old_path in files:
        try:
            relative = old_path.relative_to(ROOT)
        except ValueError:
            continue

        safe_relative = Path(*(sanitize_path_component(part) for part in relative.parts))
        target = ROOT / safe_relative
        key = safe_relative.as_posix().casefold()
        old_owner = file_to_url.get(str(old_path), "legacy:" + relative.as_posix())

        collision = key in seen and seen[key] != safe_relative.as_posix()
        collision = collision or (target.exists() and target != old_path)
        if collision:
            digest = hashlib.sha256((old_owner + "|" + relative.as_posix()).encode("utf-8", errors="replace")).hexdigest()[:12]
            target = target.with_name(target.stem + "__" + digest + target.suffix)
            key = target.relative_to(ROOT).as_posix().casefold()
            counter = 1
            while key in seen or (target.exists() and target != old_path):
                target = target.with_name(target.stem + "_" + str(counter) + target.suffix)
                key = target.relative_to(ROOT).as_posix().casefold()
                counter += 1
            path_collisions += 1

        if target != old_path:
            target.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(target)
            renamed += 1
            for mapped_url, mapped_path in list(url_to_file.items()):
                if Path(mapped_path) == old_path:
                    url_to_file[mapped_url] = target
            if str(old_path) in file_to_url:
                file_to_url[str(target)] = file_to_url.pop(str(old_path))

        seen[key] = target.relative_to(ROOT).as_posix()
        rebuilt_owners[key] = old_owner

    local_path_owners.clear()
    local_path_owners.update(rebuilt_owners)
    if renamed:
        print(f"Rutas heredadas normalizadas: {renamed}")


def api_request_identity(request):
    """Identidad estable: URL + método + hash del cuerpo enviado."""
    method = (request.method or "GET").upper()
    post_data = request.post_data or ""
    body_hash = hashlib.sha256(post_data.encode("utf-8", errors="replace")).hexdigest()[:16] if post_data else "no-body"
    identity = f"{method}\n{clean_url(request.url)}\n{body_hash}"
    digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
    return method, body_hash, digest


async def save_api_response(response, site, tracker):
    """Registra y guarda respuestas de fetch/XHR de tipo textual."""
    request = response.request
    url = clean_url(response.url)
    method, body_hash, digest = api_request_identity(request)
    headers = response.headers
    content_type = headers.get("content-type", "")
    mime = content_type.split(";")[0].strip().lower()
    status = response.status
    entry = {
        "sitio": site["name"],
        "url": url,
        "metodo": method,
        "tipo_recurso": request.resource_type,
        "estado_http": status,
        "content_type": content_type,
        "tamano_bytes": None,
        "hash_cuerpo_solicitud": body_hash,
        "archivo": None,
        "guardado": False,
        "motivo_omision": None,
    }

    # Se registra incluso cuando el host/MIME/estado no permite guardar el cuerpo.
    api_responses.append(entry)

    if status not in (200, 206):
        entry["motivo_omision"] = "estado_http_no_descargable"
        tracker["api_omitidas"] += 1
        return
    if not url or not is_allowed_resource(url, site):
        entry["motivo_omision"] = "host_fuera_de_hosts_permitidos"
        tracker["api_omitidas"] += 1
        return

    is_json = mime == "application/json" or mime.endswith("+json")
    is_xml = mime in ("application/xml", "text/xml") or mime.endswith("+xml")
    is_text = mime.startswith("text/")
    if not (is_json or is_xml or is_text):
        entry["motivo_omision"] = "mime_no_textual"
        tracker["api_omitidas"] += 1
        return

    lock = api_response_locks.setdefault(digest, asyncio.Lock())
    async with lock:
        if digest in tracker["api_firmas"]:
            entry["motivo_omision"] = "respuesta_duplicada"
            tracker["api_omitidas"] += 1
            return

        length = headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_RESOURCE_SIZE:
                    entry["motivo_omision"] = "supera_limite_por_archivo"
                    tracker["api_omitidas"] += 1
                    return
            except ValueError:
                pass

        try:
            data = await response.body()
        except Exception as exc:
            entry["motivo_omision"] = "error_leyendo_respuesta"
            tracker["api_omitidas"] += 1
            errors.append(f"API BODY | {url} | {exc}")
            return

        entry["tamano_bytes"] = len(data)
        if not data:
            entry["motivo_omision"] = "respuesta_vacia"
            tracker["api_omitidas"] += 1
            return
        if len(data) > MAX_RESOURCE_SIZE:
            entry["motivo_omision"] = "supera_limite_por_archivo"
            tracker["api_omitidas"] += 1
            return
        if tracker["limite_alcanzado"]:
            entry["motivo_omision"] = "limite_total_del_sitio_alcanzado"
            tracker["api_omitidas"] += 1
            return

        extension = detect_extension(url, content_type, data)
        api_dir = ROOT / (urlsplit(url).hostname or "host_desconocido") / "_api"
        filename = f"{method.lower()}_{digest[:24]}{extension}"
        path = api_dir / filename
        if not save_limited_file(path, data, tracker, description=f"API {method} {url}"):
            entry["motivo_omision"] = "limite_total_del_sitio_alcanzado"
            tracker["api_omitidas"] += 1
            return

        tracker["api_firmas"].add(digest)
        tracker["api_guardadas"] += 1
        entry["archivo"] = str(path).replace("\\", "/")
        entry["guardado"] = True


async def save_response_resource(response, site, tracker):
    """Guarda recursos con bloqueo por URL: una descarga duplicada espera a la
    primera y, si esta falla, puede intentar guardar su propia respuesta.
    """
    url = ""

    try:
        if tracker["limite_alcanzado"]:
            return

        resource_type = response.request.resource_type
        if resource_type in ("document", "websocket"):
            return

        url = clean_url(response.url)

        # Registrar también errores HTTP y hosts externos para poder auditarlos.
        # save_api_response decide si el cuerpo puede guardarse.
        if resource_type in ("xhr", "fetch"):
            await save_api_response(response, site, tracker)
            return

        if response.status not in (200, 206):
            return
        if not url or not is_allowed_resource(url, site):
            return

        allowed = {"stylesheet", "script", "image", "font", "manifest", "media"}
        if resource_type not in allowed:
            return

        # El lock evita que una solicitud duplicada se descarte si la primera falla.
        lock = resource_locks.setdefault(url, asyncio.Lock())
        async with lock:
            existing_file = url_to_file.get(url)
            if existing_file and Path(existing_file).is_file():
                return

            headers = response.headers
            content_type = headers.get("content-type", "")
            length = headers.get("content-length")
            if length:
                try:
                    if int(length) > MAX_RESOURCE_SIZE:
                        print("Recurso individual demasiado grande:", url)
                        return
                except ValueError:
                    pass

            data = None
            for body_attempt in range(1, RESOURCE_BODY_RETRIES + 1):
                try:
                    data = await response.body()
                    if data:
                        break
                except Exception as body_exc:
                    if body_attempt >= RESOURCE_BODY_RETRIES:
                        raise body_exc
                    await asyncio.sleep(0.25 * body_attempt)

            if not data or len(data) > MAX_RESOURCE_SIZE or looks_like_html(data):
                return

            extension = detect_extension(url, content_type, data)
            path = safe_local_path(url, content_type=content_type)
            if path.suffix.lower() != extension:
                path = path.with_suffix(extension)

            if not save_limited_file(path, data, tracker, description=url):
                return

            register_url(url, path)
            asset_urls.add(url)

    except Exception as exc:
        errors.append(f"RECURSO | {url or response.url} | {exc}")


async def extract_page_links(page, current_url, site):
    results = []

    try:
        elements = await page.locator(
            "a[href], area[href], iframe[src], frame[src], "
            "source[src], video[src], audio[src], link[href]"
        ).evaluate_all("""
            elements => elements.map(el => ({
                href: el.href || el.getAttribute('href') || '',
                src: el.src || el.getAttribute('src') || ''
            }))
        """)

        for item in elements:
            value = item.get("href") or item.get("src")

            if not value:
                continue

            value = value.strip()

            if value.startswith(IGNORED_SCHEMES):
                continue

            absolute = clean_url(urljoin(current_url, value))

            if not absolute:
                continue

            if not is_allowed_page(absolute, site):
                continue

            results.append(absolute)

    except Exception as exc:
        errors.append(f"EXTRACCION ENLACES | {current_url} | {exc}")

    return list(dict.fromkeys(results))


def is_probable_page(url):
    extension = Path(urlsplit(url).path).suffix.lower()
    return extension not in NON_PAGE_EXTENSIONS


async def crawl_site(browser, site):
    print("\n" + "=" * 65)
    print("SITIO:", site["name"])
    print("INICIO:", site["start"])
    print("LÍMITE TOTAL:", site.get("max_total_mb", 2048), "MB")
    print("LÍMITE POR ARCHIVO:", format_mb(MAX_RESOURCE_SIZE), "MB")
    print("PÁGINAS SIMULTÁNEAS:", PAGE_WORKERS)
    print("INTENTOS MÁXIMOS POR PÁGINA:", MAX_PAGE_ATTEMPTS)
    print("=" * 65)

    tracker = create_site_tracker(site)
    site_storage[site["name"]] = tracker

    context = await browser.new_context(
        ignore_https_errors=True,
        viewport={"width": 1440, "height": 1000},
        locale="es-ES"
    )
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
    context.set_default_timeout(15000)

    starts = site.get("starts", [site["start"]])
    initial_urls = list(dict.fromkeys(
        clean_url(url) for url in starts if clean_url(url)
    ))

    queue = asyncio.Queue()
    for url in initial_urls:
        queue.put_nowait((url, 0, 1))

    # queued = descubiertas alguna vez; in_progress = activas; visited = guardadas.
    # Una URL solo pasa a visited después de guardar correctamente su HTML.
    queued = set(initial_urls)
    in_progress = set()
    visited = set()
    failed_pages = {}
    retry_count = 0
    pages_saved = 0
    pages_processed = 0
    mdn_translation_cache = {}
    mdn_translation_locks = {}
    mdn_english_skipped = 0
    mdn_english_saved = 0
    mdn_translation_unknown = 0
    mdn_aliases = []
    resource_tasks = set()

    worker_count = PAGE_WORKERS
    pages = [await context.new_page() for _ in range(worker_count)]

    def attach_resource_listener(page):
        def on_response(response):
            task = asyncio.create_task(save_response_resource(response, site, tracker))
            resource_tasks.add(task)
            task.add_done_callback(resource_tasks.discard)
        page.on("response", on_response)

    for page in pages:
        attach_resource_listener(page)

    async def worker(worker_id, page):
        nonlocal pages_saved, pages_processed, retry_count
        nonlocal mdn_english_skipped, mdn_english_saved, mdn_translation_unknown

        while True:
            item = await queue.get()
            current_url = ""
            depth = 0
            attempt = 1
            eligible_for_retry = False
            success = False
            claimed = False

            try:
                if item is None:
                    return

                current_url, depth, attempt = item
                current_url = clean_url(current_url)

                if not current_url or current_url in visited or current_url in in_progress:
                    continue
                if depth > MAX_DEPTH:
                    continue
                if pages_processed >= MAX_PAGES_PER_SITE or tracker["limite_alcanzado"]:
                    continue
                if not is_allowed_page(current_url, site) or not is_probable_page(current_url):
                    continue

                # No hay await entre la comprobación y la reserva: evita que dos
                # trabajadores procesen simultáneamente la misma URL.
                in_progress.add(current_url)
                claimed = True
                eligible_for_retry = True

                print(
                    f"[{site['name']}] [Trabajador {worker_id}] "
                    f"({pages_processed + 1}/{MAX_PAGES_PER_SITE}) "
                    f"Intento {attempt}/{MAX_PAGE_ATTEMPTS}: {current_url}"
                )

                navigation_timed_out = False
                response = None
                previous_page_url = clean_url(page.url)

                for nav_attempt in range(1, NAVIGATION_RETRIES + 1):
                    try:
                        response = await page.goto(
                            current_url,
                            wait_until="domcontentloaded",
                            timeout=NAVIGATION_TIMEOUT
                        )
                    except PlaywrightTimeoutError:
                        if nav_attempt < NAVIGATION_RETRIES:
                            await page.wait_for_timeout(RETRY_DELAY_MS)
                            continue
                        navigation_timed_out = True
                        errors.append(f"TIMEOUT NAVEGACIÓN | {current_url}")
                        response = None
                        break

                    if (
                        response is not None
                        and response.status in (429, 500, 502, 503, 504)
                        and nav_attempt < NAVIGATION_RETRIES
                    ):
                        await page.wait_for_timeout(RETRY_DELAY_MS * nav_attempt)
                        continue
                    break

                if response is not None and response.status >= 400:
                    errors.append(f"HTTP {response.status} | {current_url}")
                    continue

                if response is None:
                    current_browser_url = clean_url(page.url)
                    if (
                        not navigation_timed_out
                        or current_browser_url in ("", clean_url("about:blank"))
                        or current_browser_url == previous_page_url
                    ):
                        continue

                try:
                    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT)
                except PlaywrightTimeoutError:
                    pass

                # Activa imágenes diferidas y recorre la página para disparar lazy loading.
                try:
                    lazy_count = await page.locator(
                        'img[loading="lazy"], img[data-src], img[data-lazy-src]'
                    ).count()

                    if lazy_count:
                        await page.evaluate("""
                            () => {
                                document.querySelectorAll(
                                    'img[loading="lazy"], img[data-src], img[data-lazy-src]'
                                ).forEach(img => {
                                    img.loading = 'eager';
                                    if (!img.src && img.dataset.src) img.src = img.dataset.src;
                                    if (!img.src && img.dataset.lazySrc) img.src = img.dataset.lazySrc;
                                });
                            }
                        """)
                        await page.evaluate("""async (maxSteps, delay) => {
                            const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
                            const height = Math.max(document.body.scrollHeight,
                                document.documentElement.scrollHeight);
                            const step = Math.max(window.innerHeight, Math.ceil(height / maxSteps));
                            for (let y = 0, n = 0; y < height && n < maxSteps; y += step, n++) {
                                window.scrollTo(0, y);
                                await pause(delay);
                            }
                            window.scrollTo(0, 0);
                        }""", MAX_LAZY_SCROLL_STEPS, LAZY_SCROLL_STEP_MS)
                        await page.wait_for_timeout(300)
                    else:
                        await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
                except Exception:
                    await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

                try:
                    await page.locator("details").evaluate_all(
                        "elements => elements.forEach(el => el.open = true)"
                    )
                except Exception:
                    pass

                final_url = clean_url(page.url) or current_url
                if not is_allowed_page(final_url, site):
                    errors.append(f"REDIRECCIÓN FUERA DE RUTA | {current_url} -> {final_url}")
                    eligible_for_retry = False
                    continue

                html = await page.content()
                html_bytes = html.encode("utf-8", errors="replace")
                if not looks_like_html(html_bytes):
                    errors.append(f"NO HTML | {current_url}")
                    continue

                if pages_processed >= MAX_PAGES_PER_SITE or tracker["limite_alcanzado"]:
                    eligible_for_retry = False
                    continue

                # MDN: español tiene prioridad; solo se guarda inglés si no hay traducción.
                store_page = True
                spanish_counterpart = None
                if site.get("language_strategy") == "mdn_es_then_en":
                    final_parts = urlsplit(final_url)
                    if final_parts.path.startswith("/en-US/docs/") or final_parts.path == "/en-US/":
                        spanish_counterpart = mdn_spanish_counterpart(final_url, html)
                        if spanish_counterpart:
                            translation_exists = await mdn_translation_status(
                                context, spanish_counterpart,
                                mdn_translation_cache, mdn_translation_locks
                            )
                            if translation_exists is True:
                                store_page = False
                                mdn_english_skipped += 1
                                mdn_aliases.append((current_url, spanish_counterpart))
                                if spanish_counterpart not in queued and spanish_counterpart not in visited and spanish_counterpart not in in_progress:
                                    queued.add(spanish_counterpart)
                                    queue.put_nowait((spanish_counterpart, depth + 1, 1))
                                print(f"   Inglés omitido: existe versión española -> {spanish_counterpart}")
                            elif translation_exists is None:
                                mdn_translation_unknown += 1
                                errors.append(f"TRADUCCIÓN MDN NO VERIFICADA; se conserva inglés | {final_url} | {spanish_counterpart}")

                if store_page:
                    local_file = safe_local_path(
                        final_url, force_html=True, content_type="text/html"
                    )
                    if not save_limited_file(local_file, html_bytes, tracker, description=final_url):
                        eligible_for_retry = False
                        continue
                    register_url(current_url, local_file, is_page=True)
                    register_url(final_url, local_file, is_page=True)
                    pages_saved += 1
                    if urlsplit(final_url).path.startswith("/en-US/"):
                        mdn_english_saved += 1
                    print(
                        f"   Guardada: {pages_saved} | Almacenamiento: "
                        f"{format_mb(tracker['bytes_guardados'])} / {tracker['limite_mb']} MB"
                    )

                visited.add(current_url)
                visited.add(final_url)
                pages_processed += 1
                success = True

                # Una vez guardado el HTML, un fallo al extraer enlaces no debe
                # provocar que se descargue de nuevo la misma página.
                links = []
                for link_attempt in range(1, 3):
                    links = await extract_page_links(page, final_url, site)
                    if links:
                        break
                    if link_attempt == 1:
                        await asyncio.sleep(0.2)
                if not links:
                    errors.append(f"SIN ENLACES EXTRAÍDOS O PÁGINA SIN ENLACES | {final_url}")

                for link in links:
                    if link in queued or link in visited or link in in_progress:
                        continue
                    if is_probable_page(link):
                        queued.add(link)
                        queue.put_nowait((link, depth + 1, 1))

            except PlaywrightTimeoutError:
                errors.append(f"TIMEOUT | {current_url}")
            except Exception as exc:
                errors.append(f"PAGINA | {current_url} | {exc}")
            finally:
                if claimed:
                    in_progress.discard(current_url)

                if eligible_for_retry and not success and current_url:
                    if attempt < MAX_PAGE_ATTEMPTS and not tracker["limite_alcanzado"]:
                        retry_count += 1
                        delay = RETRY_DELAY_MS * attempt
                        print(
                            f"   Reintentando página ({attempt + 1}/{MAX_PAGE_ATTEMPTS}) "
                            f"en {delay} ms: {current_url}"
                        )
                        await asyncio.sleep(delay / 1000)
                        queue.put_nowait((current_url, depth, attempt + 1))
                    else:
                        failed_pages[current_url] = attempt
                        errors.append(
                            f"PÁGINA FALLIDA DEFINITIVA | intentos={attempt} | {current_url}"
                        )

                queue.task_done()

    workers = [
        asyncio.create_task(worker(index + 1, page))
        for index, page in enumerate(pages)
    ]

    await queue.join()
    for _ in workers:
        queue.put_nowait(None)
    await asyncio.gather(*workers, return_exceptions=True)

    # Las respuestas pueden seguir generando tareas de guardado después del crawl.
    while resource_tasks:
        pending = list(resource_tasks)
        await asyncio.gather(*pending, return_exceptions=True)

    for page in pages:
        try:
            await page.close()
        except Exception:
            pass

    # Los enlaces ingleses omitidos se redirigen al archivo local español equivalente.
    for english_url, spanish_url in mdn_aliases:
        spanish_file = url_to_file.get(clean_url(spanish_url))
        if spanish_file and Path(spanish_file).is_file():
            register_url(english_url, spanish_file, is_page=True)

    if site.get("language_strategy") == "mdn_es_then_en":
        mdn_language_stats[site["name"]] = {
            "paginas_inglesas_omitidas_por_traduccion_espanola": mdn_english_skipped,
            "paginas_inglesas_guardadas_por_no_tener_traduccion": mdn_english_saved,
            "traducciones_no_verificadas_se_conservo_ingles": mdn_translation_unknown,
            "equivalencias_detectadas": len(mdn_aliases),
            "rutas_excluidas": site.get("exclude_paths", [])
        }

    visited_by_site[site["name"]] = {
        "paginas_procesadas": pages_processed,
        "paginas_guardadas": pages_saved,
        "urls_descubiertas": len(queued),
        "paginas_completadas": len(visited),
        "paginas_fallidas_definitivas": len(failed_pages),
        "reintentos_de_pagina": retry_count,
        "detalle_fallidas": failed_pages,
        "almacenamiento_MB": format_mb(tracker["bytes_guardados"]),
        "limite_MB": tracker["limite_mb"],
        "limite_alcanzado": tracker["limite_alcanzado"],
        "trabajadores_simultaneos": worker_count
    }

    await context.close()

    print("\n" + "-" * 55)
    print("FINALIZADO:", site["name"])
    print("Páginas guardadas:", pages_saved)
    print("URLs descubiertas:", len(queued))
    print("Páginas fallidas definitivas:", len(failed_pages))
    print("Reintentos de página:", retry_count)
    print("Almacenamiento:", format_mb(tracker["bytes_guardados"]), "MB de", tracker["limite_mb"], "MB")
    print("Límite alcanzado:", "SÍ" if tracker["limite_alcanzado"] else "NO")
    print("-" * 55)


@lru_cache(maxsize=500000)
def find_local_target(url):
    url = clean_url(url)

    if not url:
        return None

    candidates = [url]
    parts = urlsplit(url)

    without_query = urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        "",
        ""
    ))

    if without_query not in candidates:
        candidates.append(without_query)

    path = parts.path or "/"

    if path.endswith("/"):
        candidates.append(
            urlunsplit((
                parts.scheme,
                parts.netloc,
                path + "index.html",
                "",
                ""
            ))
        )

    elif not Path(path).suffix:
        candidates.append(
            urlunsplit((
                parts.scheme,
                parts.netloc,
                path + ".html",
                "",
                ""
            ))
        )

    if not path.endswith("/") and not Path(path).suffix:
        candidates.append(
            urlunsplit((
                parts.scheme,
                parts.netloc,
                path.rstrip("/") + "/index.html",
                "",
                ""
            ))
        )

    for candidate in candidates:
        candidate = clean_url(candidate)
        target = url_to_file.get(candidate)

        if target and Path(target).exists():
            return Path(target)

    for candidate in candidates:
        candidate = clean_url(candidate)

        if not candidate:
            continue

        candidate_parts = urlsplit(candidate)
        host = candidate_parts.hostname or ""
        relative = unquote(candidate_parts.path or "/").lstrip("/")

        if not relative or relative.endswith("/"):
            relative += "index.html"

        local = ROOT / host / relative

        if local.exists() and local.is_file():
            return local

    return None


def relative_target(source_file, target_file):
    return os.path.relpath(
        Path(target_file),
        start=Path(source_file).parent
    ).replace(os.sep, "/")


def rewrite_html():
    for html_file in ROOT.rglob("*.html"):
        if "_metadata" in html_file.parts:
            continue

        source_url = file_to_url.get(str(html_file))

        if not source_url:
            continue

        try:
            soup = BeautifulSoup(
                html_file.read_text(encoding="utf-8", errors="replace"),
                "html.parser"
            )

            for tag in soup.find_all(True):
                for attr in (
                    "href", "src", "poster", "action",
                    "data-src", "data-lazy-src", "data-original",
                    "data-href", "data-url"
                ):
                    value = tag.get(attr)

                    if not isinstance(value, str) or not value.strip():
                        continue

                    value = value.strip()

                    if value.startswith(IGNORED_SCHEMES) or value.startswith("#"):
                        continue

                    absolute_with_fragment = urljoin(source_url, value)
                    parsed = urlsplit(absolute_with_fragment)
                    absolute = clean_url(absolute_with_fragment)

                    if not absolute:
                        continue

                    target = find_local_target(absolute)

                    if target:
                        local = relative_target(html_file, target)

                        if parsed.fragment:
                            local += "#" + parsed.fragment

                        tag[attr] = local

                if tag.has_attr("srcset"):
                    rewritten = []

                    for item in str(tag["srcset"]).split(","):
                        bits = item.strip().split()

                        if not bits:
                            continue

                        absolute = clean_url(urljoin(source_url, bits[0]))
                        target = find_local_target(absolute) if absolute else None

                        if target:
                            bits[0] = relative_target(html_file, target)

                        rewritten.append(" ".join(bits))

                    tag["srcset"] = ", ".join(rewritten)

            html_file.write_text(str(soup), encoding="utf-8")

        except Exception as exc:
            errors.append(f"REESCRITURA HTML | {html_file} | {exc}")


def rewrite_css():
    url_pattern = re.compile(
        r"url\(\s*([\"']?)(.*?)\1\s*\)",
        re.IGNORECASE
    )

    import_pattern = re.compile(
        r"@import\s+([\"'])(.*?)\1",
        re.IGNORECASE
    )

    for css_file in ROOT.rglob("*.css"):
        if "_metadata" in css_file.parts:
            continue

        source_url = file_to_url.get(str(css_file))

        if not source_url:
            continue

        try:
            content = css_file.read_text(
                encoding="utf-8",
                errors="replace"
            )

            def replace_url(match):
                raw = match.group(2).strip()

                if not raw or raw.startswith(("data:", "blob:", "#")):
                    return match.group(0)

                absolute = clean_url(urljoin(source_url, raw))
                target = find_local_target(absolute) if absolute else None

                if not target:
                    return match.group(0)

                quote_char = match.group(1)

                return (
                    "url("
                    + quote_char
                    + relative_target(css_file, target)
                    + quote_char
                    + ")"
                )

            content = url_pattern.sub(replace_url, content)

            def replace_import(match):
                raw = match.group(2).strip()
                absolute = clean_url(urljoin(source_url, raw))
                target = find_local_target(absolute) if absolute else None

                if not target:
                    return match.group(0)

                quote_char = match.group(1)

                return (
                    "@import "
                    + quote_char
                    + relative_target(css_file, target)
                    + quote_char
                )

            content = import_pattern.sub(replace_import, content)

            css_file.write_text(content, encoding="utf-8")

        except Exception as exc:
            errors.append(f"REESCRITURA CSS | {css_file} | {exc}")


def save_maps():
    url_map = {}

    for url, path in url_to_file.items():
        try:
            url_map[url] = str(
                Path(path).relative_to(ROOT)
            ).replace(os.sep, "/")

        except ValueError:
            continue

    save_json(URL_MAP_FILE, url_map)
    save_json(PAGE_MAP_FILE, sorted(page_urls))


def create_offline_index():
    links = []

    for site in SITES:
        target = find_local_target(site["start"])

        if target and target.exists():
            href = os.path.relpath(target, ROOT).replace(os.sep, "/")
            links.append(
                f'<li><a href="{href}">{site["name"]}</a></li>'
            )

        else:
            links.append(
                f'<li>{site["name"]} — página inicial no encontrada</li>'
            )

    html = '''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Documentación offline</title>
<style>
body{font-family:Arial,sans-serif;max-width:900px;margin:40px auto;padding:20px;line-height:1.6;background:#f3f5f7;color:#222}
main{background:white;padding:28px;border-radius:12px}
li{margin:14px 0}
a{color:#145db8;font-weight:bold}
</style>
</head>
<body>
<main>
<h1>Documentación de programación offline</h1>
<p>Selecciona el sitio que deseas consultar:</p>
<ul>
''' + "\n".join(links) + '''
</ul>
</main>
</body>
</html>'''

    (ROOT / "INICIO_OFFLINE.html").write_text(
        html,
        encoding="utf-8"
    )



def audit_artifact_paths():
    """Comprueba que ninguna ruta final viole las restricciones del artifact."""
    forbidden = set('"<>:|?*')
    invalid = []
    seen = {}
    collisions = []

    if not ROOT.exists():
        return {"rutas_invalidas": invalid, "colisiones_case_insensitive": collisions}

    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(ROOT)
        except ValueError:
            continue

        text_path = relative.as_posix()
        bad = any(ch in forbidden or ord(ch) < 32 for ch in text_path)
        bad = bad or any(part.endswith((" ", ".")) for part in relative.parts)
        reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
        bad = bad or any(part.split(".", 1)[0].upper() in reserved for part in relative.parts)
        if bad:
            invalid.append(text_path)

        key = text_path.casefold()
        previous = seen.get(key)
        if previous and previous != text_path:
            collisions.append([previous, text_path])
        else:
            seen[key] = text_path

    if invalid:
        errors.append("RUTAS NO COMPATIBLES CON ARTIFACT | " + " | ".join(invalid[:50]))
    if collisions:
        errors.append("COLISIONES CASE-INSENSITIVE | " + " | ".join(" <> ".join(x) for x in collisions[:50]))

    print("Auditoría de rutas:", len(invalid), "inválidas;", len(collisions), "colisiones por mayúsculas/minúsculas")
    return {"rutas_invalidas": invalid, "colisiones_case_insensitive": collisions}

def create_report():
    files = [
        p for p in ROOT.rglob("*")
        if p.is_file()
    ]

    total_bytes = sum(p.stat().st_size for p in files)
    storage_report = []

    for site in SITES:
        name = site["name"]
        tracker = site_storage.get(name, create_site_tracker(site))

        used = tracker["bytes_guardados"]
        limit = tracker["limite_bytes"]

        storage_report.append({
            "sitio": name,
            "limite_MB": tracker["limite_mb"],
            "descargado_MB": format_mb(used),
            "disponible_MB": format_mb(max(0, limit - used)),
            "porcentaje_usado": round(used / limit * 100, 2) if limit else 0,
            "archivos_guardados": len(tracker["archivos"]),
            "respuestas_api_guardadas": tracker.get("api_guardadas", 0),
            "respuestas_api_omitidas": tracker.get("api_omitidas", 0),
            "limite_alcanzado": tracker["limite_alcanzado"],
        })

    report = {
        "sitios": [
            {
                "name": s["name"],
                "start": s["start"],
                "hosts": sorted(s["hosts"]),
                "max_total_mb": s.get("max_total_mb", 2048)
            }
            for s in SITES
        ],
        "almacenamiento_por_sitio": storage_report,
        "paginas_por_sitio": visited_by_site,
        "seleccion_bilingue_mdn": mdn_language_stats,
        "paginas_en_mapa": len(page_urls),
        "recursos_capturados": len(asset_urls),
        "respuestas_fetch_xhr_detectadas": len(api_responses),
        "respuestas_fetch_xhr": api_responses,
        "archivos_generados": len(files),
        "tamano_total_bytes": total_bytes,
        "tamano_total_MB": round(total_bytes / 1024 / 1024, 2),
        "limite_por_recurso_MB": format_mb(MAX_RESOURCE_SIZE),
        "network_idle_timeout_ms": NETWORK_IDLE_TIMEOUT,
        "trabajadores_simultaneos": PAGE_WORKERS,
        "reintentos_navegacion": NAVIGATION_RETRIES,
        "espera_adicional_ms": WAIT_AFTER_LOAD_MS,
        "errores": len(errors),
        "rutas_sanitizadas": path_sanitizations,
        "colisiones_de_ruta_resueltas": path_collisions,
        "nota": (
            "Los límites por sitio contabilizan los archivos guardados "
            "durante esta ejecución. El tamaño total de la carpeta salida "
            "también puede incluir archivos de ejecuciones anteriores. "
            "MDN usa español como idioma prioritario y conserva en inglés "
            "las páginas /en-US/docs/ cuyo equivalente /es/docs/ no existe. "
            "Se excluye /en-US/play y se conserva /es/play. "
            "Las respuestas fetch/XHR textuales se guardan en carpetas _api "
            "con identidad por URL, método y hash del cuerpo POST. Esto no "
            "reconecta automáticamente la aplicación con esos archivos: "
            "las funciones que consultan APIs pueden seguir necesitando "
            "internet, autenticación o lógica adicional para uso offline."
        )
    }

    Path("informe-descarga.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    Path("errores-descarga.txt").write_text(
        "\n".join(errors),
        encoding="utf-8"
    )

    (METADATA / "api-responses.json").write_text(
        json.dumps(api_responses, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print("\n========== INFORME FINAL ==========")
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    METADATA.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("DESCARGADOR DE DOCUMENTACIÓN OFFLINE")
    print("SITIOS CONFIGURADOS:", len(SITES))
    print("MÁXIMO DE PÁGINAS POR SITIO:", MAX_PAGES_PER_SITE)
    print("PROFUNDIDAD MÁXIMA:", MAX_DEPTH)
    print("MÁXIMO POR ARCHIVO:", format_mb(MAX_RESOURCE_SIZE), "MB")
    print("TRABAJADORES SIMULTÁNEOS:", PAGE_WORKERS)
    print("=" * 65)

    load_previous_maps()
    infer_old_pages()
    normalize_existing_artifact_paths()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        try:
            for site in SITES:
                await crawl_site(browser, site)

        finally:
            await browser.close()

    print("\n===== GUARDANDO MAPAS =====")
    save_maps()

    print("\n===== REESCRIBIENDO HTML =====")
    rewrite_html()

    print("\n===== REESCRIBIENDO CSS =====")
    rewrite_css()

    print("\n===== CREANDO ÍNDICE OFFLINE =====")
    create_offline_index()

    save_maps()

    print("\n===== AUDITANDO RUTAS DEL ARTEFACTO =====")
    audit_artifact_paths()

    print("\n===== GENERANDO INFORME =====")
    create_report()

    print("\n===== PROCESO TERMINADO =====")


if __name__ == "__main__":
    asyncio.run(main())
