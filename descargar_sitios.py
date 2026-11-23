import asyncio
import hashlib
import json
import os
import re
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIGURACIÓN: SOLO STORYTELLER (INGLÉS + ESPAÑOL)
# ============================================================

SITE_NAME = "Storyteller - Ingles y Espanol"
START_URLS = [
    "https://storytellerenglish.com/es/stories/",
    "https://storytellerenglish.com/en/stories/",
    "https://storytellerenglish.com/es/",
    "https://storytellerenglish.com/en/",
]
ALLOWED_HOSTS = {"storytellerenglish.com", "www.storytellerenglish.com"}
ALLOWED_LANGUAGES = {"en", "es"}

ROOT = Path("salida")
METADATA = ROOT / "_metadata"

MAX_PAGES = 500000
MAX_DEPTH = 35
PAGE_WORKERS = 4
NAVIGATION_TIMEOUT = 45000
NETWORK_IDLE_TIMEOUT = 3000
WAIT_AFTER_LOAD_MS = 500
MAX_LOAD_MORE_CLICKS = 100
LOAD_MORE_WAIT_MS = 1000
MAX_RESOURCE_SIZE = 700 * 1024 * 1024
MAX_TOTAL_MB = 10000
MAX_TOTAL_BYTES = MAX_TOTAL_MB * 1024 * 1024
MAX_PAGE_ATTEMPTS = 3

IGNORED_SCHEMES = (
    "mailto:", "tel:", "javascript:", "data:", "blob:", "intent:", "about:"
)
NON_PAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".mjs", ".json", ".xml", ".txt", ".pdf",
    ".mp4", ".webm", ".mov", ".mp3", ".wav", ".ogg",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".sql", ".db",
    ".apk", ".exe", ".msi", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".bin", ".webmanifest"
}

errors = []
saved_pages = set()
saved_resources = set()
url_to_file = {}
file_to_url = {}
resource_tasks = set()
total_saved_bytes = 0
total_saved_files = 0


def clean_url(url):
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
        if port and not ((scheme == "http" and port == 80) or
                         (scheme == "https" and port == 443)):
            host = f"{host}:{port}"
        userinfo = ""
        if "@" in p.netloc:
            userinfo = p.netloc.rsplit("@", 1)[0] + "@"
        path = p.path or "/"

        def normalize_escape(match):
            value = int(match.group(1), 16)
            char = chr(value)
            if char.isalnum() or char in "-._~":
                return char
            return "%" + match.group(1).upper()

        path = re.sub(r"%([0-9a-fA-F]{2})", normalize_escape, path)
        query = re.sub(
            r"%([0-9a-fA-F]{2})",
            lambda m: "%" + m.group(1).upper(),
            p.query
        )
        return urlunsplit((scheme, userinfo + host, path, query, ""))
    except Exception:
        return ""


def is_allowed_page(url):
    url = clean_url(url)
    if not url:
        return False
    p = urlsplit(url)
    if p.hostname not in ALLOWED_HOSTS:
        return False
    first_segment = (p.path or "/").strip("/").split("/", 1)[0].lower()
    return first_segment in ALLOWED_LANGUAGES


def is_probable_page(url):
    return Path(urlsplit(url).path).suffix.lower() not in NON_PAGE_EXTENSIONS


def safe_component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(value))
    value = value.rstrip(" .") or "_"
    if value.split(".", 1)[0].upper() in (
        {"CON", "PRN", "AUX", "NUL"}
        | {f"COM{i}" for i in range(1, 10)}
        | {f"LPT{i}" for i in range(1, 10)}
    ):
        value = "_" + value
    if len(value) > 160:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        value = value[:140] + "__" + digest
    return value


