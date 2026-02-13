import requests
from datetime import datetime

USER = "bartkepl"

# Pobranie listy repo
response = requests.get(f"https://api.github.com/users/{USER}/repos")

if response.status_code != 200:
    raise Exception("GitHub API error")

repos = response.json()

# Sortowanie po ostatniej aktualizacji
repos = sorted(
    [r for r in repos if r["name"] != f"{USER}.github.io"],
    key=lambda x: x["updated_at"],
    reverse=True
)

# ===== Generowanie bloków HTML =====
html_blocks = []

for repo in repos:
    name = repo["name"]
    desc = repo.get("description") or "No description"
    repo_url = repo["html_url"]
    has_pages = repo.get("has_pages", False)

    links = f'''
        <a href="{repo_url}" target="_blank">
            <i class="fab fa-github"></i> Repo
        </a>
    '''

    if has_pages:
        links += f'''
        <a href="https://{USER}.github.io/{name}" target="_blank">
            🌐 Live
        </a>
        '''

    block = f'''
    <div class="project">
        <h3>{name}</h3>
        <p>{desc}</p>
        {links}
    </div>
    '''

    html_blocks.append(block)

projects_html = "\n".join(html_blocks)

# ===== Wstawienie bloków HTML do index.html =====
START = "<!-- GENERATED_PROJECTS_START -->"
END = "<!-- GENERATED_PROJECTS_END -->"

with open("index.html", "r", encoding="utf-8") as f:
    content = f.read()

if START not in content or END not in content:
    raise Exception("Markers not found in index.html")

start_idx = content.index(START) + len(START)
end_idx = content.index(END)

new_content = content[:start_idx] + "\n" + projects_html + "\n" + content[end_idx:]

with open("index.html", "w", encoding="utf-8") as f:
    f.write(new_content)

print("Generated index.html")

# ===== Generowanie sitemap.xml =====
sitemap_entries = []

# Strona główna
sitemap_entries.append(f"""
<url>
    <loc>https://{USER}.github.io/</loc>
    <lastmod>{datetime.utcnow().date()}</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
</url>
""")

# Repo z GitHub Pages
for repo in repos:
    if repo.get("has_pages", False):
        name = repo["name"]
        updated = repo["updated_at"][:10]  # yyyy-mm-dd
        sitemap_entries.append(f"""
<url>
    <loc>https://{USER}.github.io/{name}</loc>
    <lastmod>{updated}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
</url>
""")

sitemap_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{''.join(sitemap_entries)}
</urlset>
"""

with open("sitemap.xml", "w", encoding="utf-8") as f:
    f.write(sitemap_content)

print("Generated sitemap.xml")
