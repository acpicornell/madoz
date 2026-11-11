"""Lint the built site against the security policy it ships with.

The Content-Security-Policy in the site's own `_headers` says what a page may
load and run. A page that needs anything else does not fail the build -- it
fails silently in the reader's browser, and a plain local server, which ignores
`_headers`, shows it working. This script is the build's way of finding out
first. It is the family's version of cronicon's `scripts/check_site.py`: the
same checks, but read off this site's policy instead of written for one.

Checked on every page, against the `/*` policy in `_headers`:

  inline `<script>` (other than JSON data blocks) and `on…=` handlers, unless
    script-src admits 'unsafe-inline'
  `style="…"` attributes and `<style>` blocks, unless style-src (or
    style-src-attr / style-src-elem) admits 'unsafe-inline'
  every script, stylesheet and image is from an origin its directive admits
  every external link that opens a new tab carries rel="noopener"
  every internal link resolves to a file, and every `#fragment` to an id
  ids are unique

It reads the HTML as built. What a script adds at run time -- tab contents
drawn from JSON, a library loaded on demand -- is not seen here, so the policy
still has to be read when a script starts loading something new.

Standard library only, so it runs wherever Python does.

Usage:
  python3 scripts/check_site.py [SITE_DIR]   # default: web; exit 1 on any finding
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

DATA_SCRIPTS = {"application/ld+json", "application/json"}


def read_policy(site: Path) -> dict[str, list[str]]:
    """The CSP of the `/*` rule in the site's `_headers`, as directive -> sources."""
    headers = site / "_headers"
    if not headers.exists():
        sys.exit(f"{headers} not found: no policy to check against")
    current = None
    for line in headers.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            current = line.strip()
        elif current == "/*" and line.strip().lower().startswith("content-security-policy:"):
            value = line.split(":", 1)[1]
            policy = {}
            for part in value.split(";"):
                words = part.split()
                if words:
                    policy[words[0].lower()] = words[1:]
            return policy
    sys.exit(f"{headers}: no Content-Security-Policy under /*")


def sources(policy: dict, *names: str) -> list[str]:
    """The sources of the first directive present, falling back to default-src."""
    for name in names + ("default-src",):
        if name in policy:
            return policy[name]
    return []


def admits(srcs: list[str], url: str) -> bool:
    """Does a source list admit loading `url` (absolute, or relative = self)?"""
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc:
        return "'self'" in srcs
    if parts.scheme == "data":
        return "data:" in srcs
    origin = f"{parts.scheme}://{parts.netloc}"
    for src in srcs:
        if src in (f"{parts.scheme}:", "*"):
            return True
        if src.startswith(("'",)) or "://" not in src:
            continue
        pattern = re.escape(src.rstrip("/")).replace(r"\*", "[^/]+")
        if re.fullmatch(pattern, origin) or url.startswith(src.rstrip("/") + "/"):
            return True
    return False


class Page(HTMLParser):
    def __init__(self, policy: dict) -> None:
        super().__init__(convert_charrefs=True)
        self.findings: list[str] = []
        self.ids: Counter = Counter()
        self.links: list[str] = []
        script = sources(policy, "script-src-elem", "script-src")
        self.inline_scripts = "'unsafe-inline'" in sources(policy, "script-src")
        self.inline_style_attr = "'unsafe-inline'" in sources(policy, "style-src-attr", "style-src")
        self.inline_style_elem = "'unsafe-inline'" in sources(policy, "style-src-elem", "style-src")
        self.script_srcs = script
        self.style_srcs = sources(policy, "style-src-elem", "style-src")
        self.img_srcs = sources(policy, "img-src")
        self.in_script = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = {k: (v or "") for k, v in attrs}
        if "style" in a and not self.inline_style_attr:
            self.findings.append(f"inline style on <{tag}>")
        if not self.inline_scripts:
            for name in a:
                if name.startswith("on"):
                    self.findings.append(f"inline handler {name} on <{tag}>")
        if tag == "style" and not self.inline_style_elem:
            self.findings.append("<style> block")
        if tag == "script":
            if "src" in a:
                if not admits(self.script_srcs, a["src"]):
                    self.findings.append(f"script not admitted: {a['src']}")
            elif a.get("type", "").lower() not in DATA_SCRIPTS and not self.inline_scripts:
                self.findings.append("inline <script>")
        if tag == "link" and "stylesheet" in a.get("rel", "").split():
            if not admits(self.style_srcs, a.get("href", "")):
                self.findings.append(f"stylesheet not admitted: {a.get('href')}")
        if tag == "img" and a.get("src"):
            if not admits(self.img_srcs, a["src"]):
                self.findings.append(f"image not admitted: {a['src'][:80]}")
        if tag == "a" and a.get("href"):
            href = a["href"]
            if urlsplit(href).netloc or href.startswith("//"):
                if a.get("target") == "_blank" and "noopener" not in a.get("rel", "") \
                        and "noreferrer" not in a.get("rel", ""):
                    self.findings.append(f"new-tab link without noopener: {href}")
            elif not urlsplit(href).scheme:
                self.links.append(href)
        if "id" in a:
            self.ids[a["id"]] += 1


def target_of(site: Path, page: Path, href: str) -> tuple[Path, str]:
    """The file an internal link points at, and its fragment."""
    parts = urlsplit(href)
    path = unquote(parts.path)
    if not path:
        return page, parts.fragment
    base = site if path.startswith("/") else page.parent
    target = (base / path.lstrip("/")).resolve()
    if path.endswith("/") or target.is_dir():
        target = target / "index.html"
    elif not target.exists() and not target.suffix:
        target = target.with_suffix(".html")
    return target, parts.fragment


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("site", nargs="?", default="web")
    args = ap.parse_args()
    site = Path(args.site).resolve()
    policy = read_policy(site)

    pages = sorted(site.rglob("*.html"))
    parsed: dict[Path, Page] = {}
    for path in pages:
        page = Page(policy)
        page.feed(path.read_text(encoding="utf-8"))
        parsed[path.resolve()] = page

    findings: Counter = Counter()
    examples: dict[str, str] = {}

    def report(kind: str, where: Path, detail: str = "") -> None:
        findings[kind] += 1
        examples.setdefault(kind, f"{where.relative_to(site)} {detail}".strip())

    for path, page in parsed.items():
        for finding in page.findings:
            report(finding.split(":")[0].split(" on <")[0], path, finding)
        for ident, n in page.ids.items():
            if n > 1:
                report("duplicate id", path, f"#{ident} x{n}")
        for href in page.links:
            target, fragment = target_of(site, path, href)
            if not target.exists():
                report("dead link", path, href)
            elif fragment and target in parsed and ":~:" not in fragment:
                if fragment not in parsed[target].ids:
                    report("dead fragment", path, href)

    print(f"{len(pages)} pages checked against {site.name}/_headers")
    for kind, n in findings.most_common():
        print(f"  {n:6d}  {kind:34s} e.g. {examples[kind]}")
    if findings:
        sys.exit(1)
    print("  clean")


if __name__ == "__main__":
    main()
