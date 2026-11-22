#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 src/*.html + vendor/leaflet.* 合成自包含的查看器。
生成的页面不联网也能打开（只有地图瓦片要网），可以直接拷到手机。

    python build.py                 # 构建全部模板（默认做安全压缩）
    python build.py timeline        # 只构建 src/timeline.html
    python build.py --raw           # 不压缩，方便对照调试

模板 → 产物：
    src/template.html  →  gpx-viewer.html     经典版（日期列表 + 勾选框）
    src/timeline.html  →  gpx-timeline.html   Dawarich 风格（时间范围 / 日历 / 统计 / 图层）

压缩只做「删注释、删空行、去行首行尾空白」，不合并行、不改变量名，
因此不会碰到字符串、正则或 HTML 片段；源码始终保留在 src/ 里便于阅读。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TPLS = [
    ("src/template.html", "gpx-viewer.html"),
    ("src/timeline.html", "gpx-timeline.html"),
]


def minify_css(s):
    """删注释 / 空行 / 行首尾空白，不动行内内容（避免破坏 url()、content 等）。"""
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def minify_js(s):
    """按行安全压缩：删空行、整行 // 注释、整行 /* */ 注释，去行首尾空白。
       不合并行，字符串和正则里的内容完全不变。"""
    out = []
    for ln in s.splitlines():
        t = ln.strip()
        if not t:
            continue
        if t.startswith("//"):
            continue
        if t.startswith("/*") and t.endswith("*/") and "/*" not in t[2:-2]:
            continue
        out.append(t)
    return "\n".join(out)


def minify_inline(html):
    """压缩内联的 <style> 与 <script>，跳过 JSON 数据占位符。"""
    html = re.sub(
        r"(<style[^>]*>)(.*?)(</style>)",
        lambda m: m.group(1) + minify_css(m.group(2)) + m.group(3),
        html, flags=re.S)
    def js_sub(m):
        if "application/json" in m.group(1):
            return m.group(0)
        return m.group(1) + minify_js(m.group(2)) + m.group(3)
    html = re.sub(r"(<script[^>]*>)(.*?)(</script>)", js_sub, html, flags=re.S)
    return html


def build(rel_tpl, rel_out, do_minify=True):
    tpl = os.path.join(HERE, rel_tpl)
    out = os.path.join(HERE, rel_out)
    with open(tpl, "r", encoding="utf-8") as f:
        html = f.read()
    with open(os.path.join(HERE, "vendor", "leaflet.css"), "r", encoding="utf-8") as f:
        css = f.read()
    with open(os.path.join(HERE, "vendor", "leaflet.js"), "r", encoding="utf-8") as f:
        js = f.read()
    with open(os.path.join(HERE, "src", "core.js"), "r", encoding="utf-8") as f:
        core = f.read()

    html, n1 = re.subn(r'<link[^>]*data-vendor="leaflet\.css"[^>]*>',
                       lambda m: "<style>" + css + "</style>", html, count=1)
    html, n2 = re.subn(r'<script[^>]*data-vendor="leaflet\.js"[^>]*>\s*</script>',
                       lambda m: "<script>" + js + "</script>", html, count=1)
    if n1 + n2 != 2:
        sys.exit("模板里找不到 Leaflet 占位标记，检查 " + rel_tpl)
    html, n3 = re.subn(r'<script[^>]*data-vendor="core\.js"[^>]*>\s*</script>',
                       lambda m: "<script>" + core + "</script>", html, count=1)
    if n3 != 1:
        sys.exit("模板里找不到解析内核占位标记，检查 " + rel_tpl)

    before = len(html)
    if do_minify:
        html = minify_inline(html)

    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    tag = "压缩后" if do_minify else "未压缩"
    print(f"输出：{rel_out}  ({os.path.getsize(out)/1024:.0f} KB，{tag}，"
          f"内联前 {before/1024:.0f} KB，Leaflet/内核已内联)")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_minify = "--raw" not in flags
    arg = args[0] if args else ""
    targets = [t for t in TPLS if not arg or arg in t[0] or arg in t[1]]
    if not targets:
        sys.exit("找不到模板：" + arg + "\n可选：" + ", ".join(t[0] for t in TPLS))
    for tpl, out in targets:
        build(tpl, out, do_minify)


if __name__ == "__main__":
    main()
