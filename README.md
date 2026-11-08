# ThadCastle

A Jekyll blog on GitHub Pages, written in Obsidian.

## How it works

```
Obsidian (_notes/)  --Obsidian Git-->  GitHub  --Actions-->  convert notes  -->  Jekyll build  -->  Pages
```

- Notes live in `_notes/`. Jekyll ignores that folder, so raw notes are never served.
- On every push to `main`, `.github/workflows/pages.yml` runs
  `scripts/obsidian_to_jekyll.py`, which turns **only notes with `publish: true`**
  into posts, then builds and deploys the site.
- Generated files (`_posts/obsidian/`, `assets/notes/`) are git-ignored and rebuilt each time.
- Hand-written posts in `_posts/` still work as usual.

## One-time setup

1. **GitHub Pages source:** in the repo, go to *Settings → Pages → Build and deployment*
   and set **Source** to **GitHub Actions**. (Until you do, Pages keeps building the
   branch directly and your notes won't appear.)
2. **Clone the repo** to the device you write on and open the repo folder as an
   Obsidian vault (*Open folder as vault*). The included `.obsidian/app.json` puts new
   notes in `_notes/` and pasted images in `_notes/attachments/`.
3. **Install the Obsidian Git community plugin** (*Settings → Community plugins → Browse →
   "Git"*). Suggested settings:
   - *Auto commit-and-sync interval*: e.g. 10 minutes (or sync manually with the
     "Commit-and-sync" command)
   - *Pull on startup*: on
4. Optional: enable the core **Templates** plugin. It's already pointed at
   `_notes/templates/`, which has a "Blog post" template.

On mobile, Obsidian Git works but is slower; alternatively sync the vault to a desktop
with Obsidian Sync/iCloud and let the desktop push.

## Publishing a note

Add front matter like this to any note in `_notes/`:

```yaml
---
publish: true
title: My post          # optional: defaults to the first "# Heading", then the filename
date: 2026-11-08        # optional: defaults to a YYYY-MM-DD filename prefix, then the first commit date
tags: [obsidian]        # optional: any other keys are passed through to Jekyll
slug: my-post           # optional: defaults to the filename
---
```

Set `publish: false` (or remove it) to take a post down on the next sync.

The post's URL is `/YYYY/MM/DD/slug/`. Note that changing the date or slug changes the URL.

## What gets converted

| Obsidian | On the site |
| --- | --- |
| `[[Note]]`, `[[Note\|text]]`, `[[Note#Heading]]` | Link to that post (matches filename, filename without date prefix, or `aliases`) |
| Link to an unpublished note | Plain text, so private notes are never exposed |
| `![[image.png]]`, `![[image.png\|300]]` | Image (optionally 300px wide); only referenced attachments are copied |
| `![[file.pdf]]` | Link to the file |
| `![[Other note]]` | Link (transclusion isn't supported) |
| `![alt](attachments/x.png)` | Image with the path fixed |
| `%%comment%%` | Removed |
| `==highlight==` | `<mark>highlight</mark>` |
| `> [!note] Title` callouts | Blockquote with a bold title |
| `{{ ... }}` / `{% ... %}` in text | Shown literally (not treated as Liquid) |

Code blocks and inline code are left untouched. `_notes/templates/` and dot-folders are skipped.

## Preview locally

```sh
pip install pyyaml
python3 scripts/obsidian_to_jekyll.py   # see what would be published, with warnings
bundle install
bundle exec jekyll serve                # http://localhost:4000
```
