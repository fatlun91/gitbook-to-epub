# gitbook-to-epub

> 把 GitBook 文档站整本抓取并转换为 EPUB 电子书，正文图片自动内嵌，自动生成书籍封面。
> Turn any GitBook site into a complete EPUB book — inline images, auto-generated cover, zero heavy dependencies.

[English](#english) | [中文](#中文)

---

## 中文

### 这是什么

一个 [千问工作助理](https://tongyi.aliyun.com) / Claude 可复用 **Agent Skill**，也可作为**独立命令行工具**使用。

给定一个 GitBook 站点根地址（例如 `https://warrenluquant.gitbook.io/zuo-er-ting-feng`），它会：

1. 读取站点 `llms.txt` 章节索引（自动降级到 `sitemap.xml`）
2. 逐页抓取干净的 Markdown 原文，剥离 GitBook 注入的提示块
3. 下载正文全部图片并内嵌进 EPUB（同时支持 `![]()` 与 HTML `<figure><img src>` 两种写法）
4. 自动生成书籍封面（渐变色底 + 书名排版，注册为 EPUB 标准封面）
5. 打包为 EPUB 3：封面页 + 目录导航（`nav.xhtml` 与 `toc.ncx` 双格式）+ 章节文件

### 为什么需要它

- 官方 GitBook 导出功能常需付费套餐，且不支持 `.gitbook.io` 公共空间。
- 想要**离线阅读整本技术文档**，或导入 Kindle / Apple Books / Kobo / Calibre。
- 零重型依赖：不需要 pandoc、Calibre、ebooklib，标准库直接跑。

### 环境要求

| 依赖 | 必需 | 说明 |
|------|------|------|
| Python 3.8+ | ✅ | 仅标准库，无需 `pip install` |
| Pillow | ⭕ 可选 | 用于生成封面图；未安装时自动降级为纯文字标题页 |

```bash
# 可选：安装 Pillow 以获得封面图
python3 -m pip install pillow
```

### 快速开始

```bash
# 1. 整本转换（图片自动内嵌 + 自动生成封面）
python3 scripts/gitbook_to_epub.py "https://warrenluquant.gitbook.io/zuo-er-ting-feng" -o 左耳听风.epub

# 2. 只想快速预览，抓前 5 章
python3 scripts/gitbook_to_epub.py "<站点URL>" --limit 5 -o preview.epub

# 3. 不下载图片（产物更小，图片保留在线链接）
python3 scripts/gitbook_to_epub.py "<站点URL>" --no-images -o lite.epub

# 4. 自定义书名与抓取节流
python3 scripts/gitbook_to_epub.py "<站点URL>" --title "我的书名" --delay 0.3 -o book.epub
```

### 命令行参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `url` | 必填 | GitBook 站点根地址，如 `https://xxx.gitbook.io/space` |
| `-o, --out` | `<slug>.epub` | 输出 EPUB 路径 |
| `--title` | 自动取站点标题 | 覆盖 EPUB 元数据书名 |
| `--limit N` | 全部 | 只抓前 N 章，调试或节选用 |
| `--no-images` | 关闭 | 不下载图片，保留在线链接 |
| `--delay` | `0.15` | 每页抓取间隔（秒），站点限流时调大 |

### 支持的 Markdown / HTML 元素

标题、粗体 / 斜体 / 删除线、行内代码、代码围栏（保留语言标记）、引用块、有序与无序及嵌套列表、表格（含对齐）、自动链接、分隔线。

图片的两种写法都能内嵌：

```markdown
![说明](https://xxx.gitbook.io/space/image.png)

<figure><img src="https://.../photo.webp"><figcaption><p>陈皓，人称耗子叔</p></figcaption></figure>
```

`<figure>` 渲染为**上下结构**：图片在上、`figcaption` 中 `<p>` 的说明文字在下（居中灰色小字）。

### 输出结构

```
book.epub
├── mimetype                      # 首个条目，未压缩（EPUB 规范要求）
├── META-INF/container.xml
└── OEBPS/
    ├── content.opf               # 含 cover meta 与 cover-image 属性
    ├── cover-image.png           # 自动生成的封面
    ├── cover.xhtml               # 整页封面
    ├── nav.xhtml                 # EPUB 3 目录
    ├── toc.ncx                   # 兼容老阅读器的目录
    ├── style.css
    ├── chapter-001.xhtml ... 
    └── images/img-*.webp|png|jpg # 内嵌的正文图片
```

### 常见问题

**Q：生成的 EPUB 里没有图片？**
先确认源站 `.md` 导出里到底有没有图片引用（`![]()` 和 `<img>` 两种都要查）。脚本已同时支持两种写法；若源站确实无图，产物自然无图。

**Q：`llms.txt 与 sitemap.xml 均不可用，仅抓取根页面`？**
说明该站点是旧版 GitBook 或未开放索引。此时只会拿到 1 章，请检查 URL 是否为**空间根路径**（带 slug 的那一层）。

**Q：图片下载失败会中断整本书吗？**
不会。失败的图片自动降级为在线链接，日志里会逐条列出。单张超过 15 MB 的图片会被跳过。

**Q：能转需要登录的私有文档站吗？**
不能，本工具只处理公开可访问的 GitBook 站点。

**Q：抓取被封 / 超时？**
调大 `--delay`（如 `0.5`）降低并发压力，或用 `--limit` 分批抓取。

**Q：如何验证产物是否规范？**
用 Calibre 打开，或检查 zip 首个条目为未压缩的 `mimetype`：

```bash
unzip -l book.epub | head -5
```

### 合规提醒

请仅转换**你有权复制**的内容：自己维护的文档、开源许可证允许的技术书籍、或作者明确允许离线保存的资料。转换并分发受版权保护的书籍可能构成侵权，责任由使用者自行承担。

---

## English

### What it is

A reusable **Agent Skill** (Qianwen Work Assistant / Claude), that also works perfectly as a **standalone CLI tool**.

Given a GitBook site root URL (e.g. `https://warrenluquant.gitbook.io/zuo-er-ting-feng`), it:

1. Reads the site's `llms.txt` chapter index (falls back to `sitemap.xml`)
2. Fetches clean Markdown for every page, stripping GitBook's injected banner blocks
3. Downloads and **inlines every image** into the EPUB — supporting both `![]()` syntax and HTML `<figure><img src>` markup
4. **Auto-generates a book cover** (gradient background + typographic title, registered as the EPUB cover image)
5. Packages everything as EPUB 3: cover page + TOC (`nav.xhtml` and `toc.ncx`) + chapter files

### Why bother

- Official GitBook export usually requires a paid plan and doesn't support public `.gitbook.io` spaces.
- You want to **read a whole technical book offline** on Kindle / Apple Books / Kobo / Calibre.
- No heavy dependencies: pandoc, Calibre and ebooklib are all unnecessary — standard library only.

### Requirements

| Dependency | Required | Notes |
|---|---|---|
| Python 3.8+ | ✅ | Standard library only, no `pip install` needed |
| Pillow | ⭕ Optional | Enables cover-image generation; otherwise falls back to a text title page |

### Quick start

```bash
# Full book: images inlined, cover generated
python3 scripts/gitbook_to_epub.py "https://warrenluquant.gitbook.io/zuo-er-ting-feng" -o my-book.epub

# Quick preview: first 5 chapters
python3 scripts/gitbook_to_epub.py "<URL>" --limit 5 -o preview.epub

# Skip images (smaller output, links stay remote)
python3 scripts/gitbook_to_epub.py "<URL>" --no-images -o lite.epub
```

### CLI options

| Option | Default | Description |
|---|---|---|
| `url` | required | GitBook site root, e.g. `https://xxx.gitbook.io/space` |
| `-o, --out` | `<slug>.epub` | Output path |
| `--title` | from site | Override the EPUB title metadata |
| `--limit N` | all | Only fetch the first N chapters |
| `--no-images` | off | Don't download images; keep remote links |
| `--delay` | `0.15` | Polite delay between page fetches (seconds) |

### Figure rendering

GitBook often stores body images as raw HTML rather than Markdown. This tool parses `<figure>` blocks and renders them as a vertical layout: image on top, the `<p>` text inside `<figcaption>` underneath as a centered, muted caption.

### Troubleshooting

- **"llms.txt 与 sitemap.xml 均不可用"** — the site is legacy GitBook or has no index; you'll only get 1 chapter. Make sure the URL includes the space slug.
- **No images in output** — verify the source `.md` actually references images. Both `![]()` and `<img>` are handled.
- **A few images failed** — the run still completes; failures degrade to remote links and are logged one by one. Images over 15 MB are skipped.
- **Private / login-required docs** — not supported; public sites only.

### Compliance

Only convert content you have the right to copy: your own documentation, openly licensed technical books, or material the author permits you to archive. Redistributing copyrighted books may infringe law, at your own responsibility.

## License

MIT
