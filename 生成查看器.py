#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把一个装满 GPX / FIT 的文件夹，打包成「双击就能看」的单文件 HTML。

用法：
    python 生成查看器.py "D:/我的轨迹"                    # 输出到同目录 轨迹查看器.html
    python 生成查看器.py "D:/我的轨迹" -o "D:/我的轨迹/总览.html"
    python 生成查看器.py "D:/我的轨迹" --simplify 2        # 每 2 米抽一个点，减小体积
    python 生成查看器.py "D:/我的轨迹" --range 2025-03-01:2025-03-31
    python 生成查看器.py "D:/我的轨迹" -t gpx-timeline.html # 用 Dawarich 风格的时间线界面

生成的 HTML 里已经把轨迹数据内联进去，不依赖本地文件，
可以拷到手机浏览器打开（底图瓦片需要联网）。
"""
import argparse
import json
import math
import os
import re
import struct
import sys
from datetime import datetime, timezone
from functools import lru_cache
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "gpx-viewer.html")
MARK = '<script id="preset" type="application/json">null</script>'


def inline_vendor(html):
    """把 Leaflet 的 js/css 和解析内核内联进 HTML，脱离网络也能打开。
    如果模板本身已经是内联版（build.py 的产物），直接返回。"""
    css_path = os.path.join(HERE, "vendor", "leaflet.css")
    js_path = os.path.join(HERE, "vendor", "leaflet.js")
    core_path = os.path.join(HERE, "src", "core.js")
    if 'data-vendor="leaflet' not in html and 'data-vendor="core' not in html:
        return html, 2
    ok = 0
    if os.path.exists(css_path):
        with open(css_path, "r", encoding="utf-8") as f:
            css = f.read()
        html, n = re.subn(r'<link[^>]*data-vendor="leaflet\.css"[^>]*>',
                          lambda m: "<style>" + css + "</style>", html, count=1)
        ok += n
    if os.path.exists(js_path):
        with open(js_path, "r", encoding="utf-8") as f:
            js = f.read()
        html, n = re.subn(r'<script[^>]*data-vendor="leaflet\.js"[^>]*>\s*</script>',
                          lambda m: "<script>" + js + "</script>", html, count=1)
        ok += n
    if os.path.exists(core_path):
        with open(core_path, "r", encoding="utf-8") as f:
            core = f.read()
        html, n = re.subn(r'<script[^>]*data-vendor="core\.js"[^>]*>\s*</script>',
                          lambda m: "<script>" + core + "</script>", html, count=1)
        ok += n
    return html, ok


def local(tag):
    return tag.rsplit("}", 1)[-1]


def haversine(a, b):
    r = 6371000.0
    rad = math.pi / 180.0
    dlat = (b[0] - a[0]) * rad
    dlon = (b[1] - a[1]) * rad
    s = (math.sin(dlat / 2) ** 2
         + math.cos(a[0] * rad) * math.cos(b[0] * rad) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(s))


def date_from_name(name):
    """从文件名里认日期：2025-03-01 / 2025_3_1 / 20250301 / 2025年3月1日"""
    import re
    base = os.path.basename(name)
    m = re.search(r"(20\d{2})[-_.\s年](\d{1,2})[-_.\s月](\d{1,2})", base)
    if not m:
        m = re.search(r"(20\d{2})(\d{2})(\d{2})", base)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def thin(points, tol):
    """按最小间距抽稀，保留首尾点"""
    if tol <= 0 or len(points) <= 2:
        return points
    out = [points[0]]
    for p in points[1:-1]:
        if haversine(out[-1], p) >= tol:
            out.append(p)
    out.append(points[-1])
    return out


def day_key(ts):
    """把 trkpt 里的 UTC 时间换成浏览者本地时区的日期"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone()
    # 设备没定位就写 1970 的那种时间，不当日期用
    if dt.year < 2000 or dt.year > datetime.now().year + 1:
        return None
    return dt.strftime("%Y-%m-%d")


