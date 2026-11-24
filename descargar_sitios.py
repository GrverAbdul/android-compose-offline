# -*- coding: utf-8 -*-
"""
DESCARGADOR OFFLINE — BIBLIOTECA BILINGÜE (INGLÉS + ESPAÑOL)

Sitios:
  1. Minimal Books
  2. PONN.IO (catálogo de libros en inglés)
  3. Babelotheca (libros bilingües)

Basado en el enfoque del descargador especializado de Storyteller:
- Playwright/Chromium para capturar páginas ya renderizadas.
- Expansión de botones "Load more / Cargar más".
- Scroll progresivo para activar lazy loading.
- Descubrimiento recursivo de páginas internas.
- Captura de CSS, JavaScript, imágenes, fuentes, audio y vídeo solicitados.
- Reescritura de enlaces HTML/CSS para abrir la copia local.
- Una carpeta y mapas independientes por sitio.
- Sin inicio de sesión ni automatización de cuentas.

IMPORTANTE:
Una copia estática no puede reproducir servicios que dependan de servidores,
API privadas, autenticación, streaming externo o funciones que necesiten conexión.
El script no intenta eludir restricciones ni descargar contenido protegido.
"""

import asyncio
import hashlib
import json
import os
import re
import mimetypes
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

OUTPUT_ROOT = Path("bibliotecas_offline")

# Los tres sitios se descargan por separado.
# allowed_prefixes limita el rastreo cuando conviene.
SITES = [
    {
        "name": "Minimal Books",
        "start_urls": [
            "https://minimalbooks.com/",
            "https://minimalbooks.com/es/",
        ],
        "allowed_prefixes": None,
        "excluded_prefixes": [
            "/wp-admin/", "/wp-login.php", "/cart/", "/checkout/",
        ],
        "max_pages": 50000,
        "max_total_mb": 5000,
    },
    {
        "name": "PONN",
        "start_urls": [
            # /en/ y /es/ son el idioma de la interfaz/traducción;
            # /english/ mantiene el catálogo de libros cuyo idioma de lectura es inglés.
            "https://ponn.io/en/english/books/",
            "https://ponn.io/es/english/books/",
        ],
        # Conserva los libros en inglés y captura la interfaz en ambos idiomas,
        # para que la traducción de apoyo al español quede incluida cuando el
        # propio sitio la entrega en esa variante.
        "allowed_prefixes": [
            "/en/english/", "/es/english/",
        ],
        "excluded_prefixes": [
            "/en/english/games/", "/en/english/videos/",
            "/en/english/words/", "/en/english/grammar/",
            "/login", "/signup", "/register", "/checkout",
        ],
        "max_pages": 50000,
        "max_total_mb": 8000,
    },
    {
        "name": "Babelotheca",
        "start_urls": [
            "https://babelotheca.com/bilingual-books",
        ],
        # Se deja abierto el dominio porque las rutas de lectura de los libros
        # pueden no compartir el mismo prefijo que el catálogo.
        "allowed_prefixes": None,
        "excluded_prefixes": [
            "/login", "/sign-in", "/signup", "/register",
            "/pricing", "/checkout", "/translate",
            "/account", "/settings", "/api/",
        ],
        "max_pages": 50000,
        "max_total_mb": 5000,
    },
]

# La interfaz de PONN puede usar el idioma del navegador para elegir la
# lengua de apoyo/traducción. Se usa español como preferencia del navegador.
BROWSER_LOCALE = "es-ES"

WORKERS = 3
MAX_DEPTH = 45
NAVIGATION_TIMEOUT = 60000
NETWORK_IDLE_TIMEOUT = 3500
WAIT_AFTER_LOAD_MS = 900

# Expansión de listados y contenido dinámico.
MAX_LOAD_MORE_CLICKS = 160
LOAD_MORE_WAIT_MS = 1100
MAX_SCROLL_STEPS = 35
SCROLL_PAUSE_MS = 220
MAX_PAGE_ATTEMPTS = 3

