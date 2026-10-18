#!/usr/bin/env python3
"""Build a responsive, crawler-readable static documentation site for GitHub Pages."""
from __future__ import annotations
import argparse
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import markdown

SKIP_DIRS = {'.git', '.github', 'node_modules', '_site', '.bundle', '.sass-cache'}
ASSET_EXTS = {'.png','.jpg','.jpeg','.gif','.svg','.webp','.ico','.css','.js','.mjs','.pdf','.woff','.woff2','.ttf','.otf','.mp4','.webm','.mp3','.wav'}
MD_EXTS = {'.md','.markdown'}
FRONT_MATTER = re.compile(r'\A---\s*\r?\n.*?\r?\n---\s*\r?\n', re.S)
LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')


def plain_text(markdown_text: str) -> str:
    text = FRONT_MATTER.sub('', markdown_text)
    text = re.sub(r'```.*?```', ' ', text, flags=re.S)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    text = re.sub(r'!\[[^]]*\]\([^)]*\)', ' ', text)
    text = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[#>*_~|=-]+', ' ', text)
    return re.sub(r'\s+', ' ', html.unescape(text)).strip()


def page_title(source: Path, text: str, site_title: str) -> str:
    for line in FRONT_MATTER.sub('', text).splitlines():
        match = re.match(r'^\s*#\s+(.+?)\s*#*\s*$', line)
        if match:
            return plain_text(match.group(1))[:100]
    if source.name.lower() in {'readme.md','index.md'}:
        return site_title
    return plain_text(source.stem.replace('-', ' ').replace('_', ' ').title())[:100] or site_title


def output_path_for_md(source: Path, root: Path, home_source: Path | None) -> Path:
    rel = source.relative_to(root)
    if home_source is not None and source == home_source:
        return Path('index.html')
    return rel.with_suffix('.html')


class LinkRewriter(HTMLParser):
    def __init__(self, html_text: str, source: Path, source_root: Path, output_rel: Path, out_root: Path, md_map: dict[Path, Path]):
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.source = source
        self.source_root = source_root
        self.output_rel = output_rel
        self.out_root = out_root
        self.md_map = md_map
        self.feed(html_text)
        self.close()

    def handle_starttag(self, tag, attrs):
        new=[]
        for key,val in attrs:
            if key.lower() == 'href' and val:
                val = self.rewrite(val)
            new.append((key,val))
        self.parts.append('<'+tag)
        for key,val in new:
            if val is None: self.parts.append(' '+key)
            else: self.parts.append(' '+key+'="'+html.escape(val,quote=True)+'"')
        self.parts.append('>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag,attrs); self.parts[-1] = '/>'

    def handle_endtag(self, tag): self.parts.append('</'+tag+'>')
    def handle_data(self, data): self.parts.append(data)
    def handle_entityref(self, name): self.parts.append('&'+name+';')
    def handle_charref(self, name): self.parts.append('&#'+name+';')
    def handle_comment(self, data): self.parts.append('<!--'+data+'-->')

    def rewrite(self, href: str) -> str:
        parts=urlsplit(href)
        if parts.scheme or parts.netloc or not parts.path.lower().endswith(('.md','.markdown')):
            return href
        candidate=(self.source.parent / unquote(parts.path)).resolve()
        try: candidate.relative_to(self.source_root.resolve())
        except ValueError: return href
        dest=self.md_map.get(candidate)
        if not dest: return href
        rel=posixpath.relpath(dest.as_posix(),self.output_rel.parent.as_posix() or '.')
        if not rel.startswith('.') and '/' not in rel: rel='./'+rel
        return urlunsplit(('', '', quote(rel,safe='/._-'), parts.query, parts.fragment))

    def result(self) -> str:
        return ''.join(self.parts)


def parse_nav(root: Path, md_map: dict[Path, Path], base_path: str) -> str:
    summaries=[root/'SUMMARY.md',root/'summary.md']
    summary=next((p for p in summaries if p.exists()),None)
    entries=[]
    if summary:
        for line in summary.read_text(encoding='utf-8',errors='replace').splitlines():
            m=LINK_RE.search(line)
            if not m: continue
            label,target=m.group(1).strip(),m.group(2).strip()
            if target.startswith(('http://','https://','#','mailto:')): continue
            target_path=unquote(target.split('#',1)[0].split('?',1)[0])
            source=(summary.parent/target_path).resolve()
            out=md_map.get(source)
            if not out: continue
            depth=max(0,len(line)-len(line.lstrip(' ')))//2
            entries.append((depth,label,out.as_posix()))
    if not entries:
        for source,out in sorted(md_map.items(),key=lambda x:x[1].as_posix()):
            if out.as_posix()=='index.html': continue
            entries.append((0,page_title(source,source.read_text(encoding='utf-8',errors='replace'),''),out.as_posix()))
    lis=[]
    for depth,label,path in entries[:500]:
        href=base_path.rstrip('/')+'/'+quote(path,safe='/._-')
        lis.append(f'<li style="--depth:{depth}"><a href="{html.escape(href,quote=True)}">{html.escape(label)}</a></li>')
    return '<nav class="toc" aria-label="Table of contents"><h2>Contents</h2><ul>'+''.join(lis)+'</ul></nav>' if lis else ''


def template(title: str, description: str, site_title: str, canonical: str, base_path: str, topics: list[str], body: str, nav: str, is_home: bool=False) -> str:
    keywords=', '.join(topics[:8])
    css=(base_path.rstrip('/')+'/assets/site.css') if base_path else '/assets/site.css'
    ogtype='website' if is_home else 'article'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} | {html.escape(site_title)}</title>
<meta name="description" content="{html.escape(description,quote=True)}"><meta name="keywords" content="{html.escape(keywords,quote=True)}">
<link rel="canonical" href="{html.escape(canonical,quote=True)}"><meta property="og:type" content="{ogtype}"><meta property="og:title" content="{html.escape(title,quote=True)}"><meta property="og:description" content="{html.escape(description,quote=True)}"><meta property="og:url" content="{html.escape(canonical,quote=True)}"><meta property="og:site_name" content="{html.escape(site_title,quote=True)}">
<meta name="twitter:card" content="summary"><meta name="twitter:title" content="{html.escape(title,quote=True)}"><meta name="twitter:description" content="{html.escape(description,quote=True)}">
<link rel="stylesheet" href="{html.escape(css,quote=True)}"></head><body><header class="site-header"><a class="brand" href="{html.escape((base_path or '')+'/',quote=True)}">{html.escape(site_title)}</a><a class="source" href="https://github.com/HumayunShariarHimu">GitHub profile</a></header>
<div class="shell">{nav}<main><article>{body}</article><footer>Maintained by Humayun Shariar Himu · <a href="https://github.com/HumayunShariarHimu">GitHub</a></footer></main></div></body></html>'''


def inject_metadata(raw: str, title: str, description: str, canonical: str, site_title: str, topics: list[str]) -> str:
    keywords=', '.join(topics[:8])
    title_tag=f'<title>{html.escape(title)} | {html.escape(site_title)}</title>'
    if re.search(r'<title>.*?</title>',raw,re.I|re.S):
        raw=re.sub(r'<title>.*?</title>',lambda _:title_tag,raw,count=1,flags=re.I|re.S)
    else:
        raw=re.sub(r'<head(\b[^>]*)>',lambda m:m.group(0)+title_tag,raw,count=1,flags=re.I)
    entries=[
        (r'<meta\b(?=[^>]*\bname=["\']description["\'])[^>]*>',f'<meta name="description" content="{html.escape(description,quote=True)}">'),
        (r'<meta\b(?=[^>]*\bname=["\']keywords["\'])[^>]*>',f'<meta name="keywords" content="{html.escape(keywords,quote=True)}">'),
        (r'<link\b(?=[^>]*\brel=["\']canonical["\'])[^>]*>',f'<link rel="canonical" href="{html.escape(canonical,quote=True)}">'),
        (r'<meta\b(?=[^>]*\bproperty=["\']og:type["\'])[^>]*>','<meta property="og:type" content="website">'),
        (r'<meta\b(?=[^>]*\bproperty=["\']og:title["\'])[^>]*>',f'<meta property="og:title" content="{html.escape(title,quote=True)}">'),
        (r'<meta\b(?=[^>]*\bproperty=["\']og:description["\'])[^>]*>',f'<meta property="og:description" content="{html.escape(description,quote=True)}">'),
        (r'<meta\b(?=[^>]*\bproperty=["\']og:url["\'])[^>]*>',f'<meta property="og:url" content="{html.escape(canonical,quote=True)}">'),
        (r'<meta\b(?=[^>]*\bproperty=["\']og:site_name["\'])[^>]*>',f'<meta property="og:site_name" content="{html.escape(site_title,quote=True)}">'),
        (r'<meta\b(?=[^>]*\bname=["\']twitter:card["\'])[^>]*>','<meta name="twitter:card" content="summary">'),
        (r'<meta\b(?=[^>]*\bname=["\']twitter:title["\'])[^>]*>',f'<meta name="twitter:title" content="{html.escape(title,quote=True)}">'),
        (r'<meta\b(?=[^>]*\bname=["\']twitter:description["\'])[^>]*>',f'<meta name="twitter:description" content="{html.escape(description,quote=True)}">'),
    ]
    missing=[]
    for pattern,tag in entries:
        if re.search(pattern,raw,re.I): raw=re.sub(pattern,lambda _:tag,raw,count=1,flags=re.I)
        else: missing.append(tag)
    if missing and re.search(r'</head\s*>',raw,re.I):
        raw=re.sub(r'</head\s*>',''.join(missing)+'</head>',raw,count=1,flags=re.I)
    return raw


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='_site')
    args=parser.parse_args()
    root=Path.cwd().resolve(); out=Path(args.output).resolve()
    meta=json.loads((root/'pages-metadata.json').read_text(encoding='utf-8'))
    repo=meta['repo']; site_title=meta['title']; site_url=meta['site_url'].rstrip('/')+'/'
    base_path=meta.get('base_path','').rstrip('/'); description=meta['description']; topics=meta['topics']
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    md_sources=[]
    for p in root.rglob('*'):
        if not p.is_file(): continue
        rel=p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts): continue
        if p.suffix.lower() in MD_EXTS: md_sources.append(p.resolve())
    md_sources.sort()
    root_index_md=root/'index.md'; root_readme=root/'README.md'
    if (root/'index.html').exists(): home_source=None
    elif root_index_md.exists(): home_source=root_index_md.resolve()
    elif root_readme.exists(): home_source=root_readme.resolve()
    else: home_source=None
    md_map={p:output_path_for_md(p,root,home_source) for p in md_sources}
    nav=parse_nav(root,md_map,base_path)

    # Copy page assets and authored raw HTML. Exclude source code, hidden/internal files and vendored deps.
    for p in root.rglob('*'):
        if not p.is_file(): continue
        rel=p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts): continue
        if rel.as_posix() in {'pages-metadata.json','requirements-pages.txt','README.md','SUMMARY.md','summary.md'}: continue
        if p.suffix.lower() in MD_EXTS: continue
        if p.name == 'pages-metadata.json' or p.name == 'build_pages.py': continue
        if p.suffix.lower() not in ASSET_EXTS and p.suffix.lower() not in {'.html','.htm'}: continue
        target=out/rel
        target.parent.mkdir(parents=True,exist_ok=True)
        if p.suffix.lower() in {'.html','.htm'}:
            raw=p.read_text(encoding='utf-8',errors='replace')
            page_rel=rel.as_posix()
            canonical=site_url+(page_rel if page_rel!='index.html' else '')
            title=page_title(p,raw,site_title)
            target.write_text(inject_metadata(raw,title,description,canonical,site_title,topics),encoding='utf-8')
        else:
            shutil.copy2(p,target)

    md=markdown.Markdown(extensions=['extra','toc','fenced_code','sane_lists'])
    for source in md_sources:
        rel_out=md_map[source]; target=out/rel_out
        target.parent.mkdir(parents=True,exist_ok=True)
        raw=source.read_text(encoding='utf-8',errors='replace')
        raw=FRONT_MATTER.sub('',raw)
        body=md.convert(raw)
        body=LinkRewriter(body,source,root,rel_out,out,md_map).result()
        title=page_title(source,raw,site_title)
        page_description=plain_text(raw)
        if len(page_description)>155: page_description=page_description[:152].rsplit(' ',1)[0]+'...'
        if not page_description: page_description=description
        canonical=site_url+(rel_out.as_posix() if rel_out.as_posix()!='index.html' else '')
        # Existing authored HTML index remains the homepage and is not overwritten by Markdown.
        if rel_out.as_posix()=='index.html' and (root/'index.html').exists():
            target=out/'README.html'
        target.write_text(template(title,page_description,site_title,canonical,base_path,topics,body,nav,rel_out.as_posix()=='index.html'),encoding='utf-8')

    (out/'assets').mkdir(exist_ok=True)
    css='''*{box-sizing:border-box}body{margin:0;color:#172033;background:#f7f8fb;font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}a{color:#135b8e;text-decoration:none}a:hover{text-decoration:underline}.site-header{display:flex;justify-content:space-between;align-items:center;padding:1rem max(1.2rem,calc((100vw - 1200px)/2));background:#13243a;color:#fff}.site-header a{color:#fff}.brand{font-weight:700;font-size:1.12rem}.shell{max-width:1200px;margin:2rem auto;padding:0 1.2rem;display:grid;grid-template-columns:minmax(210px,270px) minmax(0,1fr);gap:2rem}.toc{align-self:start;position:sticky;top:1rem;max-height:calc(100vh - 2rem);overflow:auto;background:#fff;border:1px solid #e2e7ef;border-radius:12px;padding:1rem}.toc h2{font-size:1rem;margin:.2rem 0 .7rem}.toc ul{list-style:none;padding:0;margin:0}.toc li{margin:.25rem 0 .25rem calc(var(--depth)*.7rem);font-size:.92rem}main{min-width:0;background:#fff;border:1px solid #e2e7ef;border-radius:12px;padding:clamp(1.2rem,3vw,2.6rem);box-shadow:0 8px 30px #18243a0b}article{overflow-wrap:anywhere}h1,h2,h3{line-height:1.25;color:#13243a}h1{font-size:clamp(1.8rem,4vw,2.5rem)}pre{overflow:auto;padding:1rem;border-radius:8px;background:#101a2a;color:#eaf1fa}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}img{max-width:100%;height:auto}table{border-collapse:collapse;display:block;overflow:auto}th,td{padding:.55rem .75rem;border:1px solid #d9e0ea}blockquote{border-left:4px solid #79a8ce;margin-left:0;padding:.2rem 1rem;color:#536276}footer{margin-top:2rem;padding-top:1rem;border-top:1px solid #e3e8ef;color:#657287;font-size:.9rem}@media(max-width:760px){.shell{display:block;margin:1rem auto}.toc{position:static;max-height:none;margin-bottom:1rem}.site-header{padding:1rem}}
'''
    (out/'assets/site.css').write_text(css,encoding='utf-8')
    # Ensure a helpful 404 page for unknown routes.
    (out/'404.html').write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex"><title>Not found | {html.escape(site_title)}</title></head><body><main><h1>Page not found</h1><p><a href="{html.escape(base_path or "/",quote=True)}/">Return to {html.escape(site_title)}</a></p></main></body></html>',encoding='utf-8')
    pages=[]
    for p in out.rglob('*.html'):
        if p.name=='404.html': continue
        rel=p.relative_to(out).as_posix()
        url=site_url+(rel if rel!='index.html' else '')
        pages.append(url)
    pages=sorted(set(pages))
    xml='<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join(f'<url><loc>{html.escape(u)}</loc></url>' for u in pages)+'</urlset>\n'
    (out/'sitemap.xml').write_text(xml,encoding='utf-8')
    (out/'robots.txt').write_text('User-agent: *\nAllow: /\nSitemap: '+site_url+'sitemap.xml\n',encoding='utf-8')
    print(f'Built {len(pages)} HTML pages for {repo} into {out}')

if __name__=='__main__': main()
