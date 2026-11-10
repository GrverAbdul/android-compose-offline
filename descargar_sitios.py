
import asyncio
import hashlib
import json
import os
import re
import traceback
import xml.etree.ElementTree as ET

from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, unquote

import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIGURACION GENERAL
# ============================================================

ROOT = Path("salida")
ROOT.mkdir(parents=True, exist_ok=True)

SITES = [
    {
        "name": "WebCode",
        "start": "https://webcode.net.pe/web/index.html",
        "hosts": {"webcode.net.pe"},
    },
    {
        "name": "Cursos-Pensa",
        "start": "https://cursospensa.ar/",
        "hosts": {"cursospensa.ar", "www.cursospensa.ar"},
    },
    {
        "name": "Javier-Carrasco",
        "start": "https://documentation.javiercarrasco.es/",
        "hosts": {"documentation.javiercarrasco.es"},
    },
]

MAX_PAGES_PER_SITE = 7500
MAX_DEPTH = 25
NAV_TIMEOUT_MS = 45000
WAIT_AFTER_LOAD_MS = 700

MAX_CAPTURE_BYTES = 25 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
DOWNLOAD_CONCURRENCY = 3

VIDEO_EXTENSIONS = {
    ".mp4", ".webm", ".mov", ".m4v", ".ogv", ".mpg", ".mpeg"
}

DOCUMENT_EXTENSIONS = {
    ".pdf", ".sql", ".zip", ".rar", ".7z",
    ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".txt", ".csv",
    ".apk", ".aab", ".json", ".xml"
}

STATIC_EXTENSIONS = {
    ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".wav", ".ogg", ".aac",
    ".mp4", ".webm", ".mov", ".m4v", ".ogv",
    ".pdf", ".sql", ".zip", ".rar", ".7z",
    ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".txt", ".csv",
    ".apk", ".aab"
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
}

url_to_file = {}
source_url_by_file = {}

download_urls = set()
saved_assets = set()
saved_documents = set()
visited_pages = set()

errors = []
failed_downloads = []
page_records = []

html_count_by_site = {}
document_count_by_site = {}
asset_count_by_site = {}


# ============================================================
# UTILIDADES DE URL Y ARCHIVOS
# ============================================================

def clean_url(url):
    if not url:
        return None

    url, _ = urldefrag(url)
    p = urlparse(url)

    if p.scheme not in ("http", "https") or not p.netloc:
        return None

    return url


def extension_of(url):
    return Path(unquote(urlparse(url).path)).suffix.lower()


def is_internal(url, hosts):
    try:
        return urlparse(url).hostname in hosts
    except Exception:
        return False


def is_video(url):
    return extension_of(url) in VIDEO_EXTENSIONS


def is_document(url):
    return extension_of(url) in DOCUMENT_EXTENSIONS


def is_download(url):
    return is_video(url) or is_document(url)


def is_page_candidate(url):
    ext = extension_of(url)

    if ext in STATIC_EXTENSIONS:
        return False

    if ext and ext not in {
        ".html", ".htm", ".php", ".asp", ".aspx"
    }:
        return False

    return True


def local_path_for(url, default_ext=".bin"):
    url = clean_url(url)

    if not url:
        return None

    if url in url_to_file:
        return url_to_file[url]

    p = urlparse(url)

    host = (p.netloc or "unknown").lower().replace(":", "_")
    raw_path = unquote(p.path or "/")

    parts = [
        part for part in raw_path.split("/")
        if part not in ("", ".", "..")
    ]

    safe_parts = []

    for part in parts:
        part = re.sub(r"[^A-Za-z0-9._@() -]", "_", part)
        part = part[:120]
        safe_parts.append(part or "_")

    if not safe_parts:
        safe_parts = ["index" + default_ext]

    elif raw_path.endswith("/"):
        safe_parts.append("index" + default_ext)

    elif "." not in safe_parts[-1]:
        safe_parts[-1] += default_ext

    path = Path(host, *safe_parts)

    if p.query:
        digest = hashlib.sha1(
            p.query.encode("utf-8")
        ).hexdigest()[:10]

        path = path.with_name(
            path.stem + "__q_" + digest + path.suffix
        )

    full_path = ROOT / path

    url_to_file[url] = full_path
    return full_path