# Límites para evitar que un archivo enorme o un bucle agoten el disco.
MAX_RESOURCE_SIZE = 700 * 1024 * 1024
DEFAULT_MAX_TOTAL_MB = 5000

IGNORED_SCHEMES = (
    "mailto:", "tel:", "javascript:", "data:", "blob:",
    "intent:", "about:", "sms:"
)

NON_PAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
    ".css", ".js", ".mjs", ".json", ".xml", ".txt", ".pdf",
    ".mp4", ".webm", ".mov", ".mp3", ".wav", ".ogg", ".m4a",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".sql", ".db",
    ".apk", ".exe", ".msi", ".woff", ".woff2", ".ttf", ".otf",
    ".eot", ".bin", ".webmanifest", ".epub", ".mobi", ".azw3"
}

LANGUAGE_CODES = set("""
af ar az be bg bn bs ca cs cy da de el eo et eu fa fi fil fr ga gl
gu he hi hr hu hy id is ja jv ka kk km kn ko lt lv mk ml mn mr
ms my ne nl no pa pl pt pt-br pt-pt ro ru sk sl sq sr sv sw ta te
th tr uk ur uz vi zh zh-cn zh-tw
""".split())

LOAD_MORE_RE = re.compile(
    r"(load\s*more|show\s*more|more\s*(stories|books|chapters|items|results)|"
    r"view\s*more|see\s*all|"
    r"cargar\s*m[aá]s|ver\s*m[aá]s|mostrar\s*m[aá]s|"
    r"m[aá]s\s*(historias|libros|cap[ií]tulos|resultados)|"
    r"next\s*(page|chapter)|siguiente)",
    re.I
)

RESOURCE_TYPES = {
    "stylesheet", "script", "image", "font", "media",
    "manifest", "xhr", "fetch", "other"
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
    "image/avif": ".avif",
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
    "audio/mp4": ".m4a",
}


# ============================================================
# UTILIDADES DE URL Y RUTAS
# ============================================================

def clean_url(url):
    """Normaliza URL sin eliminar parámetros funcionales."""
    if not url:
        return ""
    try:
        p = urlsplit(str(url).strip())
        scheme = p.scheme.lower()
        if scheme not in ("http", "https") or not p.hostname:
            return ""

        host = p.hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

        port = p.port
        if port and not (
            (scheme == "http" and port == 80)
            or (scheme == "https" and port == 443)
        ):
            host += f":{port}"

        userinfo = ""
        if "@" in p.netloc:
            userinfo = p.netloc.rsplit("@", 1)[0] + "@"

        def normalize_escape(match):
            value = int(match.group(1), 16)
            char = chr(value)
            if char.isalnum() or char in "-._~":
                return char
            return "%" + match.group(1).upper()

        path = re.sub(r"%([0-9a-fA-F]{2})", normalize_escape, p.path or "/")
        query = re.sub(
            r"%([0-9a-fA-F]{2})",
            lambda m: "%" + m.group(1).upper(),
            p.query
        )
        return urlunsplit((scheme, userinfo + host, path, query, ""))
    except Exception:
        return ""


def safe_component(value, max_length=150):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))
    value = value.rstrip(" .") or "_"
    stem = value.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{i}" for i in range(1, 10))
    reserved.update(f"LPT{i}" for i in range(1, 10))
    if stem in reserved:
        value = "_" + value
    if len(value) > max_length:
        suffix = Path(value).suffix
        digest = hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:12]
        keep = max(1, max_length - len(suffix) - len(digest) - 2)
        value = value[:keep] + "__" + digest + suffix
    return value


def query_suffix(query):
    if not query:
        return ""
    return "__q_" + hashlib.sha1(query.encode("utf-8")).hexdigest()[:12]


def looks_like_html(data):
    if not data:
        return False
    sample = data[:4096].lstrip().lower()
    return sample.startswith((
        b"<!doctype html", b"<html", b"<head", b"<body",
        b"<title", b"<meta"
    ))


