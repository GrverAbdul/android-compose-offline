import os
import asyncio
import re
import json
import hashlib
from pathlib import Path
from functools import lru_cache
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# --- IMPORTACIÓN OPCIONAL DE PLAYWRIGHT-STEALTH ---
# Si no lo tienes instalado, puedes instalarlo con: pip install playwright-stealth
try:
    from playwright_stealth import stealth_async
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

# ============================================================
# CONFIGURACIÓN DE SITIOS
# ============================================================
SITES = [

    # --- NUEVOS SITIOS AÑADIDOS ---
    # 1. PONN.IO (Inglés y Español)
    #    Se usa 'expand_load_more' porque el catálogo de libros se carga dinámicamente.
    #    'allowed_paths' restringe la descarga a las rutas de inglés y español.
    {
        "name": "Ponn.io - Inglés y Español",
        "start": "https://ponn.io/es/ingles/libros/",
        "starts": [
            "https://ponn.io/es/ingles/libros/",
            "https://ponn.io/en/english/books/"
        ],
        "hosts": {"ponn.io"},
        "allowed_paths": ["/es/", "/en/"],  # Solo permite rutas que empiecen por /es/ o /en/
        "exclude_paths": ["/de/", "/fr/", "/it/", "/pt/", "/ru/", "/ja/", "/ko/", "/zh/"],  # Bloquea otros idiomas
        "expand_load_more": True,           # Activa la función de "Cargar más"
        "max_load_more_clicks": 100,        # Límite de seguridad
        "max_total_mb": 10000
    },

    # 2. MINIMAL BOOKS (Inglés y Español)
    #    Se asume que las rutas de idioma son /en/ y /es/. Si solo existe /es/, el crawler simplemente no encontrará enlaces en /en/.
    {
        "name": "Minimal Books",
        "start": "https://minimalbooks.com/",
        "starts": [
            "https://minimalbooks.com/",
            "https://minimalbooks.com/es/",
            "https://minimalbooks.com/en/"
        ],
        "hosts": {"minimalbooks.com"},
        "allowed_paths": ["/", "/es/", "/en/"], # Permite la raíz y los dos idiomas
        "exclude_paths": [
            "/wp-admin/", "/wp-login.php", "/cart/", "/checkout/"
        ],
        "max_total_mb": 5000
    },

    # 3. BABELOTHECA
    #    No tiene una estructura de rutas por idioma clara. Se usa 'exclude_paths' para bloquear prefijos de otros idiomas conocidos.
    {
        "name": "Babelotheca",
        "start": "https://babelotheca.com/bilingual-books",
        "starts": ["https://babelotheca.com/bilingual-books"],
        "hosts": {"babelotheca.com"},
        "allowed_paths": None, # Se deja abierto porque las rutas de los libros pueden no compartir prefijo
        "exclude_paths": [
            "/login", "/sign-in", "/signup", "/register",
            "/pricing", "/checkout", "/translate",
            "/account", "/settings", "/api/",
            # Bloqueo de otros idiomas (aproximado)
            "/de/", "/fr/", "/it/", "/pt/", "/ru/", "/ja/", "/ko/", "/zh/"
        ],
        "max_total_mb": 5000
    },
]

# ============================================================
# CONSTANTES Y VARIABLES GLOBALES
# ============================================================
ROOT = Path("salida")
METADATA = ROOT / "_metadata"
URL_MAP_FILE = METADATA / "url-map.json"
PAGE_MAP_FILE = METADATA / "page-map.json"

MAX_PAGES_PER_SITE = 50000
MAX_DEPTH = 35
NAVIGATION_TIMEOUT = 45000
NETWORK_IDLE_TIMEOUT = 2500
WAIT_AFTER_LOAD_MS = 300
PAGE_WORKERS = 4 # Reducido ligeramente para ser más cuidadoso con los sitios nuevos
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
local_path_owners = {}
errors = []
visited_by_site = {}
site_storage = {}


