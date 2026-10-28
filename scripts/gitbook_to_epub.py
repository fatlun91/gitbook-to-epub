#!/usr/bin/env python3
"""gitbook_to_epub.py — 把 GitBook 站点整本抓取并打包为 EPUB 3（仅用 Python 标准库）。

用法:
    python3 gitbook_to_epub.py <gitbook-root-url> [-o out.epub] [--no-images]
                               [--limit N] [--title TITLE] [--delay 0.2]

抓取策略:
    1. 优先读取 <root>/llms.txt —— 新版 GitBook 提供的完整页面索引，条目按目录顺序排列，
       且每个页面 URL 追加 .md 即可拿到干净的 Markdown 原文。
    2. 若无 llms.txt，退回 <root>/sitemap.xml 收集页面路径，同样按 .md 抓取。
    3. 两者都没有时，仅抓取根页面本身（root.md）。

图片处理:
    默认下载正文中引用的全部图片并内嵌进 EPUB；单张失败时降级为在线链接，不中断整本书。
"""

import argparse
import concurrent.futures
import hashlib
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TIMEOUT = 30
MAX_IMG_BYTES = 15 * 1024 * 1024  # 单图超过 15MB 跳过下载


def log(msg):
    print(f"[gitbook2epub] {msg}", flush=True)


def http_get(url, binary=False, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read()
                if binary:
                    return data, resp.headers, resp.geturl()
                charset = resp.headers.get_content_charset() or "utf-8"
                return data.decode(charset, errors="replace"), resp.headers, resp.geturl()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} 失败: {last}")


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------- 页面发现

def discover_pages(root_url):
    """返回 (pages, book_title)。pages: [(title, md_url)]，按目录顺序。"""
    root = root_url.rstrip("/")
    base = root  # e.g. https://xxx.gitbook.io/space
    # llms.txt
    try:
        text, _, _ = http_get(base + "/llms.txt")
    except Exception:
        text = None
    pages = []
    book_title = None
    if text:
        m = re.search(r"^#\s+(.+)$", text, re.M)
        if m:
            book_title = m.group(1).strip()
        for t, u in re.findall(r"^\s*[-*]\s*\[([^\]]+)\]\((https?://[^)]+)\)", text, re.M):
            pages.append((t.strip(), u.strip()))
        # 去掉无意义的落地页条目（标题就叫 Page 且后面还有其他章节）
        if len(pages) > 1:
            pages = [p for p in pages if p[0].lower() != "page"]
    if pages:
        return pages, book_title

    # sitemap.xml 兜底
    try:
        text, _, _ = http_get(base + "/sitemap.xml")
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
        seen, order = set(), []
        for u in locs:
            u = u.strip().rstrip("/")
            if u in seen:
                continue
            seen.add(u)
            order.append(u)
        if order:
            pages = [(os.path.basename(u.rstrip("/")) or "index", u.rstrip("/") + ".md") for u in order]
            log(f"未找到 llms.txt，改用 sitemap.xml，共 {len(pages)} 页")
            return pages, book_title or re.sub(r"[/]+$", "", base).split("/")[-1].replace("-", " ").title()
    except Exception:
        pass

    log("llms.txt 与 sitemap.xml 均不可用，仅抓取根页面")
    slug = urllib.parse.urlparse(base).path.rstrip("/").split("/")[-1]
    return [(slug or "index", base + ".md")], book_title or slug


# ---------------------------------------------------------------- Markdown 渲染

def _split_row(line):
    line = line.strip().strip("|")
    cells, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
            cur += ch
        elif ch == "|":
            cells.append(cur)
            cur = ""
        else:
            cur += ch
    cells.append(cur)
    return [c.strip() for c in cells]


