
import os
import re
import json
import time
import hashlib
from pathlib import Path
from collections import deque
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote, quote

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIGURACION
# ============================================================

SITES = [
    {
        "name": "WebCode",
        "start": "https://webcode.net.pe/web/index.html",
        "hosts": {"webcode.net.pe"},
    },
    {
        "name": "Cursos Pensa",
        "start": "https://cursospensa.ar/",
        "hosts": {"cursospensa.ar", "www.cursospensa.ar"},
    },
    {
        "name": "Javier Carrasco",
        "start": "https://documentation.javiercarrasco.es/",
        "hosts": {"documentation.javiercarrasco.es"},
    },
]

ROOT = Path("salida")
METADATA = ROOT / "_metadata"

URL_MAP_FILE = METADATA / "url-map.json"
PAGE_MAP_FILE = METADATA / "page-map.json"

MAX_PAGES_PER_SITE = 2500
MAX_DEPTH = 25

NAVIGATION_TIMEOUT = 45000
WAIT_AFTER_LOAD_MS = 1200
MAX_RESOURCE_SIZE = 100 * 1024 * 1024

IGNORED_SCHEMES = (
    "mailto:", "tel:", "javascript:", "data:",
    "blob:", "intent:", "about:"
)

NON_PAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".mjs", ".json", ".xml", ".txt",
    ".pdf", ".mp4", ".webm", ".mov", ".mp3", ".wav", ".ogg",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".sql", ".db", ".apk", ".exe", ".msi",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".bin", ".webmanifest",
}

HTML_MIME = {
    "text/html",
    "application/xhtml+xml",
}

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


# ============================================================
# ESTADO
# ============================================================

url_to_file = {}
file_to_url = {}
page_urls = set()
asset_urls = set()
errors = []
visited_by_site = {}


# ============================================================
# UTILIDADES GENERALES
# ============================================================

def clean_url(url):
    if not url:
        return ""

    try:
        parts = urlsplit(str(url).strip())

        if parts.scheme.lower() not in ("http", "https"):
            return ""

        return urlunsplit((
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path or "/",
            parts.query,
            ""
        ))
    except Exception:
        return ""


def is_internal(url, site):
    try:
        return urlsplit(url).hostname in site["hosts"]
    except Exception:
        return False


def query_suffix(query):
    if not query:
        return ""

    digest = hashlib.sha1(
        query.encode("utf-8")
    ).hexdigest()[:10]

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
        b"<?xml",
    )

    return any(sample.startswith(x) for x in signatures)


def looks_like_css(data):
    if not data:
        return False

    sample = data[:2048].lstrip().lower()

    return (
        b"{" in sample
        and (
            b"body" in sample
            or b"html" in sample
            or b"@" in sample
            or b":" in sample
        )
    )


def detect_extension(url, content_type, data):
    """
    Detecta el formato real del archivo.
    No depende únicamente de la extensión de la URL.
    """

    mime = (
        content_type or ""
    ).split(";")[0].strip().lower()

    path_ext = Path(
        urlsplit(url).path
    ).suffix.lower()

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


def safe_local_path(url, force_html=False, content_type=None):
    parts = urlsplit(url)

    host = parts.hostname or "desconocido"
    web_path = unquote(parts.path or "/")

    if web_path.endswith("/"):
        web_path += "index"

    relative = Path(host) / web_path.lstrip("/")

    mime = (
        content_type or ""
    ).split(";")[0].strip().lower()

    suffix = relative.suffix.lower()

    if force_html or mime in HTML_MIME:
        if suffix in (
            "", ".bin", ".php", ".asp",
            ".aspx", ".jsp"
        ):
            relative = relative.with_suffix(".html")

    elif not suffix:
        extension = MIME_EXTENSIONS.get(mime, ".bin")
        relative = relative.with_suffix(extension)

    if parts.query:
        relative = relative.with_name(
            relative.stem
            + query_suffix(parts.query)
            + relative.suffix
        )

    return ROOT / relative


