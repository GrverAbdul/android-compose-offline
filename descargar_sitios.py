
import os
import re
import json
import hashlib
from pathlib import Path
from collections import deque
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SITES = [
    #{"name": "Codica Basicos", "start": "https://basicos.codica.la/", "hosts": {"basicos.codica.la"}, "max_total_mb": 4096},
    #{"name": "W3Docs", "start": "https://es.w3docs.com/", "hosts": {"es.w3docs.com"}, "max_total_mb": 2048},
    {"name": "eLearner", "start": "https://elearner.app/es/", "hosts": {"elearner.app"}, "max_total_mb": 4096},
    {"name": "DevSchool", "start": "https://devschool.es/", "hosts": {"devschool.es"}, "max_total_mb": 4096},
    {"name": "CodeRacha", "start": "https://carloswilches.com/", "hosts": {"carloswilches.com", "www.carloswilches.com"}, "max_total_mb": 4096},
    {"name": "GameCode", "start": "https://gamecode.me/", "hosts": {"gamecode.me"}, "resource_hosts": {"assets.gamecode.me", "cdn.jsdelivr.net", "raw.githubusercontent.com"}, "max_total_mb": 4096},
    {"name": "Aprende Web desde Cero", "start": "https://cacg-code.github.io/web-desde-cero/", "hosts": {"cacg-code.github.io"}, "resource_hosts": {"cdn.jsdelivr.net"}, "max_total_mb": 4096},
    {"name": "Tutoriales Programacion Ya", "start": "https://www.tutorialesprogramacionya.com/", "hosts": {"www.tutorialesprogramacionya.com", "tutorialesprogramacionya.com"}, "max_total_mb": 4096},
    {"name": "Aprende JavaScript", "start": "https://www.aprendejavascript.dev/", "hosts": {"www.aprendejavascript.dev", "aprendejavascript.dev"}, "max_total_mb": 4096},
]
ROOT = Path("salida")
METADATA = ROOT / "_metadata"
URL_MAP_FILE = METADATA / "url-map.json"
PAGE_MAP_FILE = METADATA / "page-map.json"
MAX_PAGES_PER_SITE = 7500
MAX_DEPTH = 25
NAVIGATION_TIMEOUT = 45000
WAIT_AFTER_LOAD_MS = 1200
MAX_RESOURCE_SIZE = 500 * 1024 * 1024
IGNORED_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "blob:", "intent:", "about:")
NON_PAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".css", ".js", ".mjs", ".json", ".xml", ".txt", ".pdf", ".mp4", ".webm", ".mov", ".mp3", ".wav", ".ogg", ".zip", ".rar", ".7z", ".tar", ".gz", ".sql", ".db", ".apk", ".exe", ".msi", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".bin", ".webmanifest"}
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
errors = []
visited_by_site = {}
site_storage = {}
def create_site_tracker(site):
    limit_mb = site.get("max_total_mb", 4096)
    return {"limite_mb": limit_mb, "limite_bytes": int(limit_mb * 1024 * 1024), "bytes_guardados": 0, "archivos": {}, "limite_alcanzado": False}
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
            print("\n[ LIMITE ALCANZADO ]" f"\nArchivo: {description}" f"\nTamaño: {format_mb(new_size)} MB" f"\nGuardado: {format_mb(tracker['bytes_guardados'])} MB" f"\nLímite: {tracker['limite_mb']} MB")
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
        if parts.scheme.lower() not in ("http", "https"):
            return ""
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
    except Exception:
        return ""
def is_internal(url, site):
    try:
        return urlsplit(url).hostname in site["hosts"]
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
        return path_ext if path_ext in (".zip", ".docx", ".xlsx", ".pptx", ".apk") else ".zip"
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
    return ROOT / relative
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
            register_url("https://" + host + "/", html_file, is_page=True)
            register_url("https://" + host + "/index.html", html_file, is_page=True)
        elif web_path.lower().endswith("/index.html"):
            directory = web_path[:-10]
            if not directory.endswith("/"):
                directory += "/"
            register_url("https://" + host + directory, html_file, is_page=True)
            register_url("https://" + host + web_path, html_file, is_page=True)
        else:
            inferred_path = web_path[:-5] if web_path.lower().endswith(".html") else web_path
            register_url("https://" + host + inferred_path, html_file, is_page=True)
            register_url("https://" + host + inferred_path + ".html", html_file, is_page=True)