def render_inline(text, image_registry):
    """行内元素渲染。image_registry: {原始图片URL: EPUB内相对路径或None占位}。
    返回 HTML 片段（引用占位符 {{IMG:n}} 由上层回填）。"""
    codes = []
    images = []
    links = []

    def stash_code(m):
        codes.append(m.group(1))
        return "\x02c%d\x03" % (len(codes) - 1)

    text = re.sub(r"`([^`\n]+)`", stash_code, text)

    def stash_img(m):
        alt, src = m.group(1), m.group(2).split()[0].strip("<>")
        images.append((alt, src))
        return "\x01i%d\x02" % (len(images) - 1)

    def stash_link(m):
        label, href = m.group(1), m.group(2).split()[0].strip("<>")
        links.append((label, href))
        return "\x01l%d\x02" % (len(links) - 1)

    text = re.sub(r"!\[([^\]]*)\]\(\s*([^)\s]+(?:\s+\"[^\"]*\")?\s*)\)", stash_img, text)
    text = re.sub(r"\[([^\]]+)\]\(\s*([^)\s]+(?:\s+\"[^\"]*\")?\s*)\)", stash_link, text)

    out = xml_escape(text)

    # 自动链接（已被转义为 &lt;url&gt;）
    out = re.sub(r"&lt;((?:https?|ftp)://[^&\s]+)&gt;",
                 lambda m: '<a href="%s">%s</a>' % (xml_escape(m.group(1)), xml_escape(m.group(1))), out)
    # 强调
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(r"~~([^~\n]+)~~", r"<s>\1</s>", out)

    def put_img(m):
        idx = int(m.group(1))
        alt, src = images[idx]
        if src in image_registry:
            href = image_registry[src]
            if href:
                return '<img src="%s" alt="%s" class="figure"/>' % (xml_escape(href), xml_escape(alt))
        # 未内嵌：保留为在线链接
        return '<a href="%s">[图片: %s]</a>' % (xml_escape(src), xml_escape(alt) or "image")

    def put_link(m):
        idx = int(m.group(1))
        label, href = links[idx]
        return '<a href="%s">%s</a>' % (xml_escape(href), label)

    out = re.sub("\x01i(\\d+)\x02", put_img, out)
    out = re.sub("\x01l(\\d+)\x02", put_link, out)

    def restore_code(m):
        return "<code>%s</code>" % xml_escape(codes[int(m.group(1))])

    out = re.sub("\x02c(\\d+)\x03", restore_code, out)
    # 去掉转义残留的两空格换行标记
    return out


def render_list(lines, start, image_registry):
    """解析一个列表块，返回 (html, next_index)。支持有序/无序与缩进嵌套。"""
    items = []  # (indent, ordered, marker_len, content_lines)
    i = start
    n = len(lines)
    item_pat = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
    while i < n:
        line = lines[i]
        m = item_pat.match(line)
        if m:
            indent = len(m.group(1).replace("\t", "    "))
            marker = m.group(2)
            ordered = marker[0].isdigit()
            items.append([indent, ordered, [m.group(3)]])
        elif items and (not line.strip() or line.startswith("  ") or line.startswith("\t")):
            items[-1][2].append(line.strip())
            if not line.strip():
                # 空行后如果不再是列表项，列表结束
                nxt = lines[i + 1] if i + 1 < n else ""
                if nxt.strip() and not item_pat.match(nxt):
                    break
        else:
            break
        i += 1

    def build(subset):
        if not subset:
            return ""
        indent = subset[0][0]
        ordered = subset[0][1]
        tag = "ol" if ordered else "ul"
        # 按缩进分组：同级的作为 item，更深的递归
        html_parts = []
        k = 0
        while k < len(subset):
            ind, _o, content = subset[k]
            k += 1
            children = []
            while k < len(subset) and subset[k][0] > ind:
                children.append(subset[k])
                k += 1
            body = render_blocks(content, image_registry)
            if not body.startswith("<p>") and body != "":
                pass
            inner = body if body else ""
            if children:
                inner += build(children)
            html_parts.append("<li>%s</li>" % inner)
        return "<%s>%s</%s>" % (tag, "".join(html_parts), tag)

    # 归一化缩进后构建
    norm = [[it[0], it[1], it[2]] for it in items]
    return build(norm), i