def relative_target(source_file, target_file):
    return os.path.relpath(
        target_file,
        start=source_file.parent
    ).replace(os.sep, "/")


def safe_write(path, body):
    if not path or not body:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return True


def is_valid_mp4(path):
    try:
        if not path.exists() or path.stat().st_size < 12:
            return False

        with path.open("rb") as f:
            header = f.read(64)

        return b"ftyp" in header[:32]

    except Exception:
        return False


# ============================================================
# DESCUBRIMIENTO DE SITEMAPS
# ============================================================

async def discover_sitemaps(session, site):
    hosts = site["hosts"]
    base = site["start"]

    sitemap_queue = deque([
        urljoin(base, "robots.txt"),
        urljoin(base, "sitemap.xml"),
    ])

    visited_sitemaps = set()
    discovered = set()

    while sitemap_queue and len(visited_sitemaps) < 15:
        sitemap_url = sitemap_queue.popleft()

        if sitemap_url in visited_sitemaps:
            continue

        visited_sitemaps.add(sitemap_url)

        try:
            async with session.get(
                sitemap_url,
                timeout=aiohttp.ClientTimeout(total=35)
            ) as response:

                if response.status != 200:
                    continue

                text = await response.text(errors="replace")

            if sitemap_url.endswith("robots.txt"):
                for line in text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        candidate = line.split(":", 1)[1].strip()

                        if candidate:
                            sitemap_queue.append(candidate)

                continue

            try:
                root = ET.fromstring(text)
            except Exception:
                continue

            for element in root.iter():
                if not element.tag.lower().endswith("loc"):
                    continue

                value = (element.text or "").strip()
                value = clean_url(value)

                if not value or not is_internal(value, hosts):
                    continue

                if value.endswith(".xml"):
                    sitemap_queue.append(value)

                elif is_download(value):
                    download_urls.add(value)

                elif is_page_candidate(value):
                    discovered.add(value)

        except Exception as e:
            errors.append(
                f"[sitemap] {sitemap_url}: {e}"
            )

    return discovered


# ============================================================
# CAPTURA DE RESPUESTAS DEL NAVEGADOR
# ============================================================

async def capture_response(response, site_name):
    try:
        request = response.request
        resource_type = request.resource_type

        status = response.status

        if status not in (200, 206):
            return

        url = clean_url(response.url)

        if not url:
            return

        if is_video(url):
            download_urls.add(url)
            return

        if is_document(url):
            download_urls.add(url)
            return

        if resource_type == "document":
            return

        allowed_types = {
            "stylesheet",
            "script",
            "image",
            "font",
            "manifest",
            "xhr",
            "fetch",
        }

        if resource_type not in allowed_types:
            return

        headers = await response.all_headers()

        content_type = headers.get(
            "content-type", ""
        ).lower()

        if "text/html" in content_type:
            return

        content_length = headers.get("content-length")

        if content_length:
            try:
                if int(content_length) > MAX_CAPTURE_BYTES:
                    return
            except Exception:
                pass

        body = await response.body()

        if not body:
            return

        if len(body) > MAX_CAPTURE_BYTES:
            return

        default_ext = ".bin"

        if resource_type == "stylesheet":
            default_ext = ".css"
        elif resource_type == "script":
            default_ext = ".js"

        path = local_path_for(
            url,
            default_ext=default_ext
        )

        if safe_write(path, body):
            saved_assets.add(url)
            asset_count_by_site[site_name] = (
                asset_count_by_site.get(site_name, 0) + 1
            )

    except Exception as e:
        errors.append(
            f"[recurso navegador] {response.url}: {e}"
        )


# ============================================================
# DESCARGA DIRECTA DE VIDEOS Y DOCUMENTOS
# ============================================================

