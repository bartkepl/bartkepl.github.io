#!/usr/bin/env python3
"""Build the portfolio site from the GitHub API.

Lists every public repository of the user from config.toml, enriches each one
with its README (description fallback, preview image), file tree (stack tags,
category) and languages, then renders src/ into the output directory.

    python generate.py              # build into _site/
    python generate.py --out dist

A token (GITHUB_TOKEN or GH_TOKEN, else `gh auth token`) is optional but
recommended: without one GitHub allows only 60 API requests per hour.
Pillow is optional too; without it previews are linked straight from GitHub.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import Image, ImageChops
except ImportError:  # previews fall back to remote images
    Image = None

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
API = "https://api.github.com"
STATIC_FILES = ("style.css", "app.js", "favicon.svg", "robots.txt")

THUMB_SIZE = (960, 640)
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# Tags detected from the file tree: (label, path regex).
STACK_RULES = [
    ("KiCad", re.compile(r"\.kicad_pro$", re.I)),
    ("STM32", re.compile(r"\.ioc$", re.I)),
    ("Flutter", re.compile(r"(^|/)pubspec\.yaml$")),
    ("PlatformIO", re.compile(r"(^|/)platformio\.ini$")),
    ("Arduino", re.compile(r"\.ino$")),
    ("ESP-IDF", re.compile(r"(^|/)sdkconfig(\.defaults)?$")),
    ("FPGA", re.compile(r"\.(vhdl?|sv|xdc|qsf)$", re.I)),
]
# Languages that only come from generated or boilerplate files next to a stack.
REDUNDANT_LANGUAGES = {
    "Flutter": {"Dart", "C++", "CMake", "Swift", "Kotlin", "Objective-C", "Ruby", "HTML"},
    "KiCad": {"HTML", "CSS", "JavaScript"},  # interactive BOM exports
}
MIN_LANGUAGE_SHARE = 0.10
MAX_TAGS = 4

BADGE_URL = re.compile(
    r"shields\.io|badgen\.net|badge|/actions/workflows/|codecov|coveralls|\.svg(\?|#|$)", re.I
)
MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
README_BOILERPLATE = ("a new flutter project", "this project is a starting point")


# ---------------------------------------------------------------- GitHub API

class RateLimited(Exception):
    pass


class GitHub:
    def __init__(self, token: str | None):
        self.token = token
        self.limited = False

    def get(self, url: str, accept: str = "application/vnd.github+json") -> tuple[bytes, dict]:
        headers = {"User-Agent": "bartkepl.github.io-generator", "Accept": accept}
        if url.startswith(API):
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read(MAX_IMAGE_BYTES + 1), dict(response.headers)
        except urllib.error.HTTPError as err:
            if err.code in (403, 429) and err.headers.get("X-RateLimit-Remaining") == "0":
                self.limited = True
                raise RateLimited(url) from err
            raise

    def json(self, path: str) -> tuple[object, dict]:
        body, headers = self.get(API + path)
        return json.loads(body), headers

    def optional(self, path: str) -> object | None:
        """Enrichment request: any failure just means less detail on the card."""
        if self.limited:
            return None
        try:
            return self.json(path)[0]
        except RateLimited:
            warn("API rate limit reached, remaining projects get basic cards only")
        except urllib.error.HTTPError as err:
            if err.code not in (404, 409):  # 409: empty repository
                warn(f"{path}: HTTP {err.code}")
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            warn(f"{path}: {err}")
        return None

    def list_repos(self, user: str) -> list[dict]:
        repos, url = [], f"{API}/users/{user}/repos?per_page=100&type=owner&sort=pushed"
        while url:
            body, headers = self.get(url)
            repos.extend(json.loads(body))
            match = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", ""))
            url = match.group(1) if match else None
        return repos


def find_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token or os.environ.get("GITHUB_ACTIONS"):
        return token
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def warn(message: str) -> None:
    print(f"  warning: {message}", file=sys.stderr)


# ------------------------------------------------------------------- README

def markdown_to_text(md: str) -> str:
    text = MD_IMAGE.sub("", md)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def shorten(text: str, limit: int = 240) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence_end > limit * 0.4:
        return cut[: sentence_end + 1]
    return cut[: cut.rfind(" ")].rstrip(",;:") + "…"


def readme_summary(readme: str) -> str:
    """First prose paragraph of a README, as plain text."""
    readme = re.sub(r"<!--.*?-->", "", readme, flags=re.S)
    readme = re.sub(r"^(```|~~~).*?^\1", "", readme, flags=re.S | re.M)
    for block in re.split(r"\n\s*\n", readme):
        block = block.strip()
        if not block or block[0] in "#!<>|-*+=`" or re.match(r"\d+[.)]\s", block):
            continue
        if re.search(r"^\s*[=-]{3,}\s*$", block, re.M):  # setext heading
            continue
        text = markdown_to_text(block)
        if len(text) < 40 or text.lower().startswith(README_BOILERPLATE):
            continue
        return shorten(text)
    return ""


def resolve_repo_path(src: str, repo: dict, base_dir: str) -> str | None:
    src = html.unescape(src.strip())
    if src.startswith("//"):
        src = "https:" + src
    if re.match(r"https?://", src):
        blob = re.match(r"https://github\.com/([^/]+)/([^/]+)/(?:blob|raw)/(.+)", src)
        if blob:
            return f"https://raw.githubusercontent.com/{blob[1]}/{blob[2]}/{blob[3]}"
        return src
    if src.startswith(("data:", "#", "mailto:")):
        return None
    path = urllib.parse.unquote(re.split(r"[?#]", src)[0])
    path = path.lstrip("/") if path.startswith("/") else posixpath.normpath(posixpath.join(base_dir, path))
    if path.startswith(".."):
        return None
    return (
        f"https://raw.githubusercontent.com/{repo['full_name']}/"
        f"{repo['default_branch']}/{urllib.parse.quote(path)}"
    )


def readme_image(readme: str, repo: dict, base_dir: str) -> str | None:
    """Pick the README image that best previews the project (no badges)."""
    found = [(m.start(), m.group(2)) for m in MD_IMAGE.finditer(readme)]
    found += [(m.start(), m.group(1)) for m in HTML_IMAGE.finditer(readme)]
    candidates = [src for _, src in sorted(found) if not BADGE_URL.search(src)]
    if not candidates:
        return None
    # KiCad jobsets name the 3/4 view "*_angle"; it makes the best thumbnail.
    preferred = next((c for c in candidates if "angle" in posixpath.basename(c).lower()), candidates[0])
    return resolve_repo_path(preferred, repo, base_dir)


# ------------------------------------------------------------------- images

def is_kicad_background(pixel: tuple[int, int, int]) -> bool:
    r, g, b = pixel
    return abs(r - g) <= 4 and 14 <= b - r <= 40


def looks_like_kicad_render(im: "Image.Image") -> bool:
    """KiCad's 3D viewer paints a lavender vertical gradient behind the board."""
    w, h = im.size
    corners = [im.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    return all(is_kicad_background(p) for p in corners)


def remove_kicad_background(im: "Image.Image") -> "Image.Image":
    rgb = im.convert("RGB")
    w, h = rgb.size
    strip = Image.new("RGB", (1, h))
    row_color = rgb.getpixel((0, 0))
    for y in range(h):
        for x in (0, w - 1):
            pixel = rgb.getpixel((x, y))
            if is_kicad_background(pixel):
                row_color = pixel
                break
        strip.putpixel((0, y), row_color)
    background = strip.resize((w, h), Image.Resampling.NEAREST)
    r, g, b = ImageChops.difference(rgb, background).split()
    distance = ImageChops.lighter(ImageChops.lighter(r, g), b)
    alpha = distance.point(lambda v: 0 if v <= 6 else 255 if v >= 26 else (v - 6) * 255 // 20)
    out = im.convert("RGBA")
    out.putalpha(ImageChops.multiply(alpha, out.getchannel("A")))
    box = out.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    if box:
        pad = round(max(box[2] - box[0], box[3] - box[1]) * 0.02)
        out = out.crop((max(box[0] - pad, 0), max(box[1] - pad, 0), min(box[2] + pad, w), min(box[3] + pad, h)))
    return out


def make_preview(data: bytes, dest: Path) -> dict | None:
    try:
        im = Image.open(io.BytesIO(data))
        im.seek(0)
        im = im.convert("RGBA")
    except Exception:
        return None
    im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    fit = "cover"
    if looks_like_kicad_render(im.convert("RGB")):
        im, fit = remove_kicad_background(im), "render"
    elif im.height > im.width * 1.1:
        fit = "portrait"
    im.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
    if fit != "render":
        im = im.convert("RGB")
    im.save(dest, "WEBP", quality=82, method=6)
    return {"width": im.width, "height": im.height, "fit": fit}


# ----------------------------------------------------------------- projects

@dataclass
class Project:
    name: str
    title: str
    url: str
    description: str
    category: str
    tags: list[str]
    pushed_at: str
    created_at: str
    website: str | None = None
    stars: int = 0
    archived: bool = False
    pinned: bool = False
    fork_of: dict | None = None
    image: dict | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or "project"


def keyword_text(repo: dict) -> str:
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", repo["name"]).replace("_", " ").replace("-", " ")
    return f"{name} {repo.get('description') or ''}"


def categorize(repo: dict, paths: list[str], categories: list[dict]) -> str:
    topics = set(repo.get("topics") or [])
    text = keyword_text(repo)
    for category in categories:
        if topics & set(category.get("topics", [])):
            return category["id"]
        if any(re.search(rf"\b{re.escape(k)}\b", text, re.I) for k in category.get("keywords", [])):
            return category["id"]
        rules = [re.compile(p, re.I) for p in category.get("files", [])]
        if rules and any(rule.search(path) for path in paths for rule in rules):
            return category["id"]
    default = next((c for c in categories if c.get("default")), categories[-1])
    return default["id"]


def detect_tags(repo: dict, paths: list[str], languages: dict | None) -> list[str]:
    tags = [label for label, rule in STACK_RULES if any(rule.search(p) for p in paths)]
    if "KiCad" not in tags and re.search(r"kicad", keyword_text(repo), re.I):
        tags.insert(0, "KiCad")
    skip = set().union(*(REDUNDANT_LANGUAGES.get(t, set()) for t in tags))
    if not languages and repo.get("language"):
        languages = {repo["language"]: 1}
    total = sum((languages or {}).values()) or 1
    for language, size in sorted((languages or {}).items(), key=lambda item: -item[1]):
        if size / total >= MIN_LANGUAGE_SHARE and language not in skip and language not in tags:
            tags.append(language)
    return tags[:MAX_TAGS]


def website_for(repo: dict, user: str) -> str | None:
    homepage = (repo.get("homepage") or "").strip()
    if homepage and homepage.rstrip("/") != repo["html_url"]:
        return homepage if re.match(r"https?://", homepage) else "https://" + homepage
    if repo.get("has_pages"):
        return f"https://{user}.github.io/{repo['name']}/"
    return None


def build_project(gh: GitHub, repo: dict, config: dict, assets: Path) -> Project:
    user = config["site"]["user"]
    override = config.get("projects", {}).get(repo["name"], {})
    full = repo["full_name"]
    notes = []

    tree = gh.optional(f"/repos/{full}/git/trees/{urllib.parse.quote(repo['default_branch'])}?recursive=1")
    paths = [item["path"] for item in (tree or {}).get("tree", []) if item.get("type") == "blob"]
    languages = gh.optional(f"/repos/{full}/languages")
    readme_info = gh.optional(f"/repos/{full}/readme")
    readme, readme_dir = "", ""
    if isinstance(readme_info, dict) and readme_info.get("content"):
        readme = base64.b64decode(readme_info["content"]).decode("utf-8", "replace")
        readme_dir = posixpath.dirname(readme_info.get("path", ""))

    description = override.get("description") or (repo.get("description") or "").strip()
    if not description and readme:
        description = readme_summary(readme)
        notes.append("description from README" if description else "no description")

    category = override.get("category") or categorize(repo, paths, config["categories"])

    fork_of = None
    if repo.get("fork"):
        details = gh.optional(f"/repos/{full}")
        parent = (details or {}).get("parent")
        if parent:
            fork_of = {"name": parent["full_name"], "url": parent["html_url"]}

    website = override.get("website", website_for(repo, user)) or None

    image = None
    image_src = override.get("image")
    if image_src is None and readme:
        image_src = readme_image(readme, repo, readme_dir)
    elif isinstance(image_src, str):
        image_src = resolve_repo_path(image_src, repo, "")
    if image_src:
        image = fetch_preview(gh, image_src, assets, repo["name"])
        notes.append("preview " + (image["fit"] if image else "failed"))

    return Project(
        name=repo["name"],
        title=override.get("title", repo["name"]),
        url=repo["html_url"],
        description=description,
        category=category,
        tags=override.get("tags") or detect_tags(repo, paths, languages),
        pushed_at=repo.get("pushed_at") or repo["updated_at"],
        created_at=repo["created_at"],
        website=website,
        stars=repo.get("stargazers_count", 0),
        archived=repo.get("archived", False),
        pinned=override.get("pinned", False),
        fork_of=fork_of,
        image=image,
        notes=notes,
    )


def fetch_preview(gh: GitHub, src: str, assets: Path, name: str) -> dict | None:
    if Image is None:
        return {"src": src, "fit": "cover"}
    try:
        data, _ = gh.get(src, accept="image/*")
    except (urllib.error.URLError, TimeoutError, RateLimited) as err:
        warn(f"{name}: preview {src}: {err}")
        return None
    if len(data) > MAX_IMAGE_BYTES:
        warn(f"{name}: preview larger than {MAX_IMAGE_BYTES // 2**20} MB, skipped")
        return None
    filename = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + ".webp"
    info = make_preview(data, assets / filename)
    if not info:
        warn(f"{name}: preview {src} is not a readable image")
        return None
    return {"src": f"assets/projects/{filename}", **info}


# ---------------------------------------------------------------- rendering

ICONS = {
    "github": '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>',
    "globe": '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.4" d="M8 1.2a6.8 6.8 0 1 0 0 13.6A6.8 6.8 0 0 0 8 1.2Zm-6.8 6.8h13.6M8 1.2c1.9 1.9 2.8 4.2 2.8 6.8S9.9 12.9 8 14.8C6.1 12.9 5.2 10.6 5.2 8S6.1 3.1 8 1.2Z"/></svg>',
    "star": '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 .8l2.2 4.6 5 .7-3.6 3.5.9 5-4.5-2.4-4.5 2.4.9-5L.8 6.1l5-.7Z"/></svg>',
    "link": '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.4" d="M6.5 9.5l3-3M7 4.5l1.2-1.2a2.8 2.8 0 0 1 4 4L11 8.5m-6 -1L3.8 8.7a2.8 2.8 0 0 0 4 4L9 11.5"/></svg>',
}

esc = html.escape


def breakable(name: str) -> str:
    return esc(name).replace("_", "_<wbr>").replace("-", "-<wbr>")


def monogram(name: str) -> str:
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ").replace("-", " ").split()
    letters = "".join(w[0] for w in words[:2]) if len(words) > 1 else name[:2]
    return letters.upper()


def display_date(iso: str) -> str:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f"{d.day} {d:%b %Y}"


def render_card(p: Project) -> str:
    if p.image and p.image.get("width"):
        img = p.image
        media = (
            f'<div class="card-media" data-fit="{img["fit"]}">'
            f'<img src="{esc(img["src"])}" width="{img["width"]}" height="{img["height"]}" '
            f'alt="" loading="lazy" decoding="async"></div>'
        )
    elif p.image:
        media = (
            f'<div class="card-media" data-fit="cover">'
            f'<img src="{esc(p.image["src"])}" alt="" loading="lazy" decoding="async"></div>'
        )
    else:
        media = f'<div class="card-media" data-fit="none"><span class="package">{esc(monogram(p.name))}</span></div>'

    badges = '<span class="badge">Archived</span>' if p.archived else ""
    fork = (
        f'<p class="card-fork">Fork of <a href="{esc(p.fork_of["url"])}">{esc(p.fork_of["name"])}</a></p>'
        if p.fork_of else ""
    )
    desc = f'<p class="card-desc">{esc(p.description)}</p>' if p.description else ""
    tags = "".join(f"<li>{esc(t)}</li>" for t in p.tags)
    tags = f'<ul class="tags" aria-label="Stack">{tags}</ul>' if tags else ""
    stars = f'<span class="stars" title="Stars">{ICONS["star"]}{p.stars}</span>' if p.stars else ""
    links = f'<a href="{esc(p.url)}">{ICONS["github"]}Code</a>'
    if p.website:
        links += f'<a href="{esc(p.website)}">{ICONS["globe"]}Website</a>'
    search = " ".join([p.name, p.title, p.description, p.category, *p.tags]).lower()

    return f"""<li class="card" data-category="{esc(p.category)}" data-name="{esc(p.title.lower())}" data-pushed="{esc(p.pushed_at)}" data-created="{esc(p.created_at)}" data-pinned="{int(p.pinned)}" data-search="{esc(search)}" style="view-transition-name: p-{p.slug}">
  {media}
  <div class="card-body">
    <h3 class="card-title"><a href="{esc(p.url)}">{breakable(p.title)}</a>{badges}</h3>
    {fork}{desc}{tags}
    <div class="card-foot">
      <span class="card-meta"><time datetime="{esc(p.pushed_at)}" data-relative>Updated {display_date(p.pushed_at)}</time>{stars}</span>
      <span class="card-links">{links}</span>
    </div>
  </div>
</li>"""


def fill(template: str, values: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"template placeholder {{{{ {key} }}}} has no value")
        return values[key]

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", replace, template)


def render_page(config: dict, projects: list[Project], built: datetime) -> str:
    site = config["site"]
    counts = {c["id"]: sum(p.category == c["id"] for p in projects) for c in config["categories"]}
    # Config order decides matching priority; the page lists the biggest category first.
    categories = sorted((c for c in config["categories"] if counts[c["id"]]), key=lambda c: -counts[c["id"]])

    filters = [
        f'<label class="chip"><input type="radio" name="category" value="all" checked>'
        f'<span>All <span class="chip-count">{len(projects)}</span></span></label>'
    ]
    filters += [
        f'<label class="chip"><input type="radio" name="category" value="{esc(c["id"])}">'
        f'<span>{esc(c["label"])} <span class="chip-count">{counts[c["id"]]}</span></span></label>'
        for c in categories
    ]
    category_reply = ",<wbr>".join(
        f'<a href="#{esc(c["id"])}" tabindex="-1" data-category-link="{esc(c["id"])}">'
        f'{esc(c["label"].upper())} {counts[c["id"]]}</a>'
        for c in categories
    )
    summary = ", ".join(f'{counts[c["id"]]} {c["label"].lower()}' for c in categories)
    links = "".join(
        f'<a class="button{" button-primary" if i == 0 else ""}" href="{esc(link["url"])}">'
        f'{ICONS.get(link.get("icon", ""), ICONS["link"])}{esc(link["label"])}</a>'
        for i, link in enumerate(site.get("links", []))
    )
    profile = next((l["url"] for l in site.get("links", [])), f"https://github.com/{site['user']}")
    verification = site.get("google_site_verification")
    date = built.strftime("%Y-%m-%d")
    assets = "".join((SRC / name).read_text(encoding="utf-8") for name in ("style.css", "app.js"))

    return fill((SRC / "index.html").read_text(encoding="utf-8"), {
        "page_title": esc(site.get("page_title", site["name"])),
        "meta_description": esc(site.get("description", "")),
        "site_url": esc(site["url"]),
        "verification_meta": (
            f'<meta name="google-site-verification" content="{esc(verification)}">' if verification else ""
        ),
        "asset_version": hashlib.sha1(assets.encode()).hexdigest()[:10],
        "name": esc(site["name"]),
        "role": esc(site.get("role", "")),
        "intro": esc(site.get("intro", "")),
        "links": links,
        "idn_reply": ",<wbr>".join(f'<span class="nowrap">{esc(part)}</span>' for part in (
            site["user"].upper(), site.get("idn_model", "PORTFOLIO"), f"{len(projects)} PROJECTS", date,
        )),
        "category_reply": category_reply,
        "readout_summary": esc(f"{len(projects)} projects: {summary}. Last built {date}."),
        "filters": "\n".join(filters),
        "project_total": str(len(projects)),
        "cards": "\n".join(render_card(p) for p in projects),
        "built_iso": built.isoformat(timespec="seconds"),
        "built_human": f"{built.day} {built:%B %Y}",
        "profile_url": esc(profile),
        "profile_label": esc(re.sub(r"^https?://(www\.)?", "", profile).rstrip("/")),
    })


def render_sitemap(config: dict, projects: list[Project], built: datetime) -> str:
    base = config["site"]["url"]
    entries = [(base, built.strftime("%Y-%m-%d"))]
    entries += [(p.website, p.pushed_at[:10]) for p in projects if p.website and p.website.startswith(base)]
    urls = "\n".join(
        f"  <url>\n    <loc>{esc(loc)}</loc>\n    <lastmod>{lastmod}</lastmod>\n  </url>"
        for loc, lastmod in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n</urlset>\n"
    )


# ---------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="_site", help="output directory (default: _site)")
    args = parser.parse_args()

    out = (ROOT / args.out).resolve()
    if out in (ROOT, SRC) or ROOT not in out.parents:
        parser.error("--out must be a subdirectory of the repository other than src/")

    config = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    user = config["site"]["user"]
    repo_rules = config.get("repos", {})
    overrides = config.get("projects", {})

    gh = GitHub(find_token())
    print(f"Listing repositories of {user} ({'with' if gh.token else 'without'} token)")
    try:
        repos = gh.list_repos(user)
    except (urllib.error.URLError, RateLimited) as err:
        print(f"error: cannot list repositories: {err}", file=sys.stderr)
        return 1

    excluded = {name.lower() for name in repo_rules.get("exclude", [])}
    repos = [
        r for r in repos
        if r["name"].lower() not in excluded
        and not overrides.get(r["name"], {}).get("hidden")
        and (repo_rules.get("include_forks", True) or not r.get("fork"))
        and (repo_rules.get("include_archived", True) or not r.get("archived"))
    ]

    # Empty the output directory but keep it, so a preview server running in it survives.
    out.mkdir(exist_ok=True)
    for child in out.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    assets = out / "assets" / "projects"
    assets.mkdir(parents=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        projects = list(pool.map(lambda r: build_project(gh, r, config, assets), repos))
    projects.sort(key=lambda p: p.pushed_at, reverse=True)
    projects.sort(key=lambda p: not p.pinned)

    for p in projects:
        extra = f"  ({', '.join(p.notes)})" if p.notes else ""
        print(f"  {p.name:<42} {p.category:<9} {', '.join(p.tags)}{extra}")

    built = datetime.now(timezone.utc)
    (out / "index.html").write_text(render_page(config, projects, built), encoding="utf-8")
    (out / "sitemap.xml").write_text(render_sitemap(config, projects, built), encoding="utf-8")
    (out / "projects.json").write_text(
        json.dumps([{k: v for k, v in vars(p).items() if k != "notes"} for p in projects], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    for name in STATIC_FILES:
        shutil.copy2(SRC / name, out / name)

    if Image is None:
        warn("Pillow is not installed, previews link to the original images")
    print(f"Built {len(projects)} projects into {out.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