def save_response_resource(response, site, tracker):
    try:
        if tracker["limite_alcanzado"]:
            return
        resource_type = response.request.resource_type
        if resource_type in ("document", "websocket"):
            return
        if response.status not in (200, 206):
            return
        url = clean_url(response.url)
        if not url or not is_allowed_resource(url, site):
            return
        allowed = {"stylesheet", "script", "image", "font", "manifest", "media"}
        if resource_type not in allowed:
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
        data = response.body()
        if not data:
            return
        if len(data) > MAX_RESOURCE_SIZE:
            print("Recurso individual demasiado grande:", url)
            return
        if looks_like_html(data):
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
        errors.append(f"RECURSO | {response.url} | {exc}")
def extract_page_links(page, current_url, site):
    results = []
    try:
        elements = page.locator("a[href], area[href], iframe[src], frame[src], source[src], video[src], audio[src], link[href]").evaluate_all("""
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
            if not is_internal(absolute, site):
                continue
            results.append(absolute)
    except Exception as exc:
        errors.append(f"EXTRACCION ENLACES | {current_url} | {exc}")
    return list(dict.fromkeys(results))
def is_probable_page(url):
    extension = Path(urlsplit(url).path).suffix.lower()
    return extension not in NON_PAGE_EXTENSIONS
def crawl_site(browser, site):
    print("\n" + "=" * 65)
    print("SITIO:", site["name"])
    print("INICIO:", site["start"])
    print("LÍMITE TOTAL:", site.get("max_total_mb", 2048), "MB")
    print("LÍMITE POR ARCHIVO:", format_mb(MAX_RESOURCE_SIZE), "MB")
    print("=" * 65)
    tracker = create_site_tracker(site)
    site_storage[site["name"]] = tracker
    context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000}, locale="es-ES")
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
    context.set_default_timeout(15000)
    context.on("response", lambda response: save_response_resource(response, site, tracker))
    start = clean_url(site["start"])
    queue = deque([(start, 0)])
    queued = {start}
    visited = set()
    pages_saved = 0
    while queue and pages_saved < MAX_PAGES_PER_SITE and not tracker["limite_alcanzado"]:
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
            print(f"[{site['name']}] ({pages_saved + 1}/{MAX_PAGES_PER_SITE}) {current_url}")
            response = page.goto(current_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            if response is None:
                continue
            if response.status >= 400:
                errors.append(f"HTTP {response.status} | {current_url}")
                continue
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
            try:
                page.evaluate("""
                    () => {
                        document.querySelectorAll('img[loading="lazy"]').forEach(img => {
                            img.loading = 'eager';
                        });
                    }
                """)
            except Exception:
                pass
            try:
                page.locator("details").evaluate_all("elements => elements.forEach(el => el.open = true)")
            except Exception:
                pass
            final_url = clean_url(page.url) or current_url
            html = page.content()
            html_bytes = html.encode("utf-8", errors="replace")
            if not looks_like_html(html_bytes):
                errors.append(f"NO HTML | {current_url}")
                continue
            local_file = safe_local_path(final_url, force_html=True, content_type="text/html")
            if not save_limited_file(local_file, html_bytes, tracker, description=final_url):
                break
            register_url(current_url, local_file, is_page=True)
            register_url(final_url, local_file, is_page=True)
            pages_saved += 1
            print(f"   Almacenamiento: {format_mb(tracker['bytes_guardados'])} / {tracker['limite_mb']} MB")
            links = extract_page_links(page, final_url, site)
            for link in links:
                if link in queued or link in visited:
                    continue
                if is_probable_page(link):
                    queue.append((link, depth + 1))
                    queued.add(link)
        except PlaywrightTimeoutError:
            errors.append(f"TIMEOUT | {current_url}")
        except Exception as exc:
            errors.append(f"PAGINA | {current_url} | {exc}")
        finally:
            try:
                page.close()
            except Exception:
                pass
    visited_by_site[site["name"]] = {
        "paginas_guardadas": pages_saved,
        "urls_visitadas": len(visited),
        "urls_pendientes": len(queue),
        "almacenamiento_MB": format_mb(tracker["bytes_guardados"]),
        "limite_MB": tracker["limite_mb"],
        "limite_alcanzado": tracker["limite_alcanzado"],
    }
    context.close()
    print("\n" + "-" * 55)
    print("FINALIZADO:", site["name"])
    print("Páginas:", pages_saved)
    print("Almacenamiento:", format_mb(tracker["bytes_guardados"]), "MB de", tracker["limite_mb"], "MB")
    print("Límite alcanzado:", "SÍ" if tracker["limite_alcanzado"] else "NO")
    print("-" * 55)
def find_local_target(url):
    url = clean_url(url)
    if not url:
        return None
    candidates = [url]
    parts = urlsplit(url)
    without_query = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if without_query not in candidates:
        candidates.append(without_query)
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
    return os.path.relpath(Path(target_file), start=Path(source_file).parent).replace(os.sep, "/")
def rewrite_html():
    for html_file in ROOT.rglob("*.html"):
        if "_metadata" in html_file.parts:
            continue
        source_url = file_to_url.get(str(html_file))
        if not source_url:
            for known_file, known_url in file_to_url.items():
                if Path(known_file) == html_file:
                    source_url = known_url
                    break
        if not source_url:
            continue
        try:
            soup = BeautifulSoup(html_file.read_text(encoding="utf-8", errors="replace"), "html.parser")
            for tag in soup.find_all(True):
                for attr in ("href", "src", "poster", "action", "data-src", "data-lazy-src", "data-original", "data-href", "data-url"):
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
    url_pattern = re.compile(r"url\(\s*([\"']?)(.*?)\1\s*\)", re.IGNORECASE)
    import_pattern = re.compile(r"@import\s+([\"'])(.*?)\1", re.IGNORECASE)
    for css_file in ROOT.rglob("*.css"):
        if "_metadata" in css_file.parts:
            continue
        source_url = file_to_url.get(str(css_file))
        if not source_url:
            for known_file, known_url in file_to_url.items():
                if Path(known_file) == css_file:
                    source_url = known_url
                    break
        if not source_url:
            continue
        try:
            content = css_file.read_text(encoding="utf-8", errors="replace")
            def replace_url(match):
                raw = match.group(2).strip()
                if not raw or raw.startswith(("data:", "blob:", "#")):
                    return match.group(0)
                absolute = clean_url(urljoin(source_url, raw))
                target = find_local_target(absolute) if absolute else None
                if not target:
                    return match.group(0)
                quote_char = match.group(1)
                return "url(" + quote_char + relative_target(css_file, target) + quote_char + ")"
            content = url_pattern.sub(replace_url, content)
            def replace_import(match):
                raw = match.group(2).strip()
                absolute = clean_url(urljoin(source_url, raw))
                target = find_local_target(absolute) if absolute else None
                if not target:
                    return match.group(0)
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
        "errores": len(errors),
        "nota": "Los límites por sitio contabilizan los archivos guardados durante esta ejecución. El tamaño total de la carpeta salida también puede incluir archivos de ejecuciones anteriores. Los contenidos dependientes de APIs, autenticación, streaming o servidores externos pueden no funcionar completamente sin conexión."
    }
    Path("informe-descarga.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("errores-descarga.txt").write_text("\n".join(errors), encoding="utf-8")
    print("\n========== INFORME FINAL ==========")
    print(json.dumps(report, ensure_ascii=False, indent=2))
def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    METADATA.mkdir(parents=True, exist_ok=True)
    print("=" * 65)
    print("DESCARGADOR DE DOCUMENTACIÓN OFFLINE")
    print("SITIOS CONFIGURADOS:", len(SITES))
    print("MÁXIMO DE PÁGINAS POR SITIO:", MAX_PAGES_PER_SITE)
    print("PROFUNDIDAD MÁXIMA:", MAX_DEPTH)
    print("MÁXIMO POR ARCHIVO:", format_mb(MAX_RESOURCE_SIZE), "MB")
    print("=" * 65)
    load_previous_maps()
    infer_old_pages()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for site in SITES:
                crawl_site(browser, site)
        finally:
            browser.close()
    print("\n===== GUARDANDO MAPAS =====")
    save_maps()
    print("\n===== REESCRIBIENDO HTML =====")
    rewrite_html()
    print("\n===== REESCRIBIENDO CSS =====")
    rewrite_css()
    print("\n===== CREANDO ÍNDICE OFFLINE =====")
    create_offline_index()
    save_maps()
    print("\n===== GENERANDO INFORME =====")
    create_report()
    print("\n===== PROCESO TERMINADO =====")
if __name__ == "__main__":
    main()
