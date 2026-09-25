# bartkepl.github.io

Portfolio at <https://bartkepl.github.io/>, generated from the GitHub API.

Every public repository shows up automatically after a rebuild. For each one
`generate.py` reads:

- the description, or the first paragraph of the README when the description is empty
- a preview: the first non-badge image in the README (a `*_angle*` render is preferred);
  KiCad 3D renders get their lavender background removed and are cropped
- stack tags from the file tree (`.kicad_pro` → KiCad, `.ioc` → STM32,
  `pubspec.yaml` → Flutter, …) plus the main languages
- a category (Hardware / Software / Tools) from topics, keywords and files

## Rebuilding

The site is built only on demand: **Actions → Build & Deploy Pages → Run workflow**, or

```sh
gh workflow run pages.yml
```

## Local preview

```sh
pip install -r requirements.txt
python generate.py                     # writes _site/
python -m http.server -d _site 8000
```

Locally the script uses `GITHUB_TOKEN`/`GH_TOKEN` or `gh auth token` when available;
without a token GitHub allows only 60 API requests per hour.

## Files

| Path | Purpose |
|---|---|
| `config.toml` | Profile texts, category rules and per-project overrides (description, image, category, pinned, hidden, …) |
| `generate.py` | Fetches repositories and renders the site into `_site/` |
| `src/` | Page template, styles, script, favicon, robots.txt |
| `.github/workflows/pages.yml` | Builds and deploys to GitHub Pages (manual trigger) |