def detect_extension(url, content_type, data):
    mime = (content_type or "").split(";", 1)[0].strip().lower()
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
    if data.startswith(b"wOFF"):
        return ".woff"
    if data.startswith(b"wOF2"):
        return ".woff2"
    if data.startswith(b"\x00\x01\x00\x00"):
        return ".ttf"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4"
    if mime in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime]
    if path_ext:
        return path_ext
    guessed = mimetypes.guess_extension(mime) if mime else None
    return guessed or ".bin"


def is_language_path_allowed(path):
    """Rechaza rutas cuyo primer segmento sea un idioma distinto de en/es."""
    first = (path or "/").strip("/").split("/", 1)[0].lower()
    if first in LANGUAGE_CODES and first not in {"en", "es"}:
        return False
    return True


# ============================================================
# ESTADO INDEPENDIENTE DE CADA SITIO
# ============================================================

class Site:
    def __init__(self, config):
        self.name = config["name"]
        self.starts = [clean_url(x) for x in config["start_urls"]]
        self.hosts = set()
        for url in self.starts:
            host = urlsplit(url).hostname
            if host:
                self.hosts.add(host)
                self.hosts.add(host[4:] if host.startswith("www.") else "www." + host)

        self.allowed_prefixes = config.get("allowed_prefixes")
        self.excluded_prefixes = config.get("excluded_prefixes", [])
        self.max_pages = int(config.get("max_pages", 50000))
        self.limit_mb = int(config.get("max_total_mb", DEFAULT_MAX_TOTAL_MB))
        self.limit_bytes = self.limit_mb * 1024 * 1024

        self.root = OUTPUT_ROOT / safe_component(self.name)
        self.meta = self.root / "_metadata"

        self.url_to_file = {}
        self.file_to_url = {}
        self.path_owners = {}
        self.pages = set()
        self.resources = set()
        self.errors = []
        self.bytes_saved = 0
        self.files_saved = 0
        self.resource_locks = {}
        self.resource_tasks = set()

    def allowed_page(self, url):
        url = clean_url(url)
        if not url:
            return False

        p = urlsplit(url)
        if p.hostname not in self.hosts:
            return False
        if Path(p.path).suffix.lower() in NON_PAGE_EXTENSIONS:
            return False
        if not is_language_path_allowed(p.path):
            return False

        if self.allowed_prefixes:
            if not any(
                p.path == prefix.rstrip("/")
                or p.path.startswith(prefix.rstrip("/") + "/")
                or prefix == "/"
                for prefix in self.allowed_prefixes
            ):
                return False

        for prefix in self.excluded_prefixes:
            prefix = prefix.rstrip("/") or "/"
            if p.path == prefix or p.path.startswith(prefix + "/"):
                return False

        return True

    def allowed_resource(self, url):
        """Se aceptan recursos externos si fueron solicitados por una página."""
        return bool(clean_url(url))

    def local_path(self, url, force_html=False, content_type=""):
        url = clean_url(url)
        existing = self.url_to_file.get(url)
        if existing:
            return Path(existing)

        p = urlsplit(url)
        host = safe_component(p.hostname or "unknown")
        web_path = unquote(p.path or "/")
        if web_path.endswith("/"):
            web_path += "index"

        parts = [host]
        parts.extend(safe_component(x) for x in web_path.strip("/").split("/") if x)
        path = self.root.joinpath(*parts)

        mime = (content_type or "").split(";", 1)[0].lower()
        if force_html or mime in ("text/html", "application/xhtml+xml"):
            if path.suffix.lower() not in (".html", ".htm"):
                path = path.with_suffix(".html")
        elif not path.suffix:
            path = path.with_suffix(MIME_EXTENSIONS.get(mime, ".bin"))

        if p.query:
            path = path.with_name(path.stem + query_suffix(p.query) + path.suffix)

        # Evita colisiones entre URL distintas con el mismo nombre.
        key = str(path).replace("\\", "/").casefold()
        owner = self.path_owners.get(key)
        if (owner and owner != url) or (path.exists() and owner != url):
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
            path = path.with_name(path.stem + "__" + digest + path.suffix)
            key = str(path).replace("\\", "/").casefold()

        self.path_owners[key] = url
        return path

    def register(self, url, path, is_page=False):
        url = clean_url(url)
        if not url:
            return
        path = Path(path)
        self.url_to_file[url] = path
        self.file_to_url.setdefault(str(path), url)
        self.path_owners[str(path).replace("\\", "/").casefold()] = url
        if is_page:
            self.pages.add(url)

    def save(self, path, data):
        path = Path(path)
        old_size = path.stat().st_size if path.exists() else 0
        difference = len(data) - old_size
        if difference > 0 and self.bytes_saved + difference > self.limit_bytes:
            print(f"      [LÍMITE {self.name}] Omitido: {path}")
            return False

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.bytes_saved += difference
        if old_size == 0:
            self.files_saved += 1
        return True

    def find_local(self, url):
        url = clean_url(url)
        if not url:
            return None

        p = urlsplit(url)
        candidates = [url]
        no_query = urlunsplit((p.scheme, p.netloc, p.path, "", ""))
        if no_query not in candidates:
            candidates.append(no_query)

        path = p.path or "/"
        if path.endswith("/"):
            candidates.append(urlunsplit((p.scheme, p.netloc, path + "index.html", "", "")))
        elif not Path(path).suffix:
            candidates.append(urlunsplit((p.scheme, p.netloc, path + ".html", "", "")))
            candidates.append(urlunsplit((p.scheme, p.netloc, path.rstrip("/") + "/index.html", "", "")))

        for candidate in candidates:
            target = self.url_to_file.get(clean_url(candidate))
            if target and Path(target).is_file():
                return Path(target)

        for candidate in candidates:
            cp = urlsplit(candidate)
            local = self.root / safe_component(cp.hostname or "unknown")
            local = local.joinpath(*[
                safe_component(x) for x in unquote(cp.path or "/").strip("/").split("/") if x
            ])
            if cp.path.endswith("/") or not local.suffix:
                local = local / "index.html" if local.is_dir() else local.with_suffix(".html")
            if local.is_file():
                return local
        return None