def local_path_for(url, force_html=False, content_type=""):
    url = clean_url(url)
    if url in url_to_file:
        return Path(url_to_file[url])

    p = urlsplit(url)
    host = p.hostname or "unknown"
    parts = [safe_component(host)]
    web_path = unquote(p.path or "/")
    if web_path.endswith("/"):
        web_path += "index"
    parts.extend(safe_component(x) for x in web_path.strip("/").split("/") if x)
    path = ROOT.joinpath(*parts)

    mime = (content_type or "").split(";", 1)[0].lower()
    if force_html or mime in ("text/html", "application/xhtml+xml"):
        if path.suffix.lower() not in (".html", ".htm"):
            path = path.with_suffix(".html")
    elif not path.suffix:
        ext = {
            "text/css": ".css",
            "application/javascript": ".js",
            "text/javascript": ".js",
            "application/json": ".json",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "font/woff2": ".woff2",
            "font/woff": ".woff",
        }.get(mime, ".bin")
        path = path.with_suffix(ext)

    if p.query:
        digest = hashlib.sha1(p.query.encode("utf-8")).hexdigest()[:10]
        path = path.with_name(path.stem + "__q_" + digest + path.suffix)

    # Evita sobrescribir archivos distintos con el mismo nombre local.
    if path.exists() and url not in file_to_url.values():
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        path = path.with_name(path.stem + "__" + digest + path.suffix)
    return path


def save_bytes(path, data):
    global total_saved_bytes, total_saved_files
    path = Path(path)
    old_size = path.stat().st_size if path.exists() else 0
    difference = len(data) - old_size
    if difference > 0 and total_saved_bytes + difference > MAX_TOTAL_BYTES:
        print(f"[LÍMITE] Se omite: {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    total_saved_bytes += difference
    if old_size == 0:
        total_saved_files += 1
    return True


def register_url(url, path, is_page=False):
    url = clean_url(url)
    if not url:
        return
    path = Path(path)
    url_to_file[url] = path
    file_to_url.setdefault(str(path), url)
    if is_page:
        saved_pages.add(url)


async def expand_load_more(page, current_url):
    """Pulsa repetidamente Load more / Cargar más antes de capturar el HTML."""
    pattern = re.compile(
        r"(load\s*more|show\s*more|more\s*stories|"
        r"cargar\s*m[aá]s|ver\s*m[aá]s|mostrar\s*m[aá]s|"
        r"m[aá]s\s*historias|m[aá]s\s*cuentos)",
        re.IGNORECASE
    )

    clicks = 0
    stable_rounds = 0
    max_stable_rounds = 2

    while clicks < MAX_LOAD_MORE_CLICKS:
        before = await page.evaluate("""() => ({
            anchors: document.querySelectorAll('a[href]').length,
            cards: document.querySelectorAll(
                'article, [class*="story"], [class*="card"], [data-story]'
            ).length,
            bodyText: (document.body?.innerText || '').length,
            height: Math.max(
                document.body?.scrollHeight || 0,
                document.documentElement?.scrollHeight || 0
            )
        })""")

        # Busca botones y elementos con role=button. También admite enlaces
        # explícitamente marcados como botones, sin navegar a enlaces normales.
        clicked = await page.evaluate("""(patternSource) => {
            const pattern = new RegExp(patternSource, 'i');
            const selectors = [
                'button',
                '[role="button"]',
                'input[type="button"]',
                'input[type="submit"]'
            ];
            const candidates = Array.from(
                document.querySelectorAll(selectors.join(','))
            );

            for (const el of candidates) {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const visible =
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    rect.width > 0 &&
                    rect.height > 0;
                const disabled = el.disabled ||
                    el.getAttribute('aria-disabled') === 'true';

                const text = [
                    el.innerText || '',
                    el.textContent || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                    el.value || ''
                ].join(' ').replace(/\\s+/g, ' ').trim();

                if (visible && !disabled && pattern.test(text)) {
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return {clicked: true, text: text.slice(0, 140)};
                }
            }
            return {clicked: false, text: ''};
        }""", pattern.pattern)

        if not clicked.get("clicked"):
            break

        clicks += 1
        print(
            f"      [LOAD MORE] Clic {clicks}/{MAX_LOAD_MORE_CLICKS}: "
            f"{clicked.get('text', '')}"
        )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=min(NETWORK_IDLE_TIMEOUT, 4000)
            )
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(LOAD_MORE_WAIT_MS)

        after = await page.evaluate("""() => ({
            anchors: document.querySelectorAll('a[href]').length,
            cards: document.querySelectorAll(
                'article, [class*="story"], [class*="card"], [data-story]'
            ).length,
            bodyText: (document.body?.innerText || '').length,
            height: Math.max(
                document.body?.scrollHeight || 0,
                document.documentElement?.scrollHeight || 0
            )
        })""")

        grew = (
            after["anchors"] > before["anchors"]
            or after["cards"] > before["cards"]
            or after["bodyText"] > before["bodyText"]
            or after["height"] > before["height"]
        )

        if grew:
            stable_rounds = 0
        else:
            stable_rounds += 1
            if stable_rounds >= max_stable_rounds:
                print("      [LOAD MORE] No se detectó contenido nuevo; se detiene.")
                break

    if clicks >= MAX_LOAD_MORE_CLICKS:
        print(f"      [LOAD MORE] Se alcanzó el máximo de {MAX_LOAD_MORE_CLICKS} clics.")

    if clicks:
        print(f"      [LOAD MORE] Total: {clicks} clic(s).")
    return clicks


