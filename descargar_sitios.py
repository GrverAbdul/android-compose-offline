import asyncio
import hashlib
import json
import os
import re
import traceback
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, unquote

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path("salida")
ROOT.mkdir(exist_ok=True)

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

MAX_PAGES_PER_SITE = 2500
MAX_DEPTH = 15
NAV_TIMEOUT_MS = 45000
WAIT_AFTER_LOAD_MS = 1200

url_to_file = {}
saved_assets = set()
visited_pages = set()
errors = []
page_records = []
response_tasks = set()

def clean_url(url):
    url, _fragment = urldefrag(url)
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return None
    if not p.netloc:
        return None
    return url

def local_path_for(url, default_ext=".html"):
    if url in url_to_file:
        return url_to_file[url]

    p = urlparse(url)
    host = p.netloc.lower().replace(":", "_")
    raw_path = unquote(p.path or "/")
    parts = [x for x in raw_path.split("/") if x not in ("", ".", "..")]
    safe_parts = []
    for part in parts:
        part = re.sub(r"[^A-Za-z0-9._@() -]", "_", part)[:120]
        safe_parts.append(part or "_")

    if not safe_parts:
        safe_parts = ["index" + default_ext]
    elif raw_path.endswith("/"):
        safe_parts.append("index" + default_ext)
    elif "." not in safe_parts[-1]:
        safe_parts[-1] += default_ext

    path = Path(host, *safe_parts)
    if p.query:
        digest = hashlib.sha1(p.query.encode("utf-8")).hexdigest()[:10]
        path = path.with_name(path.stem + "__q_" + digest + path.suffix)

    full = ROOT / path
    url_to_file[url] = full
    return full

def is_internal(url, hosts):
    try:
        return urlparse(url).hostname in hosts
    except Exception:
        return False

