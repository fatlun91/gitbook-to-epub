---
name: gitbook-to-epub
description: 把 GitBook 站点（*.gitbook.io 空间或自定义域名的 GitBook 文档站）整本抓取并转换为 EPUB 电子书，正文图片自动下载内嵌，自动生成书籍封面，含封面、目录导航与章节文件，核心链路零第三方依赖（封面图在有 PIL 时启用，否则自动降级）。当用户要求"把某个 gitbook 站点/文档转成 epub、离线保存某本 gitbook 电子书"时使用。
---

# GitBook → EPUB 转换

把用户给定的 GitBook 站点根地址整本抓取，生成一本可离线阅读的 EPUB 3 电子书。

## 何时触发

- 用户给出 `xxx.gitbook.io/空间名` 形式的地址，要求转成 / 保存为 EPUB 电子书。
- 用户要求把某个 GitBook 文档站离线化、打包成书。

## 用法

脚本位于本技能目录 `scripts/gitbook_to_epub.py`，核心链路仅依赖 Python 3 标准库（无需 pandoc / Calibre / ebooklib）；封面图生成需要 Pillow，未安装时自动降级为纯文字标题页。

```bash
python3 <skill_dir>/scripts/gitbook_to_epub.py "<站点根URL>" -o "<输出>.epub"
```

常用参数：
- `-o out.epub`：输出路径，用用户可感知的书名命名（默认取站点 slug）。
- `--no-images`：不下载图片、保留在线链接（默认会内嵌全部图片）。
- `--limit N`：只抓前 N 章，调试或用户指定部分章节时用。
- `--title 书名`：覆盖 EPUB 元数据中的书名（默认取 llms.txt 的 `#` 标题）。
- `--delay 秒`：抓取节流间隔，默认 0.15。

## 工作原理（排障时参考）

1. 读站点根目录 `llms.txt` 获取按目录顺序排列的章节索引；每个页面 URL 追加 `.md` 可得干净 Markdown 原文。
2. 无 llms.txt 时降级读 `sitemap.xml`；再失败则只抓根页面（并明确告知用户只拿到 1 章）。
3. 自动剥离 GitBook 在每个 .md 页头注入的索引提示引用块。
4. 下载正文引用的全部图片（按字节魔术头识别 png/jpg/gif/webp/svg），打进 EPUB 的 `OEBPS/images/` 并改写 `img` 引用为包内相对路径；下载失败或超过 15MB 的图降级为在线链接，不中断整本书。
5. 内置 Markdown 渲染器支持：标题、粗斜体、行内代码、代码围栏、引用块、有序/无序及嵌套列表、表格（含对齐）、自动链接、分隔线。
6. GitBook 正文图片多以原始 HTML `<figure><img src="..."><figcaption><p>说明</p></figcaption></figure>` 出现（**不是** `![]()` 语法）：脚本会解析 figure 块，提取 img 的 src 下载内嵌，渲染为上下结构——图片在上、figcaption 中 p 的说明文字在下（居中、灰色小字）；figcaption 若无 p 标签，取其纯文本作说明。
7. 自动生成书籍封面：按书名哈希从 6 套渐变色方案选色，1200x1600 PNG，书名自动换行居中、底部信息条；优先用系统中文字体（PingFang/微软雅黑/文泉驿等）。封面注册为 EPUB 标准封面（OPF cover-image 属性 + 整页封面页），阅读器书架缩略图即用它。PIL 未安装时自动降级为纯文字标题页，不报错。
8. 打包为 EPUB 3（mimetype 首文件不压缩存储 + OPF/nav.xhtml/toc.ncx 双目录），中文内容自动标注 zh-CN。

## 执行要点

- 本技能需要访问公网抓取站点，Bash 调用需申请网络权限。
- 整本书页数多时脚本内部已并发+节流，直接跑完即可；不要中途重复抓取。
- 完成后检查脚本日志中的章节数与图片数，向用户报告；产物用 SendUserFile 交付。
- 若日志出现"页面抓取失败，已跳过"，如实转告用户缺了哪几章，不要静默。

## 已知边界

- 若某站点正文确实不含任何图片引用（`![]()` 与 `<img>` 都没有），EPUB 自然无图；先确认两种格式都查过再下结论，不要误判为"源站图片丢失"。
- 需要登录才能访问的私有 GitBook 站点不支持。
- 图片为外链引用的（非本站上传）同样会尝试下载内嵌；防盗链失败则保留为在线链接。

## 验证过的样例

- https://warrenluquant.gitbook.io/zuo-er-ting-feng （40 章；5 张 figure 图片全部成功下载内嵌，figcaption 说明文字渲染在图片下方）
- 合成含图页面自测：图片成功内嵌进 OEBPS/images/ 且 XML 全部可解析。
