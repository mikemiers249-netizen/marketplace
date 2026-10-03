"""Fetch exact existing frontend releases; record content hashes and licences."""
from pathlib import Path
import hashlib
import json
import urllib.request

root = Path(__file__).resolve().parents[1] / 'app' / 'static' / 'vendor'
files = {
    'bootstrap/bootstrap.min.css': 'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css',
    'bootstrap/bootstrap.bundle.min.js': 'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js',
    'bootstrap/LICENSE': 'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/LICENSE',
    'bootstrap-icons/bootstrap-icons.min.css': 'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.2/font/bootstrap-icons.min.css',
    'bootstrap-icons/fonts/bootstrap-icons.woff2': 'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.2/font/fonts/bootstrap-icons.woff2',
    'bootstrap-icons/fonts/bootstrap-icons.woff': 'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.2/font/fonts/bootstrap-icons.woff',
    'bootstrap-icons/LICENSE': 'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.2/LICENSE',
    'leaflet/leaflet.css': 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
    'leaflet/leaflet.js': 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
    'leaflet/LICENSE': 'https://unpkg.com/leaflet@1.9.4/LICENSE',
    'fullcalendar/index.global.min.js': 'https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.js',
    'fullcalendar/LICENSE': 'https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/LICENSE.md',
}
for name in ['layers.png', 'layers-2x.png', 'marker-icon.png', 'marker-icon-2x.png', 'marker-shadow.png']:
    files['leaflet/images/' + name] = 'https://unpkg.com/leaflet@1.9.4/dist/images/' + name
manifest = {}
for name, url in files.items():
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    content = urllib.request.urlopen(url, timeout=30).read()
    target.write_bytes(content)
    manifest[name] = {'source': url, 'sha256': hashlib.sha256(content).hexdigest()}
    print(name)
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
