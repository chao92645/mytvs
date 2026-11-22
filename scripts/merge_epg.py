#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EPG 自动合并器（纯源头版）

数据源（全部是源头，不经中间镜像）：
  1) kuke31/xmlgz  —— erw.cc 官方免费全量包 all.xml.gz
  2) epg.pw        —— FREE EPG 官方 CN 包

做法：
  · 读 hnyd.m3u，建立「各种频道写法 -> 规范 tvg-id」映射
  · 逐源抓取，只保留 m3u 里真实存在的频道
  · 【时区归一】两源时区标注不同（kuke31 给 +0800，epg.pw 给 +0000），
    统一解析成北京时间再比较、再输出，全部写成 +0800
  · 【时段拼接】主源先占时间段，备源只填主源的空档；
    不会出现「主源命中但内容为空、备源被挡住」的情况
  · 输出时把 <channel id> 与 <programme channel> 统一改写成 tvg-id
    => 播放器按 tvg-id 直查即可命中，不再依赖 display-name 回退匹配
  · 只保留最近 KEEP_DAYS 天（含当天）

运行环境：GitHub Actions (ubuntu) 或本地 Windows，仅需标准库。
环境变量：
  M3U_PATH   播放列表路径，默认 hnyd.m3u
  OUT_PATH   输出路径，默认 epg/merged.xml
  KEEP_DAYS  保留天数，默认 3
  MIN_PROG   节目条数下限，低于此值视为数据源异常、放弃写入，默认 2000