def hhmm(ts):
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%H:%M")


def split_by_day(pts):
    """按每个点的时间，把一条 segment 切成若干天。
    没时间的点跟随相邻点所在的日期；新的一段带上上一段最后一个点，线不会断。"""
    keys = [day_key(p[2]) if p[2] else None for p in pts]
    for i in range(1, len(keys)):
        if not keys[i]:
            keys[i] = keys[i - 1]
    for i in range(len(keys) - 2, -1, -1):
        if not keys[i]:
            keys[i] = keys[i + 1]
    out = []
    cur = None
    for i, p in enumerate(pts):
        if cur is None or cur["key"] != keys[i]:
            cur = {
                "key": keys[i],
                "pts": [(p[0], p[1])],
                "lead": (pts[i - 1][0], pts[i - 1][1]) if i > 0 else None,
                "times": [p[2]] if p[2] else [],
            }
            out.append(cur)
        else:
            cur["pts"].append((p[0], p[1]))
            if p[2]:
                cur["times"].append(p[2])
    return out


def pieces_from_raw(raw, path, tol):
    """把 [(轨迹名, [(lat, lon, time), ...]), ...] 切成按天分段的 pieces。
       GPX 与 FIT 共用这一段。"""
    if not raw:
        return []
    key_file = date_from_name(path)
    base = os.path.splitext(os.path.basename(path))[0]

    buckets = {}      # 日期 -> {segs, times, name}
    for tn, pts in raw:
        for part in split_by_day(pts):
            k = part["key"] or ""
            b = buckets.setdefault(k, {"segs": [], "times": [], "name": ""})
            full = ([part["lead"]] + part["pts"]) if part["lead"] else part["pts"]
            b["segs"].append(
                {"pts": full, "skip": 1 if part["lead"] else 0, "n": len(part["pts"])}
            )
            b["times"].extend(part["times"])
            if not b["name"] and tn:
                b["name"] = tn

    pieces = []
    for k, b in buckets.items():
        dist = 0.0
        npts = 0
        out_segs = []
        for sg in b["segs"]:
            pp = sg["pts"]
            npts += sg["n"]
            for i in range(1, len(pp)):
                dist += haversine(pp[i - 1], pp[i])
            th = thin(pp, tol)
            out_segs.append([[round(x[0], 6), round(x[1], 6)] for x in th])
        ts = sorted(b["times"])
        pieces.append({
            "keyTime": k or None,
            "keyFile": key_file,
            "file": os.path.basename(path),
            "name": b["name"] or base,
            "dist": round(dist, 1),
            "pts": npts,
            "start": hhmm(ts[0]) if ts else "",
            "end": hhmm(ts[-1]) if ts else "",
            "t0": ts[0] if ts else "",
            "segs": out_segs,
        })

    pieces.sort(key=lambda x: x["t0"] or "~")
    first_day = pieces[0]["keyTime"] if pieces else None
    for i, it in enumerate(pieces):
        it["part"] = i + 1
        it["parts"] = len(pieces)
        it["keyStart"] = first_day
    return pieces


def parse_gpx(path, tol):
    """GPX：直接遍历 trk / trkseg / rte，不再对每个子元素递归解析。"""
    try:
        root = ET.parse(path).getroot()
    except Exception as e:
        print(f"  ! 跳过（解析失败）：{os.path.basename(path)} - {e}")
        return []

    def points(node, tag):
        out = []
        for p in node:
            if local(p.tag) != tag:
                continue
            try:
                lat = float(p.get("lat")); lon = float(p.get("lon"))
            except (TypeError, ValueError):
                continue
            ts = next((c.text.strip() for c in p if local(c.tag) == "time" and c.text), None)
            out.append((lat, lon, ts))
        return out

    raw = []
    for trk in root.iter():
        t = local(trk.tag)
        if t not in ("trk", "rte"):
            continue
        nm = next((c.text.strip() for c in trk if local(c.tag) == "name" and c.text), "")
        if t == "rte":
            pts = points(trk, "rtept")
            if pts:
                raw.append((nm, pts))
        else:
            for seg in trk.iter():
                if local(seg.tag) != "trkseg":
                    continue
                pts = points(seg, "trkpt")
                if pts:
                    raw.append((nm, pts))
    if not raw:
        # 兜底：有些 GPX 把 trkpt 直接挂在 trk 下，没有 trkseg
        pts = []
        for p in root.iter():
            if local(p.tag) != "trkpt":
                continue
            try:
                lat = float(p.get("lat")); lon = float(p.get("lon"))
            except (TypeError, ValueError):
                continue
            ts = next((c.text.strip() for c in p if local(c.tag) == "time" and c.text), None)
            pts.append((lat, lon, ts))
        if pts:
            raw.append(("", pts))
    return pieces_from_raw(raw, path, tol)


