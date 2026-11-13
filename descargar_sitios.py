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