def render_blocks(lines, image_registry):
    out = []
    i = 0
    n = len(lines)
    fence_pat = re.compile(r"^\s*(```|~~~)\s*([\w+#.\-]*)\s*$")
    head_pat = re.compile(r"^(#{1,6})\s+(.*)$")
    hr_pat = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
    item_pat = re.compile(r"^\s*([-*+]|\d{1,9}[.)])\s+")
    table_pat = re.compile(r"^\s*\|.*\|\s*$")
    sep_pat = re.compile(r"^\s*\|?[\s:\-\|]+\|?\s*$")

    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if re.match(r"^\s*<figure\b", line, re.I):
            buf = []
            while i < n:
                buf.append(lines[i])
                i += 1
                if re.search(r"</figure>", lines[i - 1], re.I):
                    break
            out.append(render_figure("\n".join(buf), image_registry))
            continue
        m = fence_pat.match(line)
        if m:
            fence, lang = m.group(1), m.group(2)
            buf = []
            i += 1
            while i < n and not re.match(r"^\s*%s\s*$" % re.escape(fence), lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            code = xml_escape("\n".join(buf))
            cls = ' class="language-%s"' % lang if lang else ""
            out.append("<pre><code%s>%s</code></pre>" % (cls, code))
            continue
        m = head_pat.match(line)
        if m:
            level = min(len(m.group(1)), 6)
            if level == 1:  # 正文里残留的一级标题按二级处理，一级留给章节名
                level = 2
            out.append("<h%d>%s</h%d>" % (level, render_inline(m.group(2).strip(), image_registry), level))
            i += 1
            continue
        if hr_pat.match(line):
            out.append("<hr />")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % render_blocks(buf, image_registry))
            continue
        if table_pat.match(line) and i + 1 < n and sep_pat.match(lines[i + 1]) and "-" in lines[i + 1]:
            header = _split_row(line)
            aligns = [a.strip() for a in _split_row(lines[i + 1])]
            rows = []
            i += 2
            while i < n and table_pat.match(lines[i]):
                rows.append(_split_row(lines[i]))
                i += 1
            def align_css(a):
                if a.startswith(":") and a.endswith(":"):
                    return ' style="text-align:center;"'
                if a.endswith(":"):
                    return ' style="text-align:right;"'
                return ""
            th = "".join("<th%s>%s</th>" % (align_css(aligns[j] if j < len(aligns) else ""),
                                            render_inline(c, image_registry)) for j, c in enumerate(header))
            trs = ""
            for r in rows:
                trs += "<tr>" + "".join("<td%s>%s</td>" % (align_css(aligns[j] if j < len(aligns) else ""),
                                                           render_inline(c, image_registry)) for j, c in enumerate(r)) + "</tr>"
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (th, trs))
            continue
        if item_pat.match(line):
            block, i = render_list(lines, i, image_registry)
            out.append(block)
            continue
        # 普通段落
        buf = []
        while i < n and lines[i].strip() and not fence_pat.match(lines[i]) and not head_pat.match(lines[i]) \
                and not hr_pat.match(lines[i]) and not lines[i].lstrip().startswith(">") \
                and not item_pat.match(lines[i]) and not table_pat.match(lines[i]) \
                and not re.match(r"^\s*<figure\b", lines[i], re.I):
            buf.append(lines[i])
            i += 1
        para = " ".join(x.strip() for x in buf)
        out.append("<p>%s</p>" % render_inline(para, image_registry))
    return "".join(out)


def clean_page_md(text):
    """剥离 GitBook 注入的索引提示块，返回 (title, body_html_lines)。"""
    lines = text.splitlines()
    # 删除文件头部直到第一个一级标题之间的引用/提示
    while lines and not lines[0].strip():
        lines.pop(0)
    first_h1 = None
    for idx, l in enumerate(lines):
        if re.match(r"^#\s+\S", l):
            first_h1 = idx
            break
    title = None
    if first_h1 is not None:
        title = re.sub(r"^#\s+", "", lines[first_h1]).strip()
        lines = lines[first_h1 + 1:]
    else:
        # 没有 H1，删掉开头的 > 引用提示
        while lines and (lines[0].startswith(">") or not lines[0].strip()):
            lines.pop(0)
    return title, lines


# ---------------------------------------------------------------- 图片收集与下载