# ============================================================
# FUNCIONES AUXILIARES (Se mantienen casi todas las tuyas)
# ============================================================
def create_site_tracker(site):
    limit_mb = site.get("max_total_mb", 4096)
    return {
        "limite_mb": limit_mb,
        "limite_bytes": int(limit_mb * 1024 * 1024),
        "bytes_guardados": 0,
        "archivos": {},
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
        return urlunsplit((scheme, netloc, path, query, ""))
    except Exception:
        return ""


def is_internal(url, site):
    try:
        return urlsplit(url).hostname in site["hosts"]
    except Exception:
        return False


def is_allowed_page(url, site):
    if not is_internal(url, site):
        return False
    try:
        path = urlsplit(url).path or "/"
        for prefix in site.get("exclude_paths", []):
            prefix = str(prefix).rstrip("/") or "/"
            if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
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
    url = clean_url(url)
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
    return reserve_local_path(url, ROOT / relative)


def register_url(url, path, is_page=False):
    url = clean_url(url)
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
            print("Ruta anterior normalizada:", old, "->", new)
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
    print("Auditoría de rutas: archivos/carpetas inválidos =", len(invalid), "| colisiones =", len(collisions))
    if invalid:
        for item in invalid[:30]:
            errors.append(f"RUTA NO PORTABLE | {item}")
    if collisions:
        for key, paths in collisions.items():
            errors.append(f"COLISIÓN DE RUTA | {key} | {paths}")
    return not invalid and not collisions


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
        if "_metadata" in old_file.parts: continue
        try:
            data = old_file.read_bytes()
            if not looks_like_html(data): continue
            new_file = old_file.with_suffix(".html")
            if new_file.exists(): continue
            old_file.rename(new_file)
            print("HTML recuperado:", old_file, "->", new_file)
        except Exception as exc:
            errors.append(f"RECUPERAR BIN | {old_file} | {exc}")
    for html_file in ROOT.rglob("*.html"):
        if "_metadata" in html_file.parts: continue
        try:
            relative = html_file.relative_to(ROOT)
        except ValueError:
            continue
        if len(relative.parts) < 2: continue
        host = relative.parts[0]
        if host not in known_hosts: continue
        web_path = "/" + "/".join(relative.parts[1:])
        if web_path.lower() == "/index.html":
            register_url("https://" + host + "/", html_file, is_page=True)
            register_url("https://" + host + "/index.html", html_file, is_page=True)
        elif web_path.lower().endswith("/index.html"):
            directory = web_path[:-10]
            if not directory.endswith("/"): directory += "/"
            register_url("https://" + host + directory, html_file, is_page=True)
            register_url("https://" + host + web_path, html_file, is_page=True)
        else:
            inferred_path = web_path[:-5] if web_path.lower().endswith(".html") else web_path
            register_url("https://" + host + inferred_path, html_file, is_page=True)
            register_url("https://" + host + inferred_path + ".html", html_file, is_page=True)


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
                        print("Recurso individual demasiado grande:", url)
                        return
                except ValueError:
                    pass
            data = None
            for body_attempt in range(1, RESOURCE_BODY_RETRIES + 1):
                try:
                    data = await response.body()
                    if data: break
                except Exception as body_exc:
                    if body_attempt >= RESOURCE_BODY_RETRIES: raise body_exc
                    await asyncio.sleep(0.25 * body_attempt)
            if not data or len(data) > MAX_RESOURCE_SIZE or looks_like_html(data): return
            extension = detect_extension(url, content_type, data)
            path = safe_local_path(url, content_type=content_type)
            if path.suffix.lower() != extension:
                path = reserve_local_path(url, path.with_suffix(extension))
            if not save_limited_file(path, data, tracker, description=url): return
            register_url(url, path)
            asset_urls.add(url)
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
            absolute = clean_url(urljoin(current_url, value))
            if not absolute: continue
            if not is_allowed_page(absolute, site): continue
            results.append(absolute)
    except Exception as exc:
        errors.append(f"EXTRACCION ENLACES | {current_url} | {exc}")
    return list(dict.fromkeys(results))


def is_probable_page(url):
    extension = Path(urlsplit(url).path).suffix.lower()
    return extension not in NON_PAGE_EXTENSIONS


# ============================================================
# NUEVA FUNCIÓN: EXPANDIR "CARGAR MÁS"
# ============================================================
async def expand_load_more(page, site):
    """
    Pulsa repetidamente un botón de "Cargar más" / "Load more" hasta que no aparezcan enlaces nuevos.
    La búsqueda del botón es genérica y busca por texto visible.
    Si el sitio usa un selector CSS específico, puedes cambiarlo en la variable 'selector_boton'.
    """
    print(f"   [Expand] Iniciando expansión de 'Cargar más' para: {site['name']}")
    clicks = 0
    without_changes = 0
    max_clicks = site.get("max_load_more_clicks", 100)

    # --- SELECTOR GENÉRICO ---
    # Busca botones o enlaces cuyo texto contenga "load more", "cargar más", etc.
    # Puedes reemplazar esto por un selector CSS específico si lo conoces.
    selector_boton = "button, a, [role='button']"

    while clicks < max_clicks:
        try:
            # 1. Intentar encontrar y pulsar el botón
            #    Se usa page.locator con un filtro de texto para ser más preciso.
            boton = page.locator(selector_boton).filter(
                has_text=re.compile(r"(load\s*more|cargar\s*más|ver\s*más|show\s*more)", re.IGNORECASE)
            ).first

            if await boton.count() == 0:
                print("   [Expand] No se encontró botón de 'Cargar más'.")
                break

            # Comprobar si el botón es visible y está habilitado
            if not await boton.is_visible() or not await boton.is_enabled():
                print("   [Expand] Botón no visible o deshabilitado.")
                break

            # 2. Guardar enlaces actuales antes de pulsar
            previous_links = set(
                await page.locator("a[href]").evaluate_all(
                    "elements => elements.map(el => el.href).filter(Boolean)"
                )
            )

            # 3. Pulsar el botón
            await boton.click()
            clicks += 1
            print(f"   [Expand] Clic {clicks} realizado.")

            # 4. Esperar a que aparezcan nuevos enlaces
            await page.wait_for_timeout(1500)  # Espera inicial
            current_links = set(
                await page.locator("a[href]").evaluate_all(
                    "elements => elements.map(el => el.href).filter(Boolean)"
                )
            )
            new_links = current_links - previous_links

            if new_links:
                without_changes = 0
                print(f"   [Expand]   -> {len(new_links)} enlaces nuevos detectados.")
            else:
                # A veces el contenido tarda un poco más en cargar. Se da una segunda oportunidad.
                await page.wait_for_timeout(2000)
                current_links = set(
                    await page.locator("a[href]").evaluate_all(
                        "elements => elements.map(el => el.href).filter(Boolean)"
                    )
                )
                new_links = current_links - previous_links
                if new_links:
                    without_changes = 0
                    print(f"   [Expand]   -> {len(new_links)} enlaces nuevos detectados (tras espera extra).")
                else:
                    without_changes += 1
                    print(f"   [Expand]   -> Sin cambios ({without_changes}/2).")

            # 5. Si no hay cambios en dos clics consecutivos, se considera terminado.
            if without_changes >= 2:
                print("   [Expand] Detenido: no aparecen más enlaces nuevos.")
                break

        except PlaywrightTimeoutError:
            print("   [Expand] Tiempo de espera agotado al interactuar con el botón.")
            break
        except Exception as exc:
            print(f"   [Expand] Error: {exc}")
            errors.append(f"LOAD_MORE | {site['name']} | {exc}")
            break

    print(f"   [Expand] Expansión terminada. Clics realizados: {clicks}")
    return clicks


# ============================================================
# FUNCIÓN PRINCIPAL DE CRAWLING (MODIFICADA)
# ============================================================
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

    # --- CONTEXTO ANTI-403 ---
    # Se añaden cabeceras realistas y un User-Agent de un navegador de escritorio.
    # Esto es crucial para sitios como ponn.io que pueden bloquear peticiones de bots.
    context = await browser.new_context(
        ignore_https_errors=True,
        viewport={"width": 1920, "height": 1080},
        locale="es-ES",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        extra_http_headers={
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
    )
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
    context.set_default_timeout(15000)

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

    worker_count = PAGE_WORKERS
    pages = [await context.new_page() for _ in range(worker_count)]

    # --- APLICAR STEALTH (SI ESTÁ DISPONIBLE) ---
    if STEALTH_AVAILABLE:
        for page in pages:
            await stealth_async(page)
        print("   [Anti-403] playwright-stealth aplicado a las páginas.")
    else:
        print("   [Anti-403] playwright-stealth no está instalado. Se usarán solo cabeceras personalizadas.")

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
                if pages_saved >= MAX_PAGES_PER_SITE or tracker["limite_alcanzado"]:
                    continue
                if not is_allowed_page(current_url, site) or not is_probable_page(current_url):
                    continue

                in_progress.add(current_url)
                claimed = True
                eligible_for_retry = True

                print(f"[{site['name']}] [W{worker_id}] ({pages_saved + 1}/{MAX_PAGES_PER_SITE}) Intento {attempt}/{MAX_PAGE_ATTEMPTS}: {current_url}")

                navigation_timed_out = False
                response = None
                previous_page_url = clean_url(page.url)

                for nav_attempt in range(1, NAVIGATION_RETRIES + 1):
                    try:
                        response = await page.goto(current_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                    except PlaywrightTimeoutError:
                        if nav_attempt < NAVIGATION_RETRIES:
                            await page.wait_for_timeout(RETRY_DELAY_MS)
                            continue
                        navigation_timed_out = True
                        errors.append(f"TIMEOUT NAVEGACIÓN | {current_url}")
                        response = None
                        break
                    if (response is not None and response.status in (429, 500, 502, 503, 504) and nav_attempt < NAVIGATION_RETRIES):
                        await page.wait_for_timeout(RETRY_DELAY_MS * nav_attempt)
                        continue
                    break

                if response is not None and response.status >= 400:
                    errors.append(f"HTTP {response.status} | {current_url}")
                    continue
                if response is None:
                    current_browser_url = clean_url(page.url)
                    if (not navigation_timed_out or current_browser_url in ("", clean_url("about:blank")) or current_browser_url == previous_page_url):
                        continue

                try:
                    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT)
                except PlaywrightTimeoutError:
                    pass

                # --- LAZY LOADING DE IMÁGENES ---
                try:
                    lazy_count = await page.locator('img[loading="lazy"], img[data-src], img[data-lazy-src]').count()
                    if lazy_count:
                        await page.evaluate("""
                            () => {
                                document.querySelectorAll('img[loading="lazy"], img[data-src], img[data-lazy-src]').forEach(img => {
                                    img.loading = 'eager';
                                    if (!img.src && img.dataset.src) img.src = img.dataset.src;
                                    if (!img.src && img.dataset.lazySrc) img.src = img.dataset.lazySrc;
                                });
                            }
                        """)
                        await page.evaluate("""async (maxSteps, delay) => {
                            const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
                            const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
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
                    await page.locator("details").evaluate_all("elements => elements.forEach(el => el.open = true)")
                except Exception:
                    pass

                # --- LLAMADA A LA NUEVA FUNCIÓN DE "CARGAR MÁS" ---
                if site.get("expand_load_more", False):
                    await expand_load_more(page, site)

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

                if pages_saved >= MAX_PAGES_PER_SITE or tracker["limite_alcanzado"]:
                    eligible_for_retry = False
                    continue

                local_file = safe_local_path(final_url, force_html=True, content_type="text/html")
                if not save_limited_file(local_file, html_bytes, tracker, description=final_url):
                    eligible_for_retry = False
                    continue

                register_url(current_url, local_file, is_page=True)
                register_url(final_url, local_file, is_page=True)
                visited.add(current_url)
                visited.add(final_url)
                pages_saved += 1
                success = True

                print(f"   Guardada: {pages_saved} | Almacenamiento: {format_mb(tracker['bytes_guardados'])} / {tracker['limite_mb']} MB")

                links = []
                for link_attempt in range(1, 3):
                    links = await extract_page_links(page, final_url, site)
                    if links: break
                    if link_attempt == 1: await asyncio.sleep(0.2)
                if not links:
                    errors.append(f"SIN ENLACES EXTRAÍDOS O PÁGINA SIN ENLACES | {final_url}")

                for link in links:
                    if link in queued or link in visited or link in in_progress: continue
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
                        print(f"   Reintentando página ({attempt + 1}/{MAX_PAGE_ATTEMPTS}) en {delay} ms: {current_url}")
                        await asyncio.sleep(delay / 1000)
                        queue.put_nowait((current_url, depth, attempt + 1))
                    else:
                        failed_pages[current_url] = attempt
                        errors.append(f"PÁGINA FALLIDA DEFINITIVA | intentos={attempt} | {current_url}")
                queue.task_done()

    workers = [asyncio.create_task(worker(index + 1, page)) for index, page in enumerate(pages)]
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


# ============================================================
# RESTO DE FUNCIONES (find_local_target, rewrite_html, etc.)
# ============================================================
@lru_cache(maxsize=500000)
def find_local_target(url):
    url = clean_url(url)
    if not url: return None
    candidates = [url]
    parts = urlsplit(url)
    without_query = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if without_query not in candidates: candidates.append(without_query)
    path = parts.path or "/"
    if path.endswith("/"):
        candidates.append(urlunsplit((parts.scheme, parts.netloc, path + "index.html", "", "")))
    elif not Path(path).suffix:
        candidates.append(urlunsplit((parts.scheme, parts.netloc, path + ".html", "", "")))
    if not path.endswith("/") and not Path(path).suffix:
        candidates.append(urlunsplit((parts.scheme, parts.netloc, path.rstrip("/") + "/index.html", "", "")))
    for candidate in candidates:
        candidate = clean_url(candidate)
        target = url_to_file.get(candidate)
        if target and Path(target).exists(): return Path(target)
    for candidate in candidates:
        candidate = clean_url(candidate)
        if not candidate: continue
        candidate_parts = urlsplit(candidate)
        host = candidate_parts.hostname or ""
        relative = unquote(candidate_parts.path or "/").lstrip("/")
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
                    absolute = clean_url(absolute_with_fragment)
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
    import_pattern = re.compile(r"@import\s+([\"'])(.*?)\1", re.IGNORECASE)
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
                quote_char = match.group(1)
                return "url(" + quote_char + relative_target(css_file, target) + quote_char + ")"
            content = url_pattern.sub(replace_url, content)
            def replace_import(match):
                raw = match.group(2).strip()
                absolute = clean_url(urljoin(source_url, raw))
                target = find_local_target(absolute) if absolute else None
                if not target: return match.group(0)
                quote_char = match.group(1)
                return "@import " + quote_char + relative_target(css_file, target) + quote_char
            content = import_pattern.sub(replace_import, content)
            css_file.write_text(content, encoding="utf-8")
        except Exception as exc:
            errors.append(f"REESCRITURA CSS | {css_file} | {exc}")


def save_maps():
    url_map = {}
    for url, path in url_to_file.items():
        try:
            url_map[url] = str(Path(path).relative_to(ROOT)).replace(os.sep, "/")
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
            links.append(f'<li><a href="{href}">{site["name"]}</a></li>')
        else:
            links.append(f'<li>{site["name"]} — página inicial no encontrada</li>')
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
    (ROOT / "INICIO_OFFLINE.html").write_text(html, encoding="utf-8")


def create_report():
    files = [p for p in ROOT.rglob("*") if p.is_file()]
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
            "limite_alcanzado": tracker["limite_alcanzado"],
        })
    report = {
        "sitios": [{"name": s["name"], "start": s["start"], "hosts": sorted(s["hosts"]), "max_total_mb": s.get("max_total_mb", 2048)} for s in SITES],
        "almacenamiento_por_sitio": storage_report,
        "paginas_por_sitio": visited_by_site,
        "paginas_en_mapa": len(page_urls),
        "recursos_capturados": len(asset_urls),
        "archivos_generados": len(files),
        "tamano_total_bytes": total_bytes,
        "tamano_total_MB": round(total_bytes / 1024 / 1024, 2),
        "limite_por_recurso_MB": format_mb(MAX_RESOURCE_SIZE),
        "network_idle_timeout_ms": NETWORK_IDLE_TIMEOUT,
        "trabajadores_simultaneos": PAGE_WORKERS,
        "reintentos_navegacion": NAVIGATION_RETRIES,
        "espera_adicional_ms": WAIT_AFTER_LOAD_MS,
        "errores": len(errors),
        "nota": "Los límites por sitio contabilizan los archivos guardados durante esta ejecución. El tamaño total de la carpeta salida también puede incluir archivos de ejecuciones anteriores. Los contenidos dependientes de APIs, autenticación, streaming o servidores externos pueden no funcionar completamente sin conexión."
    }
    Path("informe-descarga.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("errores-descarga.txt").write_text("\n".join(errors), encoding="utf-8")
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
    normalize_existing_paths()
    async with async_playwright() as playwright:
        # Se añade --disable-blink-features=AutomationControlled para intentar ocultar la automatización.
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
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
    audit_artifact_paths()
    save_maps()
    print("\n===== GENERANDO INFORME =====")
    create_report()
    print("\n===== PROCESO TERMINADO =====")


if __name__ == "__main__":
    asyncio.run(main())