"""
import copy
import gzip
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
CN_TZ = timezone(timedelta(hours=8))

# 源的优先级 = 列表顺序：越靠前，越优先占用时间段
# 每个源给两条通道：直连（Actions 境外）/ gh 加速（本地可用）
SOURCES = [
    ("kuke31", [
        "https://raw.githubusercontent.com/kuke31/xmlgz/main/all.xml.gz",
        "https://gh-proxy.org/https://raw.githubusercontent.com/kuke31/xmlgz/main/all.xml.gz",
    ]),
    ("epg.pw", [
        "https://epg.pw/xmltv/epg_CN.xml",
    ]),
]


def norm(s):
    """归一化：去所有空白 + 转小写，用于严格匹配"""
    return re.sub(r"[\s\u3000]+", "", s or "").lower()


def norm2(s):
    """宽松归一化：额外去掉横杠/下划线，用于兜底匹配"""
    return re.sub(r"[\s\u3000\-_]+", "", s or "").lower()


def maybe_gunzip(data):
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def fetch(urls, label):
    """依次尝试多条通道，每条通道最多 3 次，返回原始 bytes（未解压）"""
    last = None
    for url in urls:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = r.read()
                if len(data) < 10000:
                    raise ValueError(f"内容过短 {len(data)}B")
                print(f"  [{label}] {len(data)/1048576:.2f} MB  <- {url.split('/')[2]}")
                return data
            except Exception as e:
                last = e
                print(f"  [{label}] 失败({attempt+1}): {e}")
                time.sleep(2 + attempt * 2)
    raise RuntimeError(f"{label} 全部通道失败: {last}")


def load_m3u(path):
    """返回 (顺序列表 [(规范id, 键集合, 宽松键集合)], 频道总数)"""
    text = open(path, encoding="utf-8", errors="replace").read()
    order, seen = [], set()
    count = 0
    for line in text.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        count += 1
        gid = re.search(r'tvg-id="([^"]*)"', line)
        gname = re.search(r'tvg-name="([^"]*)"', line)
        gid = gid.group(1).strip() if gid else ""
        gname = gname.group(1).strip() if gname else ""
        disp = line.rsplit(",", 1)[-1].strip()
        canon = gid or gname or disp
        if not canon or canon in seen:
            continue
        seen.add(canon)
        vals = [v for v in (gid, gname, disp) if v]
        order.append((canon, {norm(v) for v in vals}, {norm2(v) for v in vals}))
    return order, count


def parse_sdt(s):
    """把 '20261123000000 +0800' / '20261123000000 +0000' 解析成北京时间 datetime

    无偏移标注的一律按北京时间处理（xmltv 默认使用本地时区）。
    """
    m = re.match(r"(\d{14})\s*([+-]\d{4})?", (s or "").strip())
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    off = m.group(2)
    if off:
        sign = 1 if off[0] == "+" else -1
        tz = timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
    else:
        tz = CN_TZ
    return dt.replace(tzinfo=tz).astimezone(CN_TZ)


def fmt_dt(dt):
    """输出统一写北京时间 +0800"""
    return dt.astimezone(CN_TZ).strftime("%Y%m%d%H%M%S") + " +0800"


def parse_epg(raw):
    """返回 (channels 列表, {cid: [(start_dt, stop_dt, 元素)]})"""
    root = ET.fromstring(raw)
    channels, progs = [], {}
    for ch in root.findall("channel"):
        cid = ch.get("id")
        if not cid:
            continue
        names = [(d.text or "").strip() for d in ch.findall("display-name")]
        channels.append((cid, names))
        progs.setdefault(cid, [])
    for pg in root.findall("programme"):
        cid = pg.get("channel")
        if cid not in progs:
            continue
        s = parse_sdt(pg.get("start"))
        if s is None:
            continue
        e = parse_sdt(pg.get("stop"))
        if e is None or e <= s:
            e = s + timedelta(minutes=30)
        progs[cid].append((s, e, pg))
    return channels, progs


def stitch(items, lo, hi):
    """时段拼接：按源优先级先占位，与已占时段重叠的丢弃

    items: [(start_dt, stop_dt, 元素, 源序号)]
    返回按开始时间排序的 [(start_dt, 元素)]
    """
    live = [x for x in items if x[0] < hi and x[1] > lo]
    live.sort(key=lambda x: (x[3], x[0]))
    taken, out = [], []
    for s, e, pg, _ in live:
        a, b = max(s, lo), min(e, hi)
        if any(a < tb and ta < b for ta, tb in taken):
            continue
        taken.append((a, b))
        out.append((s, pg))
    out.sort(key=lambda x: x[0])
    return out


def main():
    m3u_path = os.environ.get("M3U_PATH", "hnyd.m3u")
    out_path = os.environ.get("OUT_PATH", os.path.join("epg", "merged.xml"))
    keep_days = int(os.environ.get("KEEP_DAYS", "3"))
    min_prog = int(os.environ.get("MIN_PROG", "2000"))

    if not os.path.exists(m3u_path):
        print(f"! 找不到播放列表 {m3u_path}，当前目录: {os.getcwd()}")
        print("  目录内容:", os.listdir(".")[:20])
        return 1

    print("[1/4] 读取播放列表 ...")
    order, total = load_m3u(m3u_path)
    strict, loose = {}, {}
    for canon, k1, k2 in order:
        for k in k1:
            strict.setdefault(k, canon)
        for k in k2:
            loose.setdefault(k, canon)
    print(f"  {m3u_path}: {total} 条频道, {len(order)} 个唯一 tvg-id")

    def lookup(cid, names):
        for v in [cid] + list(names):
            c = strict.get(norm(v))
            if c:
                return c
        for v in [cid] + list(names):
            c = loose.get(norm2(v))
            if c:
                return c
        return None

    print("[2/4] 下载并解析 EPG ...")
    pool = {}          # canon -> [(s, e, 元素, 源序号)]
    for idx, (label, urls) in enumerate(SOURCES):
        try:
            raw = maybe_gunzip(fetch(urls, label))
            channels, progs = parse_epg(raw)
        except Exception as e:
            print(f"  [{label}] 跳过: {e}")
            continue
        got_ch, got_pg = 0, 0
        for cid, names in channels:
            canon = lookup(cid, names)
            if not canon:
                continue
            rows = progs.get(cid) or []
            if not rows:
                continue
            got_ch += 1
            got_pg += len(rows)
            for s, e, pg in rows:
                pool.setdefault(canon, []).append((s, e, pg, idx))
        print(f"  [{label}] 源内 {len(channels)} 频道, 命中 {got_ch} 个, 节目 {got_pg} 条")

    print("[3/4] 拼接时间线 ...")
    now = datetime.now(CN_TZ)
    lo = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hi = lo + timedelta(days=keep_days)

    out_ch, out_pg = [], []
    used = {}          # canon -> 用到的源标签集合
    for canon, _, _ in order:
        items = pool.get(canon)
        if not items:
            continue
        rows = stitch(items, lo, hi)
        if not rows:
            continue
        el = ET.Element("channel", {"id": canon})
        ET.SubElement(el, "display-name", {"lang": "zh"}).text = canon
        out_ch.append(el)
        owner = {id(pg): SOURCES[i][0] for _, _, pg, i in items}
        used[canon] = set()
        for s, pg in rows:
            e = parse_sdt(pg.get("stop")) or (s + timedelta(minutes=30))
            new = ET.Element("programme", {
                "channel": canon,
                "start": fmt_dt(s),
                "stop": fmt_dt(e),
            })
            for child in list(pg):
                new.append(copy.deepcopy(child))
            out_pg.append(new)
            used[canon].add(owner[id(pg)])

    from collections import Counter
    missing = [c for c, _, _ in order if c not in used]
    mix = Counter()
    for v in used.values():
        mix["+".join(sorted(v))] += 1
    print(f"  命中 {len(out_ch)} / {len(order)} 个频道, {len(out_pg)} 条节目")
    print("  频道来源构成: " + ", ".join(f"{k}={v}" for k, v in mix.most_common()))
    if missing:
        print(f"  未覆盖 {len(missing)} 个: {', '.join(missing[:20])}"
              + (" ..." if len(missing) > 20 else ""))

    if len(out_ch) < 10:
        print("! 收录频道过少，疑似数据源异常，放弃写入")
        return 1
    if len(out_pg) < min_prog:
        print(f"! 节目条数过少({len(out_pg)} < {min_prog})，疑似数据源异常，放弃写入")
        return 1

    print("[4/4] 写出 ...")
    tv = ET.Element("tv", {
        "generator-info-name": "merged-epg (kuke31 + epg.pw)",
        "generator-info-url": "https://github.com/chao92645/mytvs",
        "source-info-name": "kuke31/xmlgz(all.xml.gz) + epg.pw(xmltv/epg_CN.xml)",
    })
    for el in out_ch:
        tv.append(el)
    for el in out_pg:
        tv.append(el)
    tree = ET.ElementTree(tv)
    try:
        ET.indent(tree, space=" ")
    except AttributeError:
        pass

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        tree.write(f, encoding="utf-8", xml_declaration=True)
    os.replace(tmp, out_path)

    size = os.path.getsize(out_path)
    print(f"  -> {out_path}  {size/1048576:.2f} MB")
    print(f"  完成: {len(out_ch)} 频道 / {len(out_pg)} 节目")
    return 0


if __name__ == "__main__":
    sys.exit(main())