async def extract_links(page, current_url):
    try:
        values = await page.locator(
            "a[href], area[href], iframe[src], frame[src], "
            "source[src], video[src], audio[src], link[href]"
        ).evaluate_all("""els => els.map(el =>
            el.href || el.src || el.getAttribute('href') ||
            el.getAttribute('src') || ''
        )""")
    except Exception as exc:
        errors.append(f"EXTRAER ENLACES | {current_url} | {exc}")
        return []

    found = []
    for value in values:
        value = (value or "").strip()
        if not value or value.startswith(IGNORED_SCHEMES):
            continue
        absolute = clean_url(urljoin(current_url, value))
        if absolute and is_allowed_page(absolute) and is_probable_page(absolute):
            found.append(absolute)
    return list(dict.fromkeys(found))


async def save_resource_response(response):
    """Guarda CSS, JS, imágenes, fuentes, medios y respuestas textuales fetch/XHR."""
    try:
        if response.status not in (200, 206):
            return
        request = response.request
        resource_type = request.resource_type
        if resource_type in ("document", "websocket"):
            return

        url = clean_url(response.url)
        if not url:
            return
        host = urlsplit(url).hostname
        if host not in ALLOWED_HOSTS and host != "storytellerenglish.com":
            # Se guardan recursos externos usados por la página, por ejemplo CDN.
            if resource_type not in ("stylesheet", "script", "image", "font", "media"):
                return

        allowed_types = {
            "stylesheet", "script", "image", "font", "media", "manifest",
            "xhr", "fetch"
        }
        if resource_type not in allowed_types:
            return

        if url in url_to_file and Path(url_to_file[url]).exists():
            return

        headers = response.headers
        content_type = headers.get("content-type", "")
        length = headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_RESOURCE_SIZE:
                    return
            except ValueError:
                pass

        data = await response.body()
        if not data or len(data) > MAX_RESOURCE_SIZE:
            return
        if data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            return

        mime = content_type.split(";", 1)[0].lower()
        ext = {
            "text/css": ".css",
            "application/javascript": ".js",
            "text/javascript": ".js",
            "application/json": ".json",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "font/woff2": ".woff2",
            "font/woff": ".woff",
            "application/pdf": ".pdf",
            "video/mp4": ".mp4",
            "audio/mpeg": ".mp3",
        }.get(mime, Path(urlsplit(url).path).suffix or ".bin")

        path = local_path_for(url, content_type=content_type)
        if path.suffix.lower() != ext:
            path = path.with_suffix(ext)

        if save_bytes(path, data):
            register_url(url, path)
            saved_resources.add(url)
    except Exception as exc:
        errors.append(f"RECURSO | {response.url} | {exc}")