IMG_MD_PAT = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)")
# GitBook 正文图片常以原始 HTML 形式出现：<figure><img src="..."><figcaption><p>说明</p></figcaption></figure>
IMG_TAG_PAT = re.compile(r"<img\b[^>]*?>", re.I | re.S)
SRC_PAT = re.compile(r"\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
ALT_PAT = re.compile(r"\balt\s*=\s*[\"']([^\"']*)[\"']", re.I)
FIGURE_PAT = re.compile(r"<figure\b[^>]*>.*?</figure>", re.I | re.S)
CAPTION_PAT = re.compile(r"<figcaption\b[^>]*>(.*?)</figcaption>", re.I | re.S)
TAG_STRIP_PAT = re.compile(r"<[^>]+>")


def unescape_entities(s):
    return s.replace("&amp;", "&").replace("&#38;", "&").replace("&quot;", '"').replace("&#34;", '"')


def render_figure(block, image_registry):
    """把 <figure> 块渲染为上下结构：图片在上，figcaption 中的 p 文字在下。"""
    img_html = ""
    tag_m = IMG_TAG_PAT.search(block)
    if tag_m:
        tag = tag_m.group(0)
        src_m = SRC_PAT.search(tag)
        if src_m:
            src = unescape_entities(src_m.group(1)).strip()
            alt_m = ALT_PAT.search(tag)
            alt = unescape_entities(alt_m.group(1)) if alt_m else ""
            href = image_registry.get(src) or src  # 未内嵌则保留在线地址
            img_html = '<img class="figure" src="%s" alt="%s"/>' % (xml_escape(href), xml_escape(alt))
    cap_html = ""
    cap_m = CAPTION_PAT.search(block)
    if cap_m:
        inner = cap_m.group(1)
        paras = re.findall(r"<p\b[^>]*>(.*?)</p>", inner, re.I | re.S) or [inner]
        texts = []
        for p in paras:
            t = " ".join(unescape_entities(TAG_STRIP_PAT.sub(" ", p)).split())
            if t:
                texts.append('<p class="figcap">%s</p>' % xml_escape(t))
        if texts:
            cap_html = '<div class="figcap-wrap">%s</div>' % "".join(texts)
    if not img_html and not cap_html:
        return ""
    return '<div class="figure-block">%s%s</div>' % (img_html, cap_html)


def collect_images(md_text, page_url):
    """返回有序去重的绝对 URL 列表，以及 绝对URL -> 原文写法集合（相对路径等）。
    同时识别 Markdown ![]() 语法与正文 HTML 中的 <img src="">（GitBook figure 写法）。"""
    urls, raw_map = [], {}

    def add(raw):
        raw = unescape_entities(raw).strip().strip("<>")
        if not raw or raw.startswith("data:"):
            return
        absu = urllib.parse.urljoin(page_url, raw)
        raw_map.setdefault(absu, set()).add(raw)
        if absu not in urls:
            urls.append(absu)

    for m in IMG_MD_PAT.finditer(md_text):
        add(m.group(2))
    for m in IMG_TAG_PAT.finditer(md_text):
        src_m = SRC_PAT.search(m.group(0))
        if src_m:
            add(src_m.group(1))
    return urls, raw_map


def sniff_ext(data, content_type):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "gif" in ct:
        return ".gif"
    if "svg" in ct:
        return ".svg"
    if "webp" in ct:
        return ".webp"
    return None


def download_images(urls, delay):
    """返回 {url: (filename, media_type) 或 None}"""
    result = {}

    def fetch(u):
        try:
            data, headers, _ = http_get(u, binary=True, retries=1)
            if len(data) > MAX_IMG_BYTES:
                log(f"图片过大跳过: {u[:90]}")
                return u, None
            ext = sniff_ext(data, headers.get("Content-Type"))
            if not ext:
                guess = os.path.splitext(urllib.parse.urlparse(u).path)[1].lower()
                ext = guess if guess in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp") else None
            if not ext:
                log(f"无法识别图片类型，跳过: {u[:90]}")
                return u, None
            if ext == ".jpeg":
                ext = ".jpg"
            h = hashlib.md5(u.encode()).hexdigest()[:12]
            media = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif",
                     ".svg": "image/svg+xml", ".webp": "image/webp"}[ext]
            return u, (f"img-{h}{ext}", media, data)
        except Exception as e:  # noqa: BLE001
            log(f"图片下载失败（保留为链接）: {u[:90]} — {e}")
            return u, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for u, r in ex.map(fetch, urls):
            result[u] = r
            time.sleep(delay * 0.1)
    return result


# ---------------------------------------------------------------- 封面生成

FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]

COVER_PALETTES = [
    ((38, 66, 128), (12, 22, 54)),    # 深蓝
    ((110, 42, 60), (36, 12, 22)),    # 酒红
    ((28, 100, 88), (8, 32, 28)),     # 墨绿
    ((96, 70, 140), (30, 20, 52)),    # 紫罗兰
    ((150, 92, 30), (48, 28, 8)),     # 棕金
    ((52, 96, 130), (14, 30, 46)),    # 灰蓝
]