# ============================================================
# EXPANSIÓN DE CONTENIDO DINÁMICO
# ============================================================

async def scroll_to_load_lazy(page):
    """Activa imágenes y contenido que aparece al desplazarse."""
    try:
        await page.evaluate(
            """async ({steps, pause}) => {
                document.querySelectorAll('img[loading="lazy"]').forEach(img => {
                    img.loading = 'eager';
                });
                document.querySelectorAll(
                    'img[data-src], img[data-lazy-src], source[data-src]'
                ).forEach(el => {
                    const candidate = el.dataset.src || el.dataset.lazySrc;
                    if (candidate && (!el.getAttribute('src') ||
                        el.getAttribute('src').startsWith('data:'))) {
                        el.setAttribute('src', candidate);
                    }
                });
                const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
                let previousHeight = 0;
                let stable = 0;
                for (let i = 0; i < steps; i++) {
                    window.scrollTo(0, document.documentElement.scrollHeight);
                    await wait(pause);
                    const h = document.documentElement.scrollHeight;
                    if (h === previousHeight) stable++;
                    else stable = 0;
                    previousHeight = h;
                    if (stable >= 4) break;
                }
                window.scrollTo(0, 0);
                await wait(250);
            }""",
            {"steps": MAX_SCROLL_STEPS, "pause": SCROLL_PAUSE_MS}
        )
    except Exception:
        pass