async def crawl():
    global resource_tasks

    queue = asyncio.Queue()
    for url in START_URLS:
        queue.put_nowait((clean_url(url), 0, 1))

    queued = set(clean_url(x) for x in START_URLS)
    visited = set()
    in_progress = set()
    failed = {}
    pages_processed = 0
    context = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1440, "height": 1000},
            locale="es-ES"
        )
        context.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
        context.set_default_timeout(15000)

        pages = [await context.new_page() for _ in range(PAGE_WORKERS)]

        def attach_listener(page):
            def on_response(response):
                task = asyncio.create_task(save_resource_response(response))
                resource_tasks.add(task)
                task.add_done_callback(resource_tasks.discard)
            page.on("response", on_response)

        for page in pages:
            attach_listener(page)

        async def worker(worker_id, page):
            nonlocal pages_processed

            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return

                    current_url, depth, attempt = item
                    current_url = clean_url(current_url)

                    if (
                        not current_url or current_url in visited
                        or current_url in in_progress or depth > MAX_DEPTH
                        or pages_processed >= MAX_PAGES
                    ):
                        continue

                    in_progress.add(current_url)
                    success = False

                    try:
                        print(
                            f"[{SITE_NAME}] [Trabajador {worker_id}] "
                            f"Página {pages_processed + 1}: {current_url}"
                        )

                        response = await page.goto(
                            current_url,
                            wait_until="domcontentloaded",
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

                        # Activa imágenes diferidas y recorre la página para lazy loading.
                        try:
                            await page.evaluate("""async () => {
                                document.querySelectorAll(
                                    'img[loading="lazy"], img[data-src], img[data-lazy-src]'
                                ).forEach(img => {
                                    img.loading = 'eager';
                                    if (!img.src && img.dataset.src) img.src = img.dataset.src;
                                    if (!img.src && img.dataset.lazySrc) img.src = img.dataset.lazySrc;
                                });
                                const pause = ms => new Promise(r => setTimeout(r, ms));
                                for (let i = 0; i < 20; i++) {
                                    window.scrollTo(0, document.body.scrollHeight);
                                    await pause(100);
                                }
                                window.scrollTo(0, 0);
                            }""")
                        except Exception:
                            pass

                        try:
                            await page.locator("details").evaluate_all(
                                "els => els.forEach(el => el.open = true)"
                            )
                        except Exception:
                            pass

                        # CAMBIO PRINCIPAL: expandir historias antes de capturar el DOM.
                        await expand_load_more(page, current_url)

                        final_url = clean_url(page.url) or current_url
                        if not is_allowed_page(final_url):
                            raise RuntimeError(
                                f"Redirección fuera de /en/ o /es/: {final_url}"
                            )

                        html = await page.content()
                        data = html.encode("utf-8", errors="replace")
                        if not data:
                            raise RuntimeError("HTML vacío")

                        path = local_path_for(
                            final_url, force_html=True, content_type="text/html"
                        )
                        if not save_bytes(path, data):
                            raise RuntimeError("Se alcanzó el límite de almacenamiento")

                        register_url(current_url, path, is_page=True)
                        register_url(final_url, path, is_page=True)
                        visited.add(current_url)
                        visited.add(final_url)
                        pages_processed += 1
                        success = True

                        # Los enlaces se extraen después de pulsar Load More,
                        # por lo que también se descubren las historias añadidas.
                        links = await extract_links(page, final_url)
                        for link in links:
                            if (
                                link not in queued and link not in visited
                                and link not in in_progress
                            ):
                                queued.add(link)
                                queue.put_nowait((link, depth + 1, 1))

                        print(
                            f"      Guardada. Páginas: {pages_processed} | "
                            f"URLs descubiertas: {len(queued)} | "
                            f"Almacenamiento: {total_saved_bytes / 1024 / 1024:.2f} MB"
                        )

                    except Exception as exc:
                        errors.append(f"PÁGINA | {current_url} | {exc}")
                        print(f"      ERROR: {exc}")

                    finally:
                        in_progress.discard(current_url)
                        if not success and current_url:
                            if attempt < MAX_PAGE_ATTEMPTS:
                                await asyncio.sleep(0.5 * attempt)
                                queue.put_nowait((current_url, depth, attempt + 1))
                            else:
                                failed[current_url] = attempt
                finally:
                    queue.task_done()

        workers = [
            asyncio.create_task(worker(i + 1, page))
            for i, page in enumerate(pages)
        ]

        # join() espera también a las páginas nuevas añadidas por los workers.
        await queue.join()

        for _ in workers:
            queue.put_nowait(None)
        await queue.join()
        await asyncio.gather(*workers, return_exceptions=True)

        # Espera a que terminen las descargas de recursos iniciadas por respuestas.
        while resource_tasks:
            await asyncio.gather(*list(resource_tasks), return_exceptions=True)

        for page in pages:
            try:
                await page.close()
            except Exception:
                pass

        await context.close()
        await browser.close()

    return {
        "paginas_procesadas": pages_processed,
        "urls_descubiertas": len(queued),
        "paginas_fallidas": failed,
    }


def find_local_target(url):
    url = clean_url(url)
    if not url:
        return None

    candidates = [url]
    p = urlsplit(url)
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
        target = url_to_file.get(clean_url(candidate))
        if target and Path(target).is_file():
            return Path(target)

    for candidate in candidates:
        cp = urlsplit(candidate)
        local = ROOT / (cp.hostname or "") / unquote(cp.path or "/").lstrip("/")
        if local.is_dir():
            local = local / "index.html"
        if local.is_file():
            return local
    return None


def relative_target(source, target):
    return os.path.relpath(target, Path(source).parent).replace(os.sep, "/")


def rewrite_offline_links():
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
                    "href", "src", "poster", "data-src", "data-lazy-src",
                    "data-original", "data-href", "data-url"
                ):
                    value = tag.get(attr)
                    if not isinstance(value, str) or not value.strip():
                        continue
                    value = value.strip()
                    if value.startswith(IGNORED_SCHEMES) or value.startswith("#"):
                        continue
                    absolute_full = urljoin(source_url, value)
                    parsed = urlsplit(absolute_full)
                    absolute = clean_url(absolute_full)
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