def _find_font(size, bold=True):
    from PIL import ImageFont
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 9.2
    except Exception:
        return ImageFont.load_default()


def _wrap_text(draw, text, font, max_w):
    """中英混合换行：英文按词、中文按字。返回行列表。"""
    tokens, buf = [], ""
    for ch in text:
        if ord(ch) < 128:
            if ch in " \t":
                if buf:
                    tokens.append(buf)
                    buf = ""
                tokens.append(" ")
            else:
                buf += ch
        else:
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
    if buf:
        tokens.append(buf)

    lines, cur = [], ""
    for tok in tokens:
        trial = cur + tok
        if draw.textlength(trial, font=font) <= max_w or not cur.strip():
            cur = trial
        else:
            lines.append(cur.rstrip())
            cur = tok.lstrip() if tok == " " else tok
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def make_cover_bytes(title, subtitle=None):
    """生成 EPUB 封面 PNG 字节。PIL 不可用时返回 None（降级为纯文字标题页）。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        log("未安装 PIL，跳过封面图生成（使用文字标题页）")
        return None
    W, H = 1200, 1600
    c1, c2 = COVER_PALETTES[int(hashlib.md5(title.encode()).hexdigest(), 16) % len(COVER_PALETTES)]
    from PIL import Image
    grad = Image.new("RGB", (1, H))
    gpx = grad.load()
    for y in range(H):
        t = y / (H - 1)
        gpx[0, y] = (int(c1[0] + (c2[0] - c1[0]) * t),
                     int(c1[1] + (c2[1] - c1[1]) * t),
                     int(c1[2] + (c2[2] - c1[2]) * t))
    img = grad.resize((W, H))
    draw = ImageDraw.Draw(img)

    # 标题字号自适应：从大到小，保证不超过 7 行
    max_w = W - 200
    size = 112
    while size >= 40:
        font = _find_font(size)
        lines = _wrap_text(draw, title, font, max_w)
        if len(lines) <= 7:
            break
        size -= 8
    line_h = int(size * 1.35)
    total_h = line_h * len(lines)
    y = (H - total_h) // 2 - 60
    for ln in lines:
        w = draw.textlength(ln, font=font)
        draw.text(((W - w) / 2, y), ln, font=font, fill=(250, 250, 248))
        y += line_h

    # 底部信息条
    foot_font = _find_font(34)
    foot = subtitle or "GitBook"
    fw = draw.textlength(foot, font=foot_font)
    draw.text(((W - fw) / 2, H - 170), foot, font=foot_font, fill=(228, 228, 228))
    bar_w = min(int(fw * 1.4), W - 300)
    draw.rectangle([(W - bar_w) / 2, H - 190, (W + bar_w) / 2, H - 186], fill=(250, 250, 248))

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- EPUB 组装

XHTML_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
{body}
</body>
</html>
"""

STYLE_CSS = """@charset "utf-8";
body { font-family: serif; line-height: 1.7; margin: 1em; }
h1 { font-size: 1.6em; }
h2 { font-size: 1.4em; }
h3 { font-size: 1.2em; }
p { margin: 0.7em 0; text-align: justify; }
pre { background: #f5f5f5; border: 1px solid #ddd; border-radius: 4px; padding: 0.7em;
      overflow-wrap: anywhere; white-space: pre-wrap; font-family: monospace; font-size: 0.85em; }
code { font-family: monospace; background: #f5f5f5; }
pre code { background: none; }
blockquote { margin: 0.8em 0; padding-left: 1em; border-left: 3px solid #ccc; color: #444; }
img.figure { max-width: 100%; height: auto; display: block; margin: 0.8em auto; }
.figure-block { margin: 1.2em auto; text-align: center; }
.figure-block img.figure { display: block; margin: 0 auto; }
.figcap-wrap { margin-top: 0.4em; }
p.figcap { color: #555; font-size: 0.85em; text-align: center; margin: 0.2em 0 0; }
table { border-collapse: collapse; margin: 0.8em 0; width: 100%; }
th, td { border: 1px solid #bbb; padding: 0.35em 0.6em; vertical-align: top; }
th { background: #eee; }
hr { border: none; border-top: 1px solid #ccc; margin: 1.2em 0; }
ol, ul { margin: 0.5em 0; padding-left: 1.6em; }
nav ol { list-style: none; }
.cover-page { margin: 0; padding: 0; }
img.cover { width: 100%; height: auto; display: block; margin: 0; }
"""


