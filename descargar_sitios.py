
import os
import re
import json
import hashlib
import time
import traceback
from pathlib import Path
from collections import deque
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SITES = [
    #{"name": "Codica Basicos", "start": "https://basicos.codica.la/", "hosts": {"basicos.codica.la"}, "max_total_mb": 2048},
    #{"name": "W3Docs", "start": "https://es.w3docs.com/", "hosts": {"es.w3docs.com"}, "max_total_mb": 2048},
    {"name": "CESLAVA Cursos", "start": "https://cursos.ceslava.com/", "hosts": {"cursos.ceslava.com"}, "resource_hosts": {"fonts.googleapis.com", "fonts.gstatic.com"}, "max_total_mb": 4096},
    #{"name": "eLearner", "start": "https://elearner.app/es/", "hosts": {"elearner.app"}, "max_total_mb": 2048},
    #{"name": "DevSchool", "start": "https://devschool.es/", "hosts": {"devschool.es"}, "max_total_mb": 2048},
    #{"name": "CodeRacha", "start": "https://carloswilches.com/", "hosts": {"carloswilches.com", "www.carloswilches.com"}, "max_total_mb": 2048},
    #{"name": "GameCode", "start": "https://gamecode.me/", "hosts": {"gamecode.me"}, "resource_hosts": {"assets.gamecode.me", "cdn.jsdelivr.net", "raw.githubusercontent.com"}, "max_total_mb": 2048},
    #{"name": "Aprende Web desde Cero", "start": "https://cacg-code.github.io/web-desde-cero/", "hosts": {"cacg-code.github.io"}, "resource_hosts": {"cdn.jsdelivr.net"}, "max_total_mb": 2048},
    #{"name": "Tutoriales Programacion Ya", "start": "https://www.tutorialesprogramacionya.com/", "hosts": {"www.tutorialesprogramacionya.com", "tutorialesprogramacionya.com"}, "max_total_mb": 2048},
    #{"name": "Aprende JavaScript", "start": "https://www.aprendejavascript.dev/", "hosts": {"www.aprendejavascript.dev", "aprendejavascript.dev"}, "max_total_mb": 2048},
]
ROOT = Path("salida")
METADATA = ROOT / "_metadata"
URL_MAP_FILE = METADATA / "url-map.json"
PAGE_MAP_FILE = METADATA / "page-map.json"
MAX_PAGES_PER_SITE = 8500
MAX_DEPTH = 30
NAVIGATION_TIMEOUT = 45000
WAIT_AFTER_LOAD_MS = 1200
MAX_RESOURCE_SIZE = 1000 * 1024 * 1024
CESLAVA_PAGE_RETRIES = 2
CESLAVA_RETRY_DELAYS = (3, 6)
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
    limit_mb = site.get("max_total_mb", 2048)
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