async def expand_more(page):
    """Pulsa botones de carga progresiva sin seguir enlaces normales."""
    clicks = 0
    stable_rounds = 0

    while clicks < MAX_LOAD_MORE_CLICKS:
        before = await page.evaluate(
            """() => ({
                links: document.querySelectorAll('a[href]').length,
                text: (document.body?.innerText || '').length,
                height: document.documentElement?.scrollHeight || 0,
                cards: document.querySelectorAll(
                    'article,[class*="book"],[class*="story"],[class*="card"],[data-book]'
                ).length
            })"""
        )

        result = await page.evaluate(
            """(source) => {
                const re = new RegExp(source, 'i');
                const selectors = [
                    'button', '[role="button"]',
                    'input[type="button"]', 'input[type="submit"]'
                ].join(',');
                for (const el of document.querySelectorAll(selectors)) {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    const visible = rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                    const disabled = el.disabled ||
                        el.getAttribute('aria-disabled') === 'true';
                    const label = [
                        el.innerText, el.textContent, el.getAttribute('aria-label'),
                        el.getAttribute('title'), el.value
                    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();

                    if (visible && !disabled && re.test(label)) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return {clicked: true, label: label.slice(0, 120)};
                    }
                }
                return {clicked: false};
            }""",
            LOAD_MORE_RE.pattern
        )

        if not result.get("clicked"):
            break

        clicks += 1
        print(f"      [CARGA PROGRESIVA] {clicks}: {result.get('label', '')}")

        try:
            await page.wait_for_load_state("networkidle", timeout=4500)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(LOAD_MORE_WAIT_MS)
        await scroll_to_load_lazy(page)

        after = await page.evaluate(
            """() => ({
                links: document.querySelectorAll('a[href]').length,
                text: (document.body?.innerText || '').length,
                height: document.documentElement?.scrollHeight || 0,
                cards: document.querySelectorAll(
                    'article,[class*="book"],[class*="story"],[class*="card"],[data-book]'
                ).length
            })"""
        )

        grew = any(after[key] > before[key] for key in before)
        stable_rounds = 0 if grew else stable_rounds + 1
        if stable_rounds >= 2:
            break

    if clicks:
        print(f"      [CARGA PROGRESIVA] Total de clics: {clicks}")
    return clicks


async def open_details(page):
    try:
        await page.locator("details").evaluate_all(
            "els => els.forEach(el => el.open = true)"
        )
    except Exception:
        pass


# ============================================================
# CAPTURA DE RECURSOS
# ============================================================

async def save_resource(site, response):
    """Guarda los recursos que el navegador realmente solicita."""
    try:
        if response.status not in (200, 206):
            return

        request = response.request
        resource_type = request.resource_type
        if resource_type in ("document", "websocket", "eventsource"):
            return
        if resource_type not in RESOURCE_TYPES:
            return

        url = clean_url(response.url)
        if not url or not site.allowed_resource(url):
            return

        existing = site.url_to_file.get(url)
        if existing and Path(existing).is_file():
            return

        # Un lock por URL evita escrituras duplicadas cuando varios workers
        # reciben el mismo recurso.
        lock = site.resource_locks.setdefault(url, asyncio.Lock())
        async with lock:
            existing = site.url_to_file.get(url)
            if existing and Path(existing).is_file():
                return

            headers = response.headers
            content_type = headers.get("content-type", "")
            length = headers.get("content-length")
            if length:
                try:
                    if int(length) > MAX_RESOURCE_SIZE:
                        return
                except (ValueError, TypeError):
                    pass

            data = await response.body()
            if not data or len(data) > MAX_RESOURCE_SIZE or looks_like_html(data):
                return

            extension = detect_extension(url, content_type, data)
            path = site.local_path(url, content_type=content_type)
            if path.suffix.lower() != extension:
                path = path.with_suffix(extension)

            if site.save(path, data):
                site.register(url, path)
                site.resources.add(url)

    except Exception as exc:
        site.errors.append(f"RECURSO | {getattr(response, 'url', '?')} | {exc}")


# ============================================================
# DESCUBRIMIENTO DE ENLACES
# ============================================================