def relative_target(source_file, target_file):
    return os.path.relpath(target_file, start=source_file.parent).replace(os.sep, "/")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="es-ES",
            timezone_id="America/La_Paz",
            viewport={"width": 1440, "height": 1000},
            device_scale_factor=1,
            ignore_https_errors=True,
            accept_downloads=False,
        )

        for site in SITES:
            name = site["name"]
            hosts = site["hosts"]
            queue = deque([(site["start"], 0)])
            site_seen = set()
            print(f"\n===== INICIANDO {name}: {site['start']} =====", flush=True)

            while queue and len(site_seen) < MAX_PAGES_PER_SITE:
                current, depth = queue.popleft()
                current = clean_url(current)
                if not current or current in site_seen or depth > MAX_DEPTH:
                    continue
                if not is_internal(current, hosts):
                    continue

                site_seen.add(current)
                visited_pages.add(current)
                target_file = local_path_for(current)
                target_file.parent.mkdir(parents=True, exist_ok=True)

                page = await context.new_page()

                async def capture_response(response):
                    try:
                        req = response.request
                        if req.resource_type in ("document", "stylesheet", "script", "image",
                                                 "font", "media", "xhr", "fetch", "manifest"):
                            u = clean_url(response.url)
                            if not u:
                                return
                            # Los documentos HTML se guardan luego como DOM ya renderizado.
                            if req.resource_type == "document":
                                return
                            body = await response.body()
                            if not body:
                                return
                            asset_file = local_path_for(u, default_ext=".bin")
                            asset_file.parent.mkdir(parents=True, exist_ok=True)
                            asset_file.write_bytes(body)
                            saved_assets.add(u)
                    except Exception as e:
                        errors.append(f"[recurso] {response.url}: {e}")

                def schedule_capture(response):
                    task = asyncio.create_task(capture_response(response))
                    response_tasks.add(task)
                    task.add_done_callback(response_tasks.discard)

                page.on("response", schedule_capture)

                try:
                    print(f"[{name}] ({len(site_seen)}/{MAX_PAGES_PER_SITE}) {current}", flush=True)
                    try:
                        await page.goto(current, wait_until="domcontentloaded",
                                        timeout=NAV_TIMEOUT_MS)
                    except PlaywrightTimeoutError:
                        errors.append(f"[timeout navegación] {current}")

                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeoutError:
                        pass

                    await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

                    # Abrir elementos de navegación/índice que estén inicialmente cerrados.
                    for selector in [
                        "details:not([open]) > summary",
                        'button[aria-expanded="false"]',
                        '[role="button"][aria-expanded="false"]',
                    ]:
                        try:
                            elements = page.locator(selector)
                            count = min(await elements.count(), 80)
                            for i in range(count):
                                try:
                                    await elements.nth(i).click(timeout=700, no_wait_after=True)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    await page.wait_for_timeout(350)

                    final_url = clean_url(page.url) or current
                    final_file = local_path_for(final_url)
                    final_file.parent.mkdir(parents=True, exist_ok=True)

                    html = await page.content()
                    final_file.write_text(html, encoding="utf-8", errors="replace")
                    url_to_file[final_url] = final_file
                    url_to_file[current] = final_file

                    links = await page.locator("a[href]").evaluate_all(
                        "(els) => els.map(a => a.href).filter(Boolean)"
                    )
                    for link in links:
                        link = clean_url(link)
                        if link and is_internal(link, hosts) and link not in site_seen:
                            queue.append((link, depth + 1))

                    page_records.append({
                        "site": name,
                        "url": final_url,
                        "depth": depth,
                        "file": str(final_file),
                        "title": await page.title(),
                    })

                except Exception as e:
                    errors.append(f"[página] {current}: {e}")
                    errors.append(traceback.format_exc())
                finally:
                    await page.close()
                    if response_tasks:
                        await asyncio.gather(*list(response_tasks), return_exceptions=True)

            if queue:
                errors.append(f"[límite] {name}: quedaron {len(queue)} URLs pendientes "
                              f"al alcanzar {MAX_PAGES_PER_SITE} páginas.")
            print(f"===== FINALIZADO {name}: {len(site_seen)} páginas visitadas =====",
                  flush=True)

        await context.close()
        await browser.close()

    # Reescribir enlaces y recursos de las páginas HTML a rutas locales.
    for html_file in ROOT.rglob("*.html"):
        try:
            soup = BeautifulSoup(html_file.read_text(encoding="utf-8", errors="replace"),
                                 "html.parser")
            for tag in soup.find_all(True):
                for attr in ("href", "src", "poster", "action", "data-src"):
                    value = tag.get(attr)
                    if not value or value.startswith(("data:", "javascript:", "mailto:", "tel:")):
                        continue
                    absolute = clean_url(urljoin("https://placeholder.invalid/", value))
                    # Resolver correctamente URLs relativas usando la URL de origen
                    # guardada en el registro de páginas.
                    source_url = next((r["url"] for r in page_records
                                       if Path(r["file"]) == html_file), None)
                    if source_url:
                        absolute = clean_url(urljoin(source_url, value))
                    if absolute and absolute in url_to_file:
                        tag[attr] = relative_target(html_file, url_to_file[absolute])

                if tag.has_attr("srcset"):
                    rewritten = []
                    for item in tag["srcset"].split(","):
                        bits = item.strip().split()
                        if not bits:
                            continue
                        source_url = next((r["url"] for r in page_records
                                           if Path(r["file"]) == html_file), None)
                        abs_url = clean_url(urljoin(source_url, bits[0])) if source_url else None
                        if abs_url in url_to_file:
                            bits[0] = relative_target(html_file, url_to_file[abs_url])
                        rewritten.append(" ".join(bits))
                    tag["srcset"] = ", ".join(rewritten)

            html_file.write_text(str(soup), encoding="utf-8")
        except Exception as e:
            errors.append(f"[reescritura HTML] {html_file}: {e}")

    # Reescribir referencias url(...) en CSS para que apunten a recursos locales capturados.
    for css_file in ROOT.rglob("*.css"):
        try:
            text = css_file.read_text(encoding="utf-8", errors="replace")
            # Resolver CSS contra la URL original que produjo el archivo.
            source_url = next((u for u, f in url_to_file.items() if f == css_file), None)
            if source_url:
                def replace_css(match):
                    quote = match.group(1)
                    raw = match.group(2).strip()
                    abs_url = clean_url(urljoin(source_url, raw))
                    if abs_url in url_to_file:
                        return "url(" + quote + relative_target(css_file, url_to_file[abs_url]) + quote + ")"
                    return match.group(0)
                text = re.sub(r'''url\(\s*(["']?)(.*?)\1\s*\)''', replace_css, text)
                css_file.write_text(text, encoding="utf-8")
        except Exception as e:
            errors.append(f"[reescritura CSS] {css_file}: {e}")

    all_files = [f for f in ROOT.rglob("*") if f.is_file()]
    total_bytes = sum(f.stat().st_size for f in all_files)
    report = {
        "sitios": SITES,
        "paginas_visitadas": len(page_records),
        "recursos_capturados": len(saved_assets),
        "archivos_generados": len(all_files),
        "tamano_total_bytes": total_bytes,
        "tamano_total_MB": round(total_bytes / 1024 / 1024, 2),
        "paginas_por_sitio": {
            site["name"]: sum(1 for r in page_records if r["site"] == site["name"])
            for site in SITES
        },
        "errores": len(errors),
        "nota": "Los contenidos que requieren servidor, login, streaming o API online "
                "pueden no funcionar offline. Revisar enlaces externos y recursos fallidos.",
    }
    Path("informe-descarga.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path("errores-descarga.txt").write_text("\n".join(errors), encoding="utf-8")
    print("\n===== INFORME FINAL =====")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Errores registrados: {len(errors)}")

if __name__ == "__main__":
    asyncio.run(main())
