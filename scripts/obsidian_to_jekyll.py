#!/usr/bin/env python3
"""Convert Obsidian notes into Jekyll posts.

Only notes whose front matter contains ``publish: true`` are converted; every
other note stays private and never reaches the built site.

For each published note this script:
  * writes ``<posts-dir>/YYYY-MM-DD-<slug>.md`` with Jekyll front matter
  * rewrites ``[[wikilinks]]`` to published posts (or plain text if the target
    is not published)
  * rewrites ``![[embeds]]`` and local Markdown images, copying only the
    referenced attachments into ``<assets-dir>``
  * strips ``%%comments%%``, turns ``==highlights==`` into ``<mark>`` and
    renders callouts as titled blockquotes

The output directories are generated and wiped on every run. They are
git-ignored; the GitHub Actions workflow regenerates them at build time.

Usage:
    python3 scripts/obsidian_to_jekyll.py [--src _notes] [--posts-dir _posts/obsidian]
                                          [--assets-dir assets/notes]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import yaml

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".bmp"}
SKIP_DIRS = {"templates", ".obsidian", ".trash"}
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[-_ ]*(.*)$")
# Fenced code blocks and inline code spans are left untouched.
CODE_RE = re.compile(r"(^(```|~~~)[^\n]*\n.*?^\2[^\n]*$|`[^`\n]+`)", re.DOTALL | re.MULTILINE)
EMBED_RE = re.compile(r"!\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|([^\]]*))?\]\]")
WIKILINK_RE = re.compile(r"\[\[([^\]|#^]*)((?:#\^?|\^)[^\]|]*)?(?:\|([^\]]*))?\]\]")
MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((?!https?://|/)([^)\s]+)(\s+\"[^\"]*\")?\)")
COMMENT_RE = re.compile(r"%%.*?%%", re.DOTALL)
HIGHLIGHT_RE = re.compile(r"==(?=\S)(.+?)(?<=\S)==")
CALLOUT_RE = re.compile(r"^(>\s*)\[!(\w+)\][+-]?[ \t]*(.*)$", re.MULTILINE)


@dataclass
class Note:
    path: Path
    meta: dict
    body: str
    stem: str
    date: str = ""
    slug: str = ""
    url: str = ""
    out_name: str = ""
    warnings: list[str] = field(default_factory=list)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "untitled"


def kramdown_anchor(heading: str) -> str:
    """Mirror kramdown's auto-generated header ids."""
    anchor = re.sub(r"^[^a-zA-Z]+", "", heading)
    anchor = re.sub(r"[^a-zA-Z0-9 -]", "", anchor)
    return anchor.replace(" ", "-").lower()


def split_front_matter(text: str) -> tuple[dict, str]:
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[match.end():]


def is_published(meta: dict) -> bool:
    value = meta.get("publish")
    return value is True or str(value).strip().lower() in {"true", "yes"}