async def extract_page_links(page, base_url, site):
    selectors = (
        "a[href], area[href], iframe[src], frame[src], "
        "source[src], video[src], audio[src], link[href]"
    )
    try:
        values = await page.locator(selectors).evaluate_all(
            """els => els.map(el =>
                el.href || el.src || el.getAttribute('href') ||
                el.getAttribute('src') || ''
            )"""
        )
    except Exception as exc:
        site.errors.append(f"EXTRAER ENLACES | {base_url} | {exc}")
        return []

    found = []
    for value in values:
        value = (value or "").strip()
        if not value or value.lower().startswith(IGNORED_SCHEMES):
            continue
        absolute = clean_url(urljoin(base_url, value))
        if absolute and site.allowed_page(absolute):
            found.append(absolute)
    return list(dict.fromkeys(found))


# ============================================================
# RASTREO DE UN SITIO
# ============================================================

# ============================================================
# REESCRITURA PARA NAVEGACIÓN LOCAL
# ============================================================

def relative_path(source, target):
    return os.path.relpath(target, Path(source).parent).replace(os.sep, "/")


def rewrite_html(site):
    for html_file in site.root.rglob("*.html"):
        if "_metadata" in html_file.parts:
            continue

        source_url = site.file_to_url.get(str(html_file))
        if not source_url:
            continue

        try:
            soup = BeautifulSoup(
                html_file.read_text(encoding="utf-8", errors="replace"),
                "html.parser"
            )

            for tag in soup.find_all(True):
                for attr in (
                    "href", "src", "poster", "data-src", "data-lazy-src",
                    "data-original", "data-href", "data-url", "xlink:href"
                ):
                    value = tag.get(attr)
                    if not isinstance(value, str) or not value.strip():
                        continue

                    value = value.strip()
                    if value.startswith(IGNORED_SCHEMES) or value.startswith("#"):
                        continue

                    full = urljoin(source_url, value)
                    parsed = urlsplit(full)
                    absolute = clean_url(full)
                    target = site.find_local(absolute) if absolute else None

                    if target:
                        local = relative_path(html_file, target)
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
                        target = site.find_local(absolute) if absolute else None
                        if target:
                            bits[0] = relative_path(html_file, target)
                        rewritten.append(" ".join(bits))
                    tag["srcset"] = ", ".join(rewritten)

                # Recursos declarados dentro de style="...".
                if tag.has_attr("style"):
                    style = str(tag["style"])
                    style_pattern = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)

                    def replace_style(match):
                        raw = match.group(2).strip()
                        absolute = clean_url(urljoin(source_url, raw))
                        target = site.find_local(absolute) if absolute else None
                        if target:
                            return "url('" + relative_path(html_file, target) + "')"
                        return match.group(0)

                    tag["style"] = style_pattern.sub(replace_style, style)

            html_file.write_text(str(soup), encoding="utf-8")

        except Exception as exc:
            site.errors.append(f"REESCRITURA HTML | {html_file} | {exc}")


def rewrite_css(site):
    pattern = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)

    for css_file in site.root.rglob("*.css"):
        source_url = site.file_to_url.get(str(css_file))
        if not source_url:
            continue

        try:
            css = css_file.read_text(encoding="utf-8", errors="replace")

            def replace_url(match):
                raw = match.group(2).strip()
                if not raw or raw.startswith(("data:", "blob:", "#")):
                    return match.group(0)

                absolute = clean_url(urljoin(source_url, raw))
                target = site.find_local(absolute) if absolute else None
                if target:
                    return "url('" + relative_path(css_file, target) + "')"
                return match.group(0)

            css_file.write_text(pattern.sub(replace_url, css), encoding="utf-8")

        except Exception as exc:
            site.errors.append(f"REESCRITURA CSS | {css_file} | {exc}")


# ============================================================
# ÍNDICE, INFORME Y EJECUCIÓN
# ============================================================

def create_index(site, stats):
    links = []
    for url in site.starts:
        target = site.find_local(url)
        if target:
            href = relative_path(site.root / "INICIO_OFFLINE.html", target)
            links.append(f'<li><a href="{href}">{url}</a></li>')
        else:
            links.append(f"<li>No descargada: {url}</li>")

    safe_title = site.name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title} — offline</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:980px;margin:40px auto;padding:20px;