async def download_file(session, url, semaphore):
    async with semaphore:
        url = clean_url(url)

        if not url:
            return

        path = local_path_for(url)

        if not path:
            return

        # Conservar archivos anteriores si son válidos.
        if path.exists() and path.stat().st_size > 0:
            if not is_video(url) or is_valid_mp4(path):
                return

        temp_path = path.with_suffix(
            path.suffix + ".part"
        )

        headers = dict(BROWSER_HEADERS)
        headers["Referer"] = (
            urlparse(url).scheme + "://" +
            urlparse(url).netloc + "/"
        )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            total = 0
            first_bytes = b""

            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=30,
                    sock_read=120
                ),
                allow_redirects=True
            ) as response:

                if response.status not in (200, 206):
                    raise Exception(
                        f"HTTP {response.status}"
                    )

                content_type = response.headers.get(
                    "Content-Type", ""
                ).lower()

                if is_video(url) and "text/html" in content_type:
                    raise Exception(
                        "El servidor devolvió HTML en lugar de vídeo"
                    )

                with temp_path.open("wb") as output:
                    async for chunk in response.content.iter_chunked(
                        1024 * 256
                    ):
                        if not chunk:
                            continue

                        total += len(chunk)

                        if total > MAX_DOWNLOAD_BYTES:
                            raise Exception(
                                "Archivo supera el límite de 1 GB"
                            )

                        if len(first_bytes) < 64:
                            needed = 64 - len(first_bytes)
                            first_bytes += chunk[:needed]

                        output.write(chunk)

            if total == 0:
                raise Exception("Archivo vacío")

            if is_video(url) and b"ftyp" not in first_bytes[:32]:
                raise Exception(
                    "El contenido descargado no tiene firma MP4 válida"
                )

            temp_path.replace(path)

            saved_documents.add(url)

            site_host = urlparse(url).hostname

            document_count_by_site[site_host] = (
                document_count_by_site.get(site_host, 0) + 1
            )

            print(
                f"[DESCARGADO] {url} ({total / 1024:.1f} KB)",
                flush=True
            )

        except Exception as e:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

            failed_downloads.append(
                f"{url} -> {e}"
            )


# ============================================================
# CRAWLER PRINCIPAL
# ============================================================