def write_file(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def register_url(url, path, is_page=False):
    url = clean_url(url)

    if not url:
        return

    path = Path(path)

    url_to_file[url] = path
    file_to_url[str(path)] = url

    if is_page:
        page_urls.add(url)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# RECUPERAR MAPAS ANTERIORES
# ============================================================

def load_previous_maps():
    if URL_MAP_FILE.exists():
        try:
            data = json.loads(
                URL_MAP_FILE.read_text(encoding="utf-8")
            )

            for url, relative in data.items():
                path = ROOT / relative

                if path.exists():
                    register_url(url, path)

            print(
                "URLs recuperadas del mapa anterior:",
                len(url_to_file)
            )

        except Exception as exc:
            print("Error recuperando mapa anterior:", exc)

    if PAGE_MAP_FILE.exists():
        try:
            data = json.loads(
                PAGE_MAP_FILE.read_text(encoding="utf-8")
            )

            for url in data:
                page_urls.add(clean_url(url))

        except Exception as exc:
            print("Error recuperando mapa de páginas:", exc)


# ============================================================
# RECUPERAR HTML ANTIGUO GUARDADO COMO BIN
# ============================================================

def infer_old_pages():
    """
    Recupera archivos .bin cuyo contenido realmente es HTML.
    Después reconstruye las direcciones de páginas antiguas.
    """

    if not ROOT.exists():
        return

    known_hosts = set()

    for site in SITES:
        known_hosts.update(site["hosts"])

    # Convertir .bin que realmente contienen HTML.
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

            print(
                "HTML recuperado:",
                old_file,
                "->",
                new_file
            )

        except Exception as exc:
            errors.append(
                f"RECUPERAR BIN | {old_file} | {exc}"
            )

    # Reconstruir URL original a partir de la ubicación.
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


# ============================================================
# CAPTURA DE RECURSOS DEL NAVEGADOR
# ============================================================

def save_response_resource(response, site):
    try:
        resource_type = response.request.resource_type

        if resource_type in ("document", "websocket"):
            return

        if response.status not in (200, 206):
            return

        url = clean_url(response.url)

        if not url or not is_internal(url, site):
            return

        allowed = {
            "stylesheet", "script", "image",
            "font", "manifest", "media"
        }

        if resource_type not in allowed:
            return

        headers = response.headers
        content_type = headers.get("content-type", "")

        length = headers.get("content-length")

        if length:
            try:
                if int(length) > MAX_RESOURCE_SIZE:
                    print("Recurso demasiado grande:", url)
                    return
            except ValueError:
                pass

        data = response.body()

        if not data or len(data) > MAX_RESOURCE_SIZE:
            return

        # Evitar guardar una página HTML como imagen, fuente o binario.
        if looks_like_html(data):
            return

        extension = detect_extension(
            url,
            content_type,
            data
        )

        path = safe_local_path(
            url,
            content_type=content_type
        )

        if path.suffix.lower() != extension:
            path = path.with_suffix(extension)

        write_file(path, data)
        register_url(url, path)

        asset_urls.add(url)

    except Exception as exc:
        errors.append(
            f"RECURSO | {response.url} | {exc}"
        )


# ============================================================
# EXTRACCIÓN DE ENLACES
# ============================================================

def extract_page_links(page, current_url, site):
    results = []

    try:
        elements = page.locator(
            "a[href], area[href], iframe[src], "
            "frame[src], source[src], video[src], "
            "audio[src], link[href]"
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

            absolute = clean_url(
                urljoin(current_url, value)
            )

            if not absolute:
                continue

            if not is_internal(absolute, site):
                continue

            results.append(absolute)

    except Exception as exc:
        errors.append(
            f"EXTRACCION ENLACES | {current_url} | {exc}"
        )

    return list(dict.fromkeys(results))


def is_probable_page(url):
    extension = Path(
        urlsplit(url).path
    ).suffix.lower()

    return extension not in NON_PAGE_EXTENSIONS


# ============================================================
# DESCARGA DE PAGINAS
# ============================================================

def crawl_site(browser, site):
    print("\n" + "=" * 65)
    print("SITIO:", site["name"])
    print("INICIO:", site["start"])
    print("=" * 65)

    context = browser.new_context(
        ignore_https_errors=True,
        viewport={
            "width": 1440,
            "height": 1000
        },
        locale="es-ES",
    )

    context.set_default_navigation_timeout(
        NAVIGATION_TIMEOUT
    )

    context.set_default_timeout(15000)

    context.on(
        "response",
        lambda response: save_response_resource(
            response,
            site
        )
    )

    start = clean_url(site["start"])

    queue = deque([(start, 0)])
    queued = {start}
    visited = set()

    pages_saved = 0

    while queue and pages_saved < MAX_PAGES_PER_SITE:
        current_url, depth = queue.popleft()

        current_url = clean_url(current_url)

        if not current_url or current_url in visited:
            continue

        if depth > MAX_DEPTH:
            continue

        if not is_internal(current_url, site):
            continue

        if not is_probable_page(current_url):
            continue

        visited.add(current_url)

        page = context.new_page()

        try:
            print(
                f"[{site['name']}] "
                f"({pages_saved + 1}/{MAX_PAGES_PER_SITE}) "
                f"{current_url}"
            )

            response = page.goto(
                current_url,
                wait_until="domcontentloaded",
                timeout=NAVIGATION_TIMEOUT
            )

            if response is None:
                continue

            if response.status >= 400:
                errors.append(
                    f"HTTP {response.status} | {current_url}"
                )
                continue

            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=10000
                )
            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(
                WAIT_AFTER_LOAD_MS
            )

            # Activar imágenes diferidas.
            try:
                page.evaluate("""
                    () => {
                        document.querySelectorAll(
                            'img[loading="lazy"]'
                        ).forEach(img => {
                            img.loading = 'eager';
                        });
                    }
                """)
            except Exception:
                pass

            # Expandir elementos details.
            try:
                page.locator("details").evaluate_all("""
                    elements => elements.forEach(
                        el => el.open = true
                    )
                """)
            except Exception:
                pass

            final_url = clean_url(page.url) or current_url

            html = page.content()
            html_bytes = html.encode(
                "utf-8",
                errors="replace"
            )

            if not looks_like_html(html_bytes):
                errors.append(
                    f"NO HTML | {current_url}"
                )
                continue

            local_file = safe_local_path(
                final_url,
                force_html=True,
                content_type="text/html"
            )

            write_file(local_file, html_bytes)

            register_url(
                current_url,
                local_file,
                is_page=True
            )

            register_url(
                final_url,
                local_file,
                is_page=True
            )

            pages_saved += 1

            links = extract_page_links(
                page,
                final_url,
                site
            )

            for link in links:
                if link in queued or link in visited:
                    continue

                if is_probable_page(link):
                    queue.append(
                        (link, depth + 1)
                    )
                    queued.add(link)

        except PlaywrightTimeoutError:
            errors.append(
                f"TIMEOUT | {current_url}"
            )

        except Exception as exc:
            errors.append(
                f"PAGINA | {current_url} | {exc}"
            )

        finally:
            try:
                page.close()
            except Exception:
                pass

    visited_by_site[site["name"]] = {
        "paginas_guardadas": pages_saved,
        "urls_visitadas": len(visited),
        "urls_pendientes": len(queue),
    }

    context.close()

    print(
        f"Finalizado {site['name']}: "
        f"{pages_saved} páginas guardadas."
    )


# ============================================================
# BUSQUEDA DE DESTINOS LOCALES
# ============================================================

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
            