def git_created_date(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "log", "--diff-filter=A", "--follow", "--format=%as", "--", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return None
    return out[-1] if out else None


def resolve_date(note: Note) -> str:
    value = note.meta.get("date")
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime("%Y-%m-%d")
    if value:
        match = re.match(r"\d{4}-\d{2}-\d{2}", str(value))
        if match:
            return match.group(0)
        note.warnings.append(f"could not parse date {value!r}")
    prefix = DATE_PREFIX_RE.match(note.stem)
    if prefix:
        return prefix.group(1)
    return git_created_date(note.path) or dt.date.today().isoformat()


def iter_files(src: Path):
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if path.is_file():
            yield path


class Converter:
    def __init__(self, src: Path, posts_dir: Path, assets_dir: Path, assets_url: str):
        self.src = src
        self.posts_dir = posts_dir
        self.assets_dir = assets_dir
        self.assets_url = assets_url.rstrip("/")
        self.notes: dict[str, Note] = {}         # lowercased stem -> published note
        self.attachments: dict[str, Path] = {}   # lowercased filename -> path
        self.copied: dict[Path, str] = {}        # source attachment -> public URL
        self.used_asset_names: set[str] = set()

    # -- discovery ---------------------------------------------------------

    def load(self) -> None:
        self.src = self.src.resolve()
        for path in iter_files(self.src):
            path = path.resolve()
            if path.suffix.lower() != ".md":
                self.attachments.setdefault(path.name.lower(), path)
                continue
            meta, body = split_front_matter(path.read_text(encoding="utf-8"))
            if not is_published(meta):
                continue
            note = Note(path=path, meta=meta, body=body, stem=path.stem)
            note.date = resolve_date(note)
            prefix = DATE_PREFIX_RE.match(note.stem)
            base = prefix.group(2) if prefix and prefix.group(2) else note.stem
            note.slug = slugify(str(meta.get("slug") or base))
            year, month, day = note.date.split("-")
            note.url = f"/{year}/{month}/{day}/{note.slug}/"
            note.out_name = f"{note.date}-{note.slug}.md"
            key = note.stem.lower()
            if key in self.notes:
                sys.exit(f"error: two published notes are named {note.stem!r}")
            self.notes[key] = note

        # Also let [[links]] match a note's name without its date prefix, and
        # its Obsidian aliases, as long as that doesn't shadow a real filename.
        for note in list(self.notes.values()):
            aliases = note.meta.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            prefix = DATE_PREFIX_RE.match(note.stem)
            names = ([prefix.group(2)] if prefix and prefix.group(2) else []) + [str(a) for a in aliases]
            for name in names:
                self.notes.setdefault(name.lower(), note)

        seen: dict[str, Path] = {}
        for note in {id(n): n for n in self.notes.values()}.values():
            if note.out_name in seen and seen[note.out_name] != note.path:
                sys.exit(f"error: {note.path} and {seen[note.out_name]} both map to {note.out_name}")
            seen[note.out_name] = note.path

    # -- attachments -------------------------------------------------------

    def find_attachment(self, target: str, note: Note) -> Path | None:
        target = urllib.parse.unquote(target.strip())
        relative = (note.path.parent / target).resolve()
        if relative.is_file() and self.src.resolve() in relative.parents:
            return relative
        return self.attachments.get(Path(target).name.lower())

    def publish_attachment(self, path: Path) -> str:
        if path in self.copied:
            return self.copied[path]
        name = f"{slugify(path.stem)}{path.suffix.lower()}"
        if name in self.used_asset_names:
            digest = hashlib.sha1(str(path).encode()).hexdigest()[:8]
            name = f"{slugify(path.stem)}-{digest}{path.suffix.lower()}"
        self.used_asset_names.add(name)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, self.assets_dir / name)
        self.copied[path] = f"{self.assets_url}/{name}"
        return self.copied[path]

    # -- markdown rewriting ------------------------------------------------

    def link_target(self, name: str, note: Note) -> Note | None:
        name = name.strip()
        if not name:
            return note  # [[#Heading]] links within the same note
        return self.notes.get(Path(name).stem.lower() if name.endswith(".md") else Path(name).name.lower())

    def rewrite_embed(self, match: re.Match, note: Note) -> str:
        target, option = match.group(1).strip(), (match.group(2) or "").strip()
        ext = Path(target).suffix.lower()
        if ext and ext != ".md":
            path = self.find_attachment(target, note)
            if not path:
                note.warnings.append(f"missing attachment {target!r}")
                return ""
            url = self.publish_attachment(path)
            if ext in IMAGE_EXTS:
                width = re.fullmatch(r"(\d+)(?:x(\d+))?", option)
                alt = "" if width else option
                image = f"![{alt or Path(target).stem}]({url})"
                if width:
                    image += f'{{: width="{width.group(1)}"}}'
                return image
            return f"[{option or Path(target).name}]({url})"
        linked = self.link_target(target, note)
        label = option or Path(target).stem
        if linked:
            return f"[{label}]({linked.url})"
        return label

    def rewrite_wikilink(self, match: re.Match, note: Note) -> str:
        target, anchor, alias = match.group(1), match.group(2) or "", match.group(3)
        heading = anchor.lstrip("#") if anchor.startswith("#") and not anchor.startswith("#^") else ""
        label = alias or (target.strip() or heading) or target
        if alias is None and target.strip() and heading:
            label = f"{target.strip()} > {heading}"
        linked = self.link_target(target, note)
        if not linked:
            return label
        url = "" if linked is note else linked.url
        if heading:
            url += f"#{kramdown_anchor(heading.split('#')[-1])}"
        return f"[{label}]({url or linked.url})"

    def rewrite_md_image(self, match: re.Match, note: Note) -> str:
        alt, target, title = match.group(1), match.group(2), match.group(3) or ""
        path = self.find_attachment(target, note)
        if not path:
            note.warnings.append(f"missing image {target!r}")
            return match.group(0)
        return f"![{alt}]({self.publish_attachment(path)}{title})"

    @staticmethod
    def rewrite_callout(match: re.Match) -> str:
        quote, kind, title = match.groups()
        return f"{quote}**{title.strip() or kind.capitalize()}**"

    def convert_text(self, text: str, note: Note) -> str:
        parts = CODE_RE.split(text)
        out = []
        # CODE_RE has two groups, so split yields [text, code, fence, text, ...]
        for i in range(0, len(parts), 3):
            chunk = parts[i]
            chunk = COMMENT_RE.sub("", chunk)
            chunk = EMBED_RE.sub(lambda m: self.rewrite_embed(m, note), chunk)
            chunk = WIKILINK_RE.sub(lambda m: self.rewrite_wikilink(m, note), chunk)
            chunk = MD_IMAGE_RE.sub(lambda m: self.rewrite_md_image(m, note), chunk)
            chunk = HIGHLIGHT_RE.sub(r"<mark>\1</mark>", chunk)
            chunk = CALLOUT_RE.sub(self.rewrite_callout, chunk)
            out.append(chunk)
            if i + 1 < len(parts):
                out.append(parts[i + 1])
        return "".join(out)

    # -- output ------------------------------------------------------------

    def write(self) -> None:
        self.posts_dir.mkdir(parents=True, exist_ok=True)
        unique = {id(n): n for n in self.notes.values()}.values()
        for note in sorted(unique, key=lambda n: n.out_name):
            body = self.convert_text(note.body, note)
            meta = {k: v for k, v in note.meta.items() if k not in {"publish", "slug"}}
            meta.setdefault("layout", "post")
            if "title" not in meta:
                heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
                meta["title"] = heading.group(1).strip() if heading else note.stem
            if heading_match := re.match(r"\s*#\s+(.+)\n", body):
                if heading_match.group(1).strip() == str(meta["title"]):
                    body = body[heading_match.end():]  # the layout already shows the title
            meta.setdefault("date", note.date)
            # Notes are Markdown, not Liquid templates: keep "{{" and "{%" literal.
            body = body.replace("{% endraw %}", '{% endraw %}{{ "{% endraw %}" }}{% raw %}')
            front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
            content = f"---\n{front}\n---\n{{% raw %}}\n{body.strip()}\n{{% endraw %}}\n"
            (self.posts_dir / note.out_name).write_text(content, encoding="utf-8")
            rel = note.path.relative_to(self.src)
            print(f"published {rel} -> {note.url}")
            for warning in note.warnings:
                print(f"  warning: {warning}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", default="_notes", type=Path)
    parser.add_argument("--posts-dir", default="_posts/obsidian", type=Path)
    parser.add_argument("--assets-dir", default="assets/notes", type=Path)
    parser.add_argument("--assets-url", default="/assets/notes")
    args = parser.parse_args()

    for generated in (args.posts_dir, args.assets_dir):
        if generated.exists():
            shutil.rmtree(generated)
    if not args.src.is_dir():
        print(f"no notes folder at {args.src}; nothing to publish")
        return

    converter = Converter(args.src, args.posts_dir, args.assets_dir, args.assets_url)
    converter.load()
    converter.write()
    count = len({id(n) for n in converter.notes.values()})
    print(f"{count} note(s) published, {len(converter.copied)} attachment(s) copied")


if __name__ == "__main__":
    main()