def build_epub(path, title, lang, chapters, images, source_url, cover_png=None):
    """chapters: [(title, body_html)]；images: {orig_url: (fname, media, data)}；
    cover_png: 自动生成的封面 PNG 字节（None 时封面页退化为纯文字标题页）。"""
    container_xml = ('<?xml version="1.0"?>\n'
                     '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                     '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
                     '</container>\n')

    book_id = "urn:uuid:" + str(uuid.uuid4())
    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="toc"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
        '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine = ['<itemref idref="cover" linear="no"/>']
    cover_meta = ""

    # 封面（有封面图：整页图片；无：纯文字标题页）
    if cover_png:
        manifest_items.append('<item id="coverimg" href="cover-image.png" media-type="image/png" properties="cover-image"/>')
        cover_meta = '<meta content="coverimg" name="cover"/>'
        cover_body = ('<div class="cover-page"><img class="cover" src="cover-image.png" alt="%s"/></div>'
                      % xml_escape(title))
    else:
        cover_body = ('<div style="text-align:center;margin-top:35%;">'
                      '<h1>{title}</h1><p style="color:#666;">GitBook · 离线版</p></div>').format(title=xml_escape(title))
    cover_xhtml = XHTML_TMPL.format(lang=xml_escape(lang), title=xml_escape(title), body=cover_body)

    img_map = {}
    for u, (fname, media, _d) in images.items():
        manifest_items.append('<item id="img%d" href="%s" media-type="%s"/>'
                              % (len(img_map) + 1, fname, media))
        img_map[u] = "images/" + fname

    for idx, (ctitle, _body) in enumerate(chapters, 1):
        manifest_items.append('<item id="ch%d" href="chapter-%03d.xhtml" media-type="application/xhtml+xml"/>'
                              % (idx, idx))
        spine.append('<itemref idref="ch%d"/>' % idx)

    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="%s">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="bookid">%s</dc:identifier>
  <dc:title>%s</dc:title>
  <dc:creator>%s</dc:creator>
  <dc:language>%s</dc:language>
  <dc:source>%s</dc:source>
  %s
  <meta property="dcterms:modified">%s</meta>