background:#f2f4f7;color:#222;line-height:1.65}}
main{{background:#fff;padding:28px;border-radius:12px}}
a{{overflow-wrap:anywhere}}li{{margin:14px 0}}
small{{color:#555}}
</style>
</head>
<body><main>
<h1>{safe_title} — biblioteca offline</h1>
<p>Abre el catálogo guardado desde el enlace inicial:</p>
<ul>{''.join(links)}</ul>
<hr>
<p>Páginas guardadas: {stats['paginas_procesadas']}<br>
URLs descubiertas: {stats['urls_descubiertas']}<br>
Recursos guardados: {len(site.resources)}<br>
Archivos: {site.files_saved}<br>
Espacio ocupado durante esta ejecución: {site.bytes_saved / 1048576:.2f} MB</p>
<small>Las funciones que necesiten servidores, API, cuentas, audio/vídeo en streaming
o servicios externos pueden no funcionar sin conexión.</small>
</main></body></html>"""

    (site.root / "INICIO_OFFLINE.html").write_text(html, encoding="utf-8")


def write_report(site, stats):
    report = {
        "sitio": site.name,
        "urls_iniciales": site.starts,
        "idioma_navegador": BROWSER_LOCALE,
        "idiomas_objetivo": ["en", "es"],
        "estadisticas": stats,
        "paginas_guardadas": len(site.pages),
        "recursos_guardados": len(site.resources),
        "archivos_guardados": site.files_saved,
        "almacenamiento_mb": round(site.bytes_saved / 1048576, 2),
        "limite_mb": site.limit_mb,
        "errores": site.errors,
        "nota": (
            "La copia contiene las páginas y recursos públicos capturados. "
            "Las funciones dependientes de servidor/API/streaming pueden requerir Internet."
        ),
    }

    site.meta.mkdir(parents=True, exist_ok=True)
    (site.meta / "informe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    (site.meta / "errores.txt").write_text(
        "\n".join(site.errors),
        encoding="utf-8"
    )

    print("\n========== INFORME:", site.name, "==========")
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def main():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("DESCARGADOR OFFLINE — MINIMAL BOOKS + PONN + BABELOTHECA")
    print("Idioma preferido del navegador:", BROWSER_LOCALE)
    print("Trabajadores por sitio:", WORKERS)
    print("Carga progresiva máxima por página:", MAX_LOAD_MORE_CLICKS)
    print("Scroll lazy-load máximo:", MAX_SCROLL_STEPS)
    print("=" * 72)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        try:
            for config in SITES:
                site = Site(config)
                site.root.mkdir(parents=True, exist_ok=True)
                site.meta.mkdir(parents=True, exist_ok=True)

                print("\n" + "=" * 72)
                print("SITIO:", site.name)
                print("INICIOS:", site.starts)
                print("DOMINIOS:", sorted(site.hosts))
                print("LÍMITE:", site.limit_mb, "MB")
                print("=" * 72)

                # La rutina usa el mismo navegador Playwright, pero crea su
                # propio contexto y mapas para no mezclar recursos entre sitios.
                stats = await crawl_site_with_browser(browser, site)

                print("\n===== REESCRIBIENDO HTML =====")
                rewrite_html(site)

                print("===== REESCRIBIENDO CSS =====")
                rewrite_css(site)

                print("===== CREANDO ÍNDICE =====")
                create_index(site, stats)

                write_report(site, stats)

                print("ÍNDICE OFFLINE:", (site.root / "INICIO_OFFLINE.html").resolve())

        finally:
            await browser.close()

    print("\n" + "=" * 72)
    print("PROCESO TERMINADO.")
    print("Carpeta principal:", OUTPUT_ROOT.resolve())
    print("Abre el INICIO_OFFLINE.html de cada sitio.")
    print("=" * 72)


async def crawl_site_with_browser(browser, site):
    """
    Igual que crawl_site(), pero recibe el navegador ya abierto para evitar
    reiniciarlo entre los tres dominios.
    """
    queue = asyncio.Queue()
    queued, visited, in_progress, failed = set(), set(), set(), {}
    processed = 0
    page_owner = {}

    for url in site.starts:
        if site.allowed_page(url) and url not in queued:
            queue.put_nowait((url, 0, 1))
            queued.add(url)

    context = await browser.new_context(
        ignore_https_errors=True,
        viewport={"width": 1440, "height": 1100},
        locale=BROWSER_LOCALE,
        color_scheme="light",
        service_workers="allow",
    )
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
    context.set_default_timeout(18000)

    pages = [await context.new_page() for _ in range(WORKERS)]

    for page in pages:
        def on_response(response, bound_page=page):
            owner = page_owner.get(bound_page)
            if owner is None:
                return
            task = asyncio.create_task(save_resource(owner, response))
            owner.resource_tasks.add(task)
            task.add_done_callback(owner.resource_tasks.discard)
        page.on("response", on_response)

    async def worker(worker_id, page):
        nonlocal processed

        while True:
            item = await queue.get()
            try:
                if item is None:
                    return

                url, depth, attempt = item
                url = clean_url(url)

                if (
                    not url or url in visited or url in in_progress
                    or depth > MAX_DEPTH or processed >= site.max_pages
                ):
                    continue

                in_progress.add(url)
                page_owner[page] = site
                success = False

                try:
                    print(f"[{site.name}] [W{worker_id}] {processed + 1}: {url}")

                    response = await page.goto(
                        url, wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT
                    )
                    if response is not None and response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}")

                    try:
                        await page.wait_for_load_state(
                            "networkidle", timeout=NETWORK_IDLE_TIMEOUT
                        )
                    except PlaywrightTimeoutError:
                        pass

                    await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
                    await open_details(page)
                    await scroll_to_load_lazy(page)
                    await expand_more(page)
                    await scroll_to_load_lazy(page)

                    final_url = clean_url(page.url) or url
                    if not site.allowed_page(final_url):
                        raise RuntimeError(
                            f"Redirección fuera de rutas permitidas: {final_url}"
                        )

                    html = await page.content()
                    if not html.strip():
                        raise RuntimeError("HTML vacío")

                    path = site.local_path(
                        final_url, force_html=True, content_type="text/html"
                    )
                    if not site.save(path, html.encode("utf-8", errors="replace")):
                        raise RuntimeError("Límite de almacenamiento alcanzado")

                    site.register(url, path, is_page=True)
                    site.register(final_url, path, is_page=True)
                    visited.update((url, final_url))
                    processed += 1
                    success = True

                    links = await extract_page_links(page, final_url, site)
                    for link in links:
                        if (
                            link not in queued and link not in visited
                            and link not in in_progress
                        ):
                            queued.add(link)
                            queue.put_nowait((link, depth + 1, 1))

                    print(
                        f"      OK | páginas {processed} | URLs {len(queued)} | "
                        f"{site.bytes_saved / 1048576:.1f} MB"
                    )

                except Exception as exc:
                    site.errors.append(f"PÁGINA | {url} | {exc}")
                    print("      ERROR:", exc)

                finally:
                    in_progress.discard(url)
                    if not success and url:
                        if attempt < MAX_PAGE_ATTEMPTS:
                            await asyncio.sleep(0.6 * attempt)
                            queue.put_nowait((url, depth, attempt + 1))
                        else:
                            failed[url] = attempt

            finally:
                queue.task_done()

    workers = [
        asyncio.create_task(worker(i + 1, page))
        for i, page in enumerate(pages)
    ]

    await queue.join()

    for _ in workers:
        queue.put_nowait(None)
    await queue.join()
    await asyncio.gather(*workers, return_exceptions=True)

    while site.resource_tasks:
        await asyncio.gather(*list(site.resource_tasks), return_exceptions=True)

    for page in pages:
        try:
            await page.close()
        except Exception:
            pass

    await context.close()

    return {
        "paginas_procesadas": processed,
        "urls_descubiertas": len(queued),
        "paginas_fallidas": failed,
    }


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProceso interrumpido por el usuario.")
