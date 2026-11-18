#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EPG 自动合并器
以 51zmt 为主源，补上 fanmingming 独有频道，只输出 m3u 里实际存在的频道。

运行环境：GitHub Actions (ubuntu) 或本地 Windows，仅需标准库。
用法:
    python scripts/merge_epg.py
环境变量:
    M3U_PATH   播放列表路径，默认 hnyd.m3u
    OUT_PATH   输出路径，默认 epg/merged.xml
    KEEP_DAYS  保留天数，默认 3
"""
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
CN_TZ = timezone(timedelta(hours=8))

# 每个源给两条通道：GitHub 原生地址（Actions 内网最快），失败回退 gh 加速（本地可用）
SOURCES = {
    "51zmt": [
        "https://raw.githubusercontent.com/taksssss/tv/main/epg/51zmt.xml",
        "https://gh-proxy.org/https://raw.githubusercontent.com/taksssss/tv/main/epg/51zmt.xml",
    ],
    "fanmingming": [
        "https://raw.githubusercontent.com/fanmingming/live/main/e.xml",
        "https://gh-proxy.org/https://raw.githubusercontent.com/fanmingming/live/main/e.xml",
    ],
}


def fetch(urls, label):
    """依次尝试多条通道，返回 bytes"""
    last = None
    for url in urls:
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read()
                if len(data) < 10000:
                    raise ValueError(f"内容过短 {len(data)}B")
                print(f"  [{label}] {len(data)/1048576:.2f} MB  <- {url.split('/')[2]}")
                return data
            except Exception as e:
                last = e
                print(f"  [{label}] 失败({attempt+1}): {e}")
                time.sleep(2)
    raise RuntimeError(f"{label} 全部通道失败: {last}")


def load_wanted(path):
    """读取 m3u，返回所有频道标识的小写集合（tvg-id / tvg-name / 显示名）"""
    text = open(path, encoding="utf-8", errors="replace").read()
    wanted = set()
    count = 0
    for line in text.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        count += 1
        for pat in (r'tvg-id="([^"]*)"', r'tvg-name="([^"]*)"'):
            m = re.search(pat, line)
            if m and m.group(1).strip():
                wanted.add(m.group(1).strip().lower())
        disp = line.rsplit(",", 1)[-1].strip()
        if disp:
            wanted.add(disp.lower())
    return wanted, count


def parse_epg(raw):
    root = ET.fromstring(raw)
    channels, progs = [], {}
    for ch in root.findall("channel"):
        cid = ch.get("id")
        if not cid:
            continue
        names = [(d.text or "").strip() for d in ch.findall("display-name")]
        channels.append((cid, names, ch))
        progs[cid] = []
    for pg in root.findall("programme"):
        cid = pg.get("channel")
        if cid in progs:
            progs[cid].append(pg)
    return channels, progs


def main():
    m3u_path = os.environ.get("M3U_PATH", "hnyd.m3u")
    out_path = os.environ.get("OUT_PATH", os.path.join("epg", "merged.xml"))
    keep_days = int(os.environ.get("KEEP_DAYS", "3"))

    if not os.path.exists(m3u_path):
        print(f"! 找不到播放列表 {m3u_path}，当前目录: {os.getcwd()}")
        print("  目录内容:", os.listdir(".")[:20])
        return 1

    print("[1/4] 读取播放列表 ...")
    wanted, total = load_wanted(m3u_path)
    print(f"  {m3u_path}: {total} 条频道, {len(wanted)} 个唯一标识")

    print("[2/4] 下载 EPG ...")
    prim_ch, prim_pg = parse_epg(fetch(SOURCES["51zmt"], "51zmt"))
    extra_ch, extra_pg = parse_epg(fetch(SOURCES["fanmingming"], "fanmingming"))
    print(f"  51zmt {len(prim_ch)} 频道 / fanmingming {len(extra_ch)} 频道")

    print("[3/4] 筛选合并 ...")
    out_ch, out_pg = [], []
    seen = set()
    take = set()

    def hit(cid, names):
        return any(v in wanted for v in [cid.lower()] + [n.lower() for n in names] if v)

    for cid, names, el in prim_ch:
        if hit(cid, names):
            out_ch.append(el)
            out_pg.extend(prim_pg.get(cid, []))
            take.add(cid)
            seen.add(cid.lower())
            seen.update(n.lower() for n in names)
    n_prim = len(out_ch)

    added = []
    for cid, names, el in extra_ch:
        keys = {cid.lower()} | {n.lower() for n in names}
        if keys & seen or cid in take or not hit(cid, names):
            continue
        out_ch.append(el)
        out_pg.extend(extra_pg.get(cid, []))
        take.add(cid)
        seen |= keys
        added.append(names[0] if names else cid)
    print(f"  主源 {n_prim} 个, 补充 {len(added)} 个: {', '.join(added) if added else '无'}")

    now = datetime.now(CN_TZ)
    lo = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hi = lo + timedelta(days=keep_days)
    lo_s = lo.strftime("%Y%m%d%H%M%S") + " +0800"
    hi_s = hi.strftime("%Y%m%d%H%M%S") + " +0800"
    kept = [p for p in out_pg if lo_s <= (p.get("start") or "") < hi_s]
    print(f"  节目 {len(out_pg)} → {len(kept)} 条（保留 {keep_days} 天）")

    if len(out_ch) < 10:
        print("! 收录频道过少，疑似数据源异常，放弃写入")
        return 1

    print("[4/4] 写出 ...")
    tv = ET.Element("tv", {
        "generator-info-name": "merged-epg (51zmt + fanmingming)",
        "generator-info-url": "https://github.com/chao92645/mytvs",
    })
    for el in out_ch:
        tv.append(el)
    for el in kept:
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
    print(f"  完成: {len(out_ch)} 频道 / {len(kept)} 节目")
    return 0


if __name__ == "__main__":
    sys.exit(main())