def create_index():
    entries = []
    for url in START_URLS:
        target = find_local_target(url)
        if target:
            href = os.path.relpath(target, ROOT).replace(os.sep, "/")
            label = "Español" if "/es/" in url else "English"
            entries.append(f'<li><a href="{href}">{label}: {url}</a></li>')
        else:
            entries.append(f"<li>No descargada: {url}</li>")

    page = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Storyteller offline</title>
<style>
body{font-family:Arial,sans-serif;max-width:950px;margin:40px auto;padding:20px;line-height:1.6;background:#f3f5f7;color:#222}
main{background:#fff;padding:28px;border-radius:12px}
a{overflow-wrap:anywhere}
li{margin:14px 0}
</style>
</head>
<body><main>
<h1>Storyteller — biblioteca offline</h1>
<p>Selecciona el idioma o catálogo:</p>
<ul>
""" + "\n".join(entries) + """
</ul>
</main></body></html>"""
    (ROOT / "INICIO_OFFLINE.html").write_text(page, encoding="utf-8")


def write_report(stats):
    report = {
        "sitio": SITE_NAME,
        "idiomas": sorted(ALLOWED_LANGUAGES),
        "estadisticas": stats,
        "paginas_guardadas": len(saved_pages),
        "recursos_guardados": len(saved_resources),
        "archivos_generados": total_saved_files,
        "almacenamiento_MB": round(total_saved_bytes / 1024 / 1024, 2),
        "limite_MB": MAX_TOTAL_MB,
        "max_load_more_clicks_por_pagina": MAX_LOAD_MORE_CLICKS,
        "errores": errors,
    }
    METADATA.mkdir(parents=True, exist_ok=True)
    (METADATA / "informe-storyteller.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    (METADATA / "errores-storyteller.txt").write_text(
        "\n".join(errors),
        encoding="utf-8"
    )
    print("\n========== INFORME STORYTELLER ==========")
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    METADATA.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("DESCARGADOR OFFLINE — SOLO STORYTELLER")
    print("Idiomas:", " / ".join(sorted(ALLOWED_LANGUAGES)))
    print("URLs iniciales:", len(START_URLS))
    print("Máximo de páginas:", MAX_PAGES)
    print("Trabajadores:", PAGE_WORKERS)
    print("Máximo Load More por página:", MAX_LOAD_MORE_CLICKS)
    print("Límite de almacenamiento:", MAX_TOTAL_MB, "MB")
    print("=" * 65)

    stats = await crawl()

    print("\n===== REESCRIBIENDO ENLACES PARA USO OFFLINE =====")
    rewrite_offline_links()

    print("\n===== CREANDO ÍNDICE OFFLINE =====")
    create_index()

    write_report(stats)
    print("\nPROCESO TERMINADO. Abre salida/INICIO_OFFLINE.html")


if __name__ == "__main__":
    asyncio.run(main())