# ---------- FIT（Garmin 二进制） ----------
FIT_EPOCH = 631065600          # 1989-12-31T00:00:00Z，FIT 时间戳纪元（秒）
FIT_SEMI = 180.0 / (2 ** 31)   # semicircle -> degree


@lru_cache(maxsize=200000)
def fit_iso(ts):
    try:
        return datetime.fromtimestamp(FIT_EPOCH + ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def read_fit_field(data, p, size, typ, le):
    """按 FIT base type 读一个字段；只关心数值类型，其它跳过即可。
       Zepp / Amazfit 等会把 0x80 标志并进 base type（0x85=sint32 / 0x86=uint32），先剥掉。"""
    e = "<" if le else ">"
    t = typ & 0x7F
    try:
        if t in (0x02, 0x0A, 0x00, 0x0D) and size >= 1:
            return data[p]
        if t == 0x01 and size >= 1:
            return struct.unpack_from("b", data, p)[0]
        if t == 0x03 and size >= 2:
            return struct.unpack_from(e + "h", data, p)[0]
        if t in (0x04, 0x0B) and size >= 2:
            return struct.unpack_from(e + "H", data, p)[0]
        if t == 0x05 and size >= 4:
            return struct.unpack_from(e + "i", data, p)[0]
        if t in (0x06, 0x0C) and size >= 4:
            return struct.unpack_from(e + "I", data, p)[0]
        if t == 0x08 and size >= 4:
            return struct.unpack_from(e + "f", data, p)[0]
        if t == 0x09 and size >= 8:
            return struct.unpack_from(e + "d", data, p)[0]
    except (struct.error, IndexError):
        pass
    return 0


def parse_fit(path, tol):
    """FIT：顺序解析定义/数据消息，只取 record(20) 的时间戳和经纬度。
       支持压缩时间戳头、大小端、开发者字段；长时间中断会切成新段。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        print(f"  ! 跳过（读取失败）：{os.path.basename(path)} - {e}")
        return []
    if len(data) < 14 or data[0] not in (12, 14):
        return []
    hs = data[0]
    data_size = struct.unpack_from("<I", data, 4)[0]
    pos, end = hs, min(len(data), hs + data_size)
    defs, last_ts, pts = {}, -1, []

    def valid(lat, lon):
        return (lat is not None and lon is not None
                and lat != 0x7FFFFFFF and lon != 0x7FFFFFFF and (lat or lon))

    while pos < end:
        h = data[pos]; pos += 1
        if h & 0x80:
            # 压缩时间戳头：local id 是 2 位，低 5 位是秒偏移，数据里没有 timestamp 字段
            lid, off = (h >> 5) & 3, h & 31
            ts = ((last_ts & ~31) | off) if last_ts >= 0 else off
            if last_ts >= 0 and ts <= last_ts:
                ts += 32
            last_ts = ts
            d = defs.get(lid)
            if not d:
                break
            p, lat, lon = pos, None, None
            for num, size, typ in d["fields"]:
                if num == 253:
                    continue
                v = read_fit_field(data, p, size, typ, d["le"])
                if num == 0: lat = v
                elif num == 1: lon = v
                p += size
            pos = p + d["dev"]
            if d["global"] == 20 and valid(lat, lon):
                pts.append((lat * FIT_SEMI, lon * FIT_SEMI, fit_iso(ts)))
        elif h & 0x40:
            lid = h & 15
            pos += 1                                  # reserved
            le = (data[pos] == 0); pos += 1           # architecture
            gmsg = struct.unpack_from("<H" if le else ">H", data, pos)[0]; pos += 2
            nf = data[pos]; pos += 1
            fields = []
            for _ in range(nf):
                fields.append((data[pos], data[pos + 1], data[pos + 2])); pos += 3
            dev = 0
            if h & 0x20:
                nd = data[pos]; pos += 1
                for _ in range(nd):
                    dev += data[pos + 1]; pos += 3
            defs[lid] = {"global": gmsg, "le": le, "fields": fields, "dev": dev}
        else:
            lid = h & 15
            d = defs.get(lid)
            if not d:
                break
            p, ts, lat, lon = pos, None, None, None
            for num, size, typ in d["fields"]:
                v = read_fit_field(data, p, size, typ, d["le"])
                if num == 253: ts = v
                elif num == 0: lat = v
                elif num == 1: lon = v
                p += size
            pos = p + d["dev"]
            if ts is not None and ts != 0xFFFFFFFF:
                last_ts = ts
            else:
                ts = last_ts
            if d["global"] == 20 and valid(lat, lon):
                pts.append((lat * FIT_SEMI, lon * FIT_SEMI, fit_iso(ts) if ts >= 0 else None))

    if not pts:
        return []
    raw, cur = [], [pts[0]]
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        gap = False
        if a[2] and b[2]:
            try:
                da = datetime.fromisoformat(a[2].replace("Z", "+00:00"))
                db = datetime.fromisoformat(b[2].replace("Z", "+00:00"))
                gap = (db - da).total_seconds() > 1800
            except ValueError:
                pass
        if not gap and haversine(a, b) > 5000:
            gap = True
        if gap:
            raw.append(("", cur)); cur = []
        cur.append(b)
    raw.append(("", cur))
    return pieces_from_raw(raw, path, tol)


def pack_segs(segs):
    """[[lat,lon],...] -> 扁平的 ×1e5 整数差值数组，体积约为原来的一半。"""
    out = []
    plat = plon = 0
    for lat, lon in segs:
        a, b = round(lat * 1e5), round(lon * 1e5)
        out.append(a - plat)
        out.append(b - plon)
        plat, plon = a, b
    return out


def walk_files(folder, recursive):
    files = []
    if recursive:
        for dirpath, _, names in os.walk(folder):
            for n in names:
                if n.lower().endswith((".gpx", ".fit")):
                    files.append(os.path.join(dirpath, n))
    else:
        for n in sorted(os.listdir(folder)):
            p = os.path.join(folder, n)
            if os.path.isfile(p) and n.lower().endswith((".gpx", ".fit")):
                files.append(p)
    return sorted(files)


def in_range(key, rng):
    if not rng:
        return True
    lo, hi = rng
    return lo <= key <= hi


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="把 GPX 文件夹打包成单文件 HTML 查看器")
    ap.add_argument("folder", help="GPX 文件所在文件夹")
    ap.add_argument("-o", "--out", default=None, help="输出 HTML 路径")
    ap.add_argument("-r", "--recursive", action="store_true", help="递归子文件夹")
    ap.add_argument("--simplify", type=float, default=1.5, help="抽稀间距（米），0 表示不抽稀")
    ap.add_argument("--range", default=None, help="只收这个日期段，如 2025-03-01:2025-03-31")
    ap.add_argument("-t", "--template", default=None,
                    help="模板 HTML，默认 gpx-viewer.html；可填 gpx-timeline.html 用时间线界面")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        sys.exit(f"找不到文件夹：{folder}")

    files = walk_files(folder, args.recursive)
    if not files:
        sys.exit(f"{folder} 里没有 .gpx 或 .fit 文件")

    rng = None
    if args.range:
        parts = args.range.split(":")
        lo = parts[0] or "0000-00-00"
        hi = parts[1] if len(parts) > 1 and parts[1] else "9999-99-99"
        rng = (lo, hi)

    n_fit = sum(1 for f in files if f.lower().endswith(".fit"))
    print(f"扫描 {folder}")
    print(f"发现 {len(files)} 个轨迹文件（GPX {len(files)-n_fit} / FIT {n_fit}），开始解析…")

    trips = []
    split_files = 0
    for i, f in enumerate(files, 1):
        if f.lower().endswith(".fit"):
            pieces = parse_fit(f, args.simplify)
        else:
            pieces = parse_gpx(f, args.simplify)
        if len(pieces) > 1:
            split_files += 1
        for pc in pieces:
            # 分组依据：轨迹里的时间 > 文件名日期 > 文件修改日期
            if not pc["keyFile"]:
                pc["keyFile"] = None
            key = pc["keyTime"] or pc["keyFile"]
            if not key:
                key = datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d")
                pc["keyFile"] = key
            if in_range(key, rng):
                trips.append(pc)
        if i % 50 == 0:
            print(f"  ... {i}/{len(files)}")

    if not trips:
        sys.exit("没有解析到任何轨迹点（文件里可能只有航点，或日期被 --range 全过滤掉了）")

    def trip_key(t):
        return t["keyTime"] or t["keyFile"] or "未标注日期"

    days = {}
    for t in trips:
        days.setdefault(trip_key(t), []).append(t)

    is_date = lambda k: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", k))
    ordered = sorted([k for k in days if is_date(k)], reverse=True) + \
              sorted([k for k in days if not is_date(k)])

    day_list = []
    for k in ordered:
        items = sorted(days[k], key=lambda x: x["start"] or "")
        day_list.append({"key": k, "items": items})

    # 写进 HTML 前把坐标打包成 ×1e5 的整数差值，体积约减半、解析更快
    for d in day_list:
        for it in d["items"]:
            it["segs"] = [pack_segs(s) for s in it["segs"]]

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "root": folder,
        "days": day_list,
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    tpl_path = os.path.abspath(args.template) if args.template else TEMPLATE
    if not os.path.isfile(tpl_path):
        sys.exit(f"找不到模板文件：{tpl_path}")
    is_timeline = "timeline" in os.path.basename(tpl_path).lower()

    with open(tpl_path, "r", encoding="utf-8") as f:
        html = f.read()
    if MARK not in html:
        sys.exit("模板文件已损坏（找不到数据占位符），请重新运行 python build.py")
    html = html.replace(MARK, '<script id="preset" type="application/json">' + data + "</script>")
    html, inlined = inline_vendor(html)
    if inlined < 2:
        print("  ! 提示：vendor/ 里的 Leaflet 没找到，生成的页面需要联网加载地图库")

    out = args.out or os.path.join(folder, "轨迹时间线.html" if is_timeline else "轨迹查看器.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)

    n_pts = sum(t["pts"] for t in trips)
    n_km = sum(t["dist"] for t in trips) / 1000
    print("-" * 48)
    print(f"天数：{len(day_list)}   轨迹：{len(trips)} 段   原始点：{n_pts:,}   总里程：{n_km:,.1f} km")
    if split_files:
        print(f"其中 {split_files} 个文件跨了零点，已按日期自动拆成多段")
    if day_list:
        print(f"日期范围：{day_list[-1]['key']} ~ {day_list[0]['key']}")
    print(f"输出：{out}  ({os.path.getsize(out)/1024/1024:.2f} MB)")
    print("界面：" + ("时间线版（Dawarich 风格）" if is_timeline else "经典版（日期列表 + 勾选框）"))
    print("双击用浏览器打开即可；拷到手机浏览器也能看。")


if __name__ == "__main__":
    main()
