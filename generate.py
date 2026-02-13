import requests

USER = "bartkepl"

repos = requests.get(f"https://api.github.com/users/{USER}/repos").json()

repos = sorted(
    [r for r in repos if r["name"] != f"{USER}.github.io"],
    key=lambda x: x["updated_at"],
    reverse=True
)

html_blocks = []

for repo in repos:
    name = repo["name"]
    desc = repo["description"] or "No description"
    repo_url = repo["html_url"]
    has_pages = repo["has_pages"]

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

with open("index.html", "r", encoding="utf-8") as f:
    content = f.read()

START = "<!-- GENERATED_PROJECTS_START -->"
END = "<!-- GENERATED_PROJECTS_END -->"

with open("index.html", "r", encoding="utf-8") as f:
    content = f.read()

start_idx = content.index(START) + len(START)
end_idx = content.index(END)

new_content = (
    content[:start_idx] +
    "\n" + projects_html + "\n" +
    content[end_idx:]
)

with open("index.html", "w", encoding="utf-8") as f:
    f.write(new_content)

with open("index.html", "w", encoding="utf-8") as f:
    f.write(content)

print("Generated index.html")