async def crawl_site(browser, session, site):
    name = site["name"]
    hosts = site["hosts"]

    queue = deque([(site["start"], 0)])
    site_seen = set()

    sitemap_urls = await discover_sitemaps(
        session,
        site
    )

    for url in sitemap_urls:
        queue.append((url, 0))

    print(
        f"\n===== INICIANDO {name} =====",
        flush=True
    )

    while queue and len(site_seen) < MAX_PAGES_PER_SITE:
        current, depth = queue.popleft()

        current = clean_url(current)

        if not current:
            continue

        if current in site_seen or depth > MAX_DEPTH:
            continue

        if not is_internal(current, hosts):
            continue

        if is_download(current):
            download_urls.add(current)
            continue

        if not is_page_candidate(current):
            continue

        site_seen.add(current)
        visited_pages.add(current)

        page = await browser.new_page()

        response_tasks = set()

        def schedule_capture(response):
            task = asyncio.create_task(
                capture_response(response, name)
            )

            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", schedule_capture)

        try:
            print(
                f"[{name}] ({len(site_seen)}/{MAX_PAGES_PER_SITE}) {current}",
                flush=True
            )

            response = None

            try:
                response = await page.goto(
                    current,
                    wait_until="domcontentloaded",
                    timeout=NAV_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                errors.append(
                    f"[timeout navegación] {current}"
                )
            except Exception as e:
                errors.append(
                    f"[navegación] {current}: {e}"
                )

            if response is None:
                continue

            status = response.status

            if status not in (200, 206):
                errors.append(
                    f"[HTTP {status}] {current}"
                )
                continue

            headers = await response.all_headers()

            content_type = headers.get(
                "content-type", ""
            ).lower()

            final_url = clean_url(page.url) or current

            if not is_internal(final_url, hosts):
                continue

            # Nunca guardar el reproductor HTML como un vídeo.
            if is_download(final_url) or is_download(current):
                download_urls.add(current)
                download_urls.add(final_url)
                continue

            if "text/html" not in content_type:
                try:
                    body = await response.body()
                    path = local_path_for(
                        final_url,
                        default_ext=".bin"
                    )

                    if body:
                        safe_write(path, body)
                        saved_documents.add(final_url)

                except Exception as e:
                    errors.append(
                        f"[documento navegación] {current}: {e}"
                    )

                continue

            # Cargar imágenes diferidas y contenido inferior.
            try:
                await page.evaluate("""
                    () => {
                        document.querySelectorAll('img[loading="lazy"]')
                            .forEach(img => img.loading = 'eager');

                        document.querySelectorAll('iframe[loading="lazy"]')
                            .forEach(frame => frame.loading = 'eager');

                        window.scrollTo(0, document.body.scrollHeight);
                        window.scrollTo(0, 0);
                    }
                """)
            except Exception:
                pass

            await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

            # Abrir elementos details, sin pulsar indiscriminadamente
            # todos los botones de la página.
            try:
                details = page.locator(
                    "details:not([open]) > summary"
                )

                count = min(await details.count(), 30)

                for i in range(count):
                    try:
                        await details.nth(i).click(
                            timeout=500,
                            no_wait_after=True
                        )
                    except Exception:
                        pass

            except Exception:
                pass

            await page.wait_for_timeout(250)

            html = await page.content()

            if not html:
                continue

            final_file = local_path_for(final_url)

            if not final_file:
                continue

            final_file.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            final_file.write_text(
                html,
                encoding="utf-8",
                errors="replace"
            )

            url_to_file[current] = final_file
            url_to_file[final_url] = final_file

            source_url_by_file[str(final_file)] = final_url

            html_count_by_site[name] = (
                html_count_by_site.get(name, 0) + 1
            )

            page_records.append({
                "site": name,
                "url": final_url,
                "depth": depth,
                "file": str(final_file),
                "title": await page.title(),
            })

            # Descubrir enlaces y recursos presentes en el DOM.
            links = await page.evaluate("""
                () => {
                    const result = [];

                    const selectors = [
                        'a[href]',
                        'area[href]',
                        'iframe[src]',
                        'frame[src]',
                        'img[src]',
                        'img[data-src]',
                        'img[data-lazy-src]',
                        'source[src]',
                        'video[src]',
                        'video[poster]',
                        'audio[src]',
                        'script[src]',
                        'link[href]',
                        'object[data]',
                        'embed[src]'
                    ];

                    for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            for (const attr of [
                                'href', 'src', 'data-src',
                                'data-lazy-src', 'poster',
                                'data', 'data-original'
                            ]) {
                                const value = el.getAttribute(attr);

                                if (value) {
                                    try {
                                        result.push(
                                            new URL(value, document.baseURI).href
                                        );
                                    } catch (_) {}
                                }
                            }
                        }
                    }

                    for (const el of document.querySelectorAll('[srcset]')) {
                        const value = el.getAttribute('srcset') || '';

                        for (const item of value.split(',')) {
                            const url = item.trim().split(/\\s+/)[0];

                            if (url) {
                                try {
                                    result.push(
                                        new URL(url, document.baseURI).href
                                    );
                                } catch (_) {}
                            }
                        }
                    }

                    return [...new Set(result)];
                }
            """)

            for link in links:
                link = clean_url(link)

                if not link or not is_internal(link, hosts):
                    continue

                if is_download(link):
                    download_urls.add(link)

                elif is_page_candidate(link):
                    if link not in site_seen:
                        queue.append((link, depth + 1))

        except Exception as e:
            errors.append(
                f"[página] {current}: {e}"
            )
            errors.append(traceback.format_exc())

        finally:
            if response_tasks:
                await asyncio.gather(
                    *list(response_tasks),
                    return_exceptions=True
                )

            await page.close()

    if queue:
        errors.append(
            f"[límite] {name}: quedaron {len(queue)} URLs pendientes."
        )

    print(
        f"===== FINALIZADO {name}: "
        f"{len(site_seen)} URLs de página procesadas =====",
        flush=True
    )


# ============================================================
# REESCRITURA DE HTML PARA NAVEGACION OFFLINE
# ============================================================

def rewrite_html():
    for html_file in ROOT.rglob("*.html"):
        source_url = source_url_by_file.get(
            str(html_file)
        )

        if not source_url:
            continue

        try:
            soup = BeautifulSoup(
                html_file.read_text(
                    encoding="utf-8",
                    errors="replace"
                ),
                "html.parser"
            )

            for tag in soup.find_all(True):
                for attr in (
                    "href",
                    "src",
                    "poster",
                    "action",
                    "data-src",
                    "data-lazy-src",
                    "data-original"
                ):
                    value = tag.get(attr)

                    if not value:
                        continue

                    if value.startswith((
                        "data:",
                        "javascript:",
                        "mailto:",
                        "tel:",
                        "blob:"
                    )):
                        continue

                    if value.startswith("#"):
                        continue

                    absolute = clean_url(
                        urljoin(source_url, value)
                    )

                    if not absolute:
                        continue

                    target = url_to_file.get(absolute)

                    if target:
                        fragment = urlparse(
                            urljoin(source_url, value)
                        ).fragment

                        local = relative_target(
                            html_file,
                            target
                        )

                        if fragment:
                            local += "#" + fragment

                        tag[attr] = local

                if tag.has_attr("srcset"):
                    rewritten = []

                    for item in tag["srcset"].split(","):
                        bits = item.strip().split()

                        if not bits:
                            continue

                        absolute = clean_url(
                            urljoin(source_url, bits[0])
                        )

                        target = url_to_file.get(absolute)

                        if target:
                            bits[0] = relative_target(
                                html_file,
                                target
                            )

                        rewritten.append(" ".join(bits))

                    tag["srcset"] = ", ".join(rewritten)

            html_file.write_text(
                str(soup),
                encoding="utf-8"
            )

        except Exception as e:
            errors.append(
                f"[reescritura HTML] {html_file}: {e}"
            )


# ============================================================
# REESCRITURA DE CSS
# ============================================================

def rewrite_css():
    url_pattern = re.compile(
        r'''url\(\s*(["']?)(.*?)\1\s*\)''',
        re.IGNORECASE
    )

    import_pattern = re.compile(
        r'''@import\s+(["'])(.*?)\1''',
        re.IGNORECASE
    )

    for css_file in ROOT.rglob("*.css"):
        source_url = source_url_by_file.get(
            str(css_file)
        )

        if not source_url:
            source_url = next(
                (
                    url for url, path in url_to_file.items()
                    if path == css_file
                ),
                None
            )

        if not source_url:
            continue

        try:
            content = css_file.read_text(
                encoding="utf-8",
                errors="replace"
            )

            def replace_url(match):
                quote = match.group(1)
                raw = match.group(2).strip()

                if raw.startswith((
                    "data:",
                    "http://",
                    "https://",
                    "#"
                )):
                    absolute = clean_url(
                        urljoin(source_url, raw)
                    )
                else:
                    absolute = clean_url(
                        urljoin(source_url, raw)
                    )

                target = url_to_file.get(absolute)

                if target:
                    local = relative_target(
                        css_file,
                        target
                    )
                    return f"url({quote}{local}{quote})"

                return match.group(0)

            content = url_pattern.sub(
                replace_url,
                content
            )

            def replace_import(match):
                quote = match.group(1)
                raw = match.group(2)

                absolute = clean_url(
                    urljoin(source_url, raw)
                )

                target = url_to_file.get(absolute)

                if target:
                    local = relative_target(
                        css_file,
                        target
                    )
                    return f"@import {quote}{local}{quote}"

                return match.group(0)

            content = import_pattern.sub(
                replace_import,
                content
            )

            css_file.write_text(
                content,
                encoding="utf-8"
            )

        except Exception as e:
            errors.append(
                f"[reescritura CSS] {css_file}: {e}"
            )


# ============================================================
# PAGINA DE INICIO OFFLINE
# ============================================================

def create_offline_index():
    links = []

    for site in SITES:
        target = local_path_for(site["start"])

        if target and target.exists():
            local = os.path.relpath(
                target,
                ROOT
            ).replace(os.sep, "/")

            links.append(
                f'<li><a href="{local}">{site["name"]}</a></li>'
            )

    html = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Documentación offline</title>
<style>
body {
    font-family: Arial, sans-serif;
    max-width: 850px;
    margin: 50px auto;
    padding: 20px;
    line-height: 1.6;
    background: #f4f6f8;
    color: #222;
}
main {
    background: white;
    padding: 30px;
    border-radius: 12px;
}
a {
    color: #145db8;
    font-weight: bold;
}
li {
    margin: 15px 0;
}
</style>
</head>
<body>
<main>
<h1>Documentación de programación offline</h1>
<p>Selecciona el sitio que deseas consultar:</p>
<ul>
""" + "\n".join(links) + """
</ul>
</main>
</body>
</html>
"""

    (ROOT / "index-offline.html").write_text(
        html,
        encoding="utf-8"
    )


# ============================================================
# INFORME FINAL
# ============================================================

def create_report():
    all_files = [
        f for f in ROOT.rglob("*")
        if f.is_file()
        and not f.name.endswith(".part")
    ]

    total_bytes = sum(
        f.stat().st_size for f in all_files
    )

    report = {
        "sitios": [
            {
                "name": site["name"],
                "start": site["start"],
                "hosts": sorted(site["hosts"])
            }
            for site in SITES
        ],
        "paginas_html": len(page_records),
        "paginas_por_sitio": html_count_by_site,
        "documentos_descargados": len(saved_documents),
        "recursos_capturados": len(saved_assets),
        "archivos_generados": len(all_files),
        "tamano_total_bytes": total_bytes,
        "tamano_total_MB": round(
            total_bytes / 1024 / 1024, 2
        ),
        "urls_video_documento_detectadas": len(download_urls),
        "descargas_fallidas": len(failed_downloads),
        "errores": len(errors),
        "nota": (
            "Los contenidos que dependen de servidores, "
            "autenticacion, streaming o API externas pueden "
            "no funcionar completamente sin conexion."
        )
    }

    Path("informe-descarga.json").write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    Path("errores-descarga.txt").write_text(
        "\n".join(errors),
        encoding="utf-8"
    )

    Path("descargas-fallidas.txt").write_text(
        "\n".join(failed_downloads),
        encoding="utf-8"
    )

    print("\n========== INFORME FINAL ==========")
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2
        )
    )

    print(
        f"\nDescargas fallidas: {len(failed_downloads)}"
    )

    print(f"Errores: {len(errors)}")


# ============================================================
# EJECUCION
# ============================================================

async def main():
    # Eliminar MP4 antiguos que en realidad sean HTML.
    for old_video in ROOT.rglob("*.mp4"):
        if not is_valid_mp4(old_video):
            try:
                old_video.unlink()
                print(
                    f"[ELIMINADO MP4 INVALIDO] {old_video}",
                    flush=True
                )
            except Exception as e:
                errors.append(
                    f"[eliminar MP4 inválido] {old_video}: {e}"
                )

    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=30,
        sock_read=120
    )

    connector = aiohttp.TCPConnector(
        limit=DOWNLOAD_CONCURRENCY,
        ssl=False
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        headers=BROWSER_HEADERS
    ) as session:

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True
            )

            context = await browser.new_context(
                locale="es-ES",
                timezone_id="America/La_Paz",
                viewport={
                    "width": 1440,
                    "height": 1000
                },
                device_scale_factor=1,
                ignore_https_errors=True,
                accept_downloads=True
            )

            for site in SITES:
                await crawl_site(
                    context,
                    session,
                    site
                )

            await context.close()
            await browser.close()

        # Descargar vídeos, SQL, PDF y otros documentos.
        print(
            "\n===== DESCARGANDO DOCUMENTOS Y MULTIMEDIA =====",
            flush=True
        )

        semaphore = asyncio.Semaphore(
            DOWNLOAD_CONCURRENCY
        )

        tasks = [
            download_file(
                session,
                url,
                semaphore
            )
            for url in sorted(download_urls)
        ]

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True
            )

    # Reescribir las rutas de las páginas y hojas CSS.
    print("\n===== REESCRIBIENDO HTML =====")
    rewrite_html()

    print("\n===== REESCRIBIENDO CSS =====")
    rewrite_css()

    create_offline_index()
    create_report()

    print("\n===== PROCESO TERMINADO =====")


if __name__ == "__main__":
    asyncio.run(main())