</metadata>
<manifest>
%s
</manifest>
<spine toc="ncx">
%s
</spine>
</package>
""" % (xml_escape(lang), xml_escape(book_id), xml_escape(title), xml_escape("GitBook"),
       xml_escape(lang), xml_escape(source_url), cover_meta, "2026-01-01T00:00:00Z",
       "\n".join(manifest_items), "\n".join(spine))

    # nav.xhtml
    nav_lis = "".join('<li><a href="chapter-%03d.xhtml">%s</a></li>' % (i, xml_escape(t))
                      for i, (t, _b) in enumerate(chapters, 1))
    nav_xhtml = XHTML_TMPL.format(lang=xml_escape(lang), title="目录",
                                  body='<h1>目录</h1><nav epub:type="toc"><ol>%s</ol></nav>' % nav_lis)

    # toc.ncx
    ncx_points = []
    for i, (t, _b) in enumerate(chapters, 1):
        ncx_points.append(
            '<navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
            '<content src="chapter-%03d.xhtml"/></navPoint>' % (i, i, xml_escape(t), i))
    ncx = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="%s"/><meta name="dtb:depth" content="1"/></head>
<docTitle><text>%s</text></docTitle>
<navMap>%s</navMap>
</ncx>
""" % (xml_escape(book_id), xml_escape(title), "".join(ncx_points))

    # 修正 opf：补上 source（站点地址）与 cover meta
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container_xml)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav_xhtml)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/style.css", STYLE_CSS)
        z.writestr("OEBPS/cover.xhtml", cover_xhtml)
        if cover_png:
            z.writestr("OEBPS/cover-image.png", cover_png)
        for idx, (ctitle, body) in enumerate(chapters, 1):
            doc = XHTML_TMPL.format(lang=xml_escape(lang), title=xml_escape(ctitle),
                                    body="<h1>%s</h1>\n%s" % (xml_escape(ctitle), body))
            z.writestr("OEBPS/chapter-%03d.xhtml" % idx, doc)
        for u, (fname, _media, data) in images.items():
            z.writestr("OEBPS/images/" + fname, data)
    return path


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="GitBook 站点整本抓取并打包为 EPUB")
    ap.add_argument("url", help="GitBook 站点根地址，如 https://xxx.gitbook.io/space")
    ap.add_argument("-o", "--out", help="输出 EPUB 路径（默认 <slug>.epub）")
    ap.add_argument("--no-images", action="store_true", help="不下载图片，保留在线链接")
    ap.add_argument("--limit", type=int, help="仅抓取前 N 章（调试用）")
    ap.add_argument("--title", help="覆盖书名")
    ap.add_argument("--delay", type=float, default=0.15, help="每页抓取间隔秒数")
    args = ap.parse_args()

    root = args.url.rstrip("/")
    parsed = urllib.parse.urlparse(root)
    if not parsed.scheme:
        root = "https://" + root

    log(f"发现页面索引: {root}")
    pages, auto_title = discover_pages(root)
    if args.limit:
        pages = pages[: args.limit]
    log(f"共 {len(pages)} 章")

    slug = parsed.path.rstrip("/").split("/")[-1] or "gitbook"
    out_path = args.out or os.path.abspath(slug + ".epub")

    # 1. 并发抓取全部 Markdown 页面
    def fetch_md(item):
        title, url = item
        try:
            text, _, final_url = http_get(url, retries=2)
            return title, final_url, text, None
        except Exception as e:  # noqa: BLE001
            return title, url, None, e

    log("抓取 Markdown 页面…")
    raw = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        for r in ex.map(fetch_md, pages):
            raw.append(r)
            time.sleep(args.delay * 0.05)

    chapters_src = []  # (title, md_text, page_url)
    failed = 0
    for page_title, url, text, err in raw:
        if text is None:
            failed += 1
            log(f"页面抓取失败: {url} — {err}")
            continue
        chapters_src.append((page_title, text, url))

    # 2. 清洗 + 收集图片
    all_img_urls, seen, raw_map_all = [], set(), {}
    for _pt, text, url in chapters_src:
        us, rmap = collect_images(text, url)
        for u in us:
            if u not in seen:
                seen.add(u)
                all_img_urls.append(u)
        for u, raws in rmap.items():
            raw_map_all.setdefault(u, set()).update(raws)

    img_registry = {}
    images = {}
    if all_img_urls and not args.no_images:
        log(f"下载 {len(all_img_urls)} 张图片…")
        dl = download_images(all_img_urls, args.delay)
        for u, r in dl.items():
            if r:
                fname, media, data = r
                images[u] = (fname, media, data)
                # 原文里可能写成绝对 URL 或相对路径，两种写法都登记
                for key in set([u]) | raw_map_all.get(u, set()):
                    img_registry[key] = "images/" + fname
        log(f"成功内嵌 {len(images)} 张，其余 {len(all_img_urls) - len(images)} 张保留为在线链接")

    # 3. 渲染
    log("渲染章节…")
    chapters = []
    for page_title, text, url in chapters_src:
        text = text.replace("\r\n", "\n")
        h1, body_lines = clean_page_md(text)
        title = h1 or page_title
        body = render_blocks(body_lines, img_registry)
        chapters.append((title, body))

    if not chapters:
        log("错误：没有任何可用页面，未生成 EPUB")
        sys.exit(2)

    # 语言猜测
    sample = "".join(b for _t, b in chapters[:3])
    lang = "zh-CN" if re.search(r"[\u4e00-\u9fff]", sample) else "en"
    book_title = args.title or auto_title or slug

    # 4. 封面 + 打包
    cover_png = make_cover_bytes(book_title, subtitle="GitBook 离线版")
    if cover_png:
        log(f"封面已生成（{len(cover_png)/1024:.0f} KB）")
    log("打包 EPUB…")
    build_epub(out_path, book_title, lang, chapters, images, root, cover_png=cover_png)
    size = os.path.getsize(out_path)
    log(f"完成：{out_path}（{len(chapters)} 章，{len(images)} 图，{size/1024/1024:.1f} MB）")
    if failed:
        log(f"注意：有 {failed} 页抓取失败，已跳过")


if __name__ == "__main__":
    main()
