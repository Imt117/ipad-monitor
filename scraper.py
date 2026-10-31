#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端版抓取脚本（GitHub Actions 用）。
与本地版逻辑一致，但：
  - cookie 从环境变量 JD_COOKIE 读取（由 GitHub Secret 注入）。
  - 结果写入 data.json（供前端 index.html 读取）。
全站扫描，锁定自营非全球购二手 iPad Pro M5 11英寸 256G，并展开验机实例。
"""
import re
import json
import time
import os
import urllib.request
import urllib.parse
import concurrent.futures as cf

KEYWORDS = [
    "iPad Pro M5", "iPad Pro M5 二手", "iPad Pro M5 9成新", "iPad Pro M5 95成新",
    "iPad Pro M5 99成新", "iPad Pro M5 资源机", "iPad Pro 11 M5", "ipadpro m5",
    "iPad Pro M5 256G", "iPad Pro M5 512G", "iPad Pro M5 蜂窝", "iPad Pro M5 eSIM",
]
MAX_PAGES = 8
SEARCH_SLEEP = 0.05
MAX_WORKERS = 20

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

COOKIE = os.environ.get("JD_COOKIE", "")


def get(url, timeout=12):
    headers = {"User-Agent": UA}
    if COOKIE:
        headers["Cookie"] = COOKIE
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


def extract_search_items(html):
    items = []
    if not html:
        return items
    for m in re.finditer(r'skuid="(\d+)"', html):
        sku = m.group(1)
        seg = html[m.start():m.start() + 900]
        vid = re.search(r'vid="([^"]*)"', seg)
        items.append({"sku": sku, "venderId": vid.group(1) if vid else None})
    return items


USED = ["二手", "资源机", "准新", "备件", "成新", "新机", "未使用", "原装", "拆封", "官翻", "翻新", "回收", "品质"]


def is_target(title):
    t = title
    tl = title.lower()
    if "ipad pro" not in tl or "m5" not in tl:
        return False
    if "13英寸" in t or "13寸" in t:
        return False
    if "11英寸" not in t and "11寸" not in t:
        return False
    if "512" in t or "1t" in tl or "2t" in tl or "128" in t or "64g" in tl:
        return False
    if "256" not in t:
        return False
    if not any(k in t for k in USED):
        return False
    return True


SELF_NAME = ["自营", "官方旗舰店", "官方"]
EXCLUDE_NAME = ["海外", "跨境", "免税", "买手", "专营", "全球购", "国际", "international"]


def is_self_shop(shop_name):
    if not shop_name:
        return False
    has_self = any(k in shop_name for k in SELF_NAME)
    has_ex = any(k in shop_name for k in EXCLUDE_NAME)
    return has_self and not has_ex


def extract_detail(html):
    out = {}
    m = re.search(r'<title>([^<]*)</title>', html)
    out["title"] = re.sub(r'\s+', ' ', m.group(1)).strip() if m else ""
    m = re.search(r'"jdPrice":"([\d.]+)"', html)
    out["jdPrice"] = float(m.group(1)) if m else None
    m = re.search(r'"salePrice":"([\d.]+)"', html)
    out["salePrice"] = float(m.group(1)) if m else None
    m = re.search(r'"StockState":(\d+)', html)
    out["stockState"] = int(m.group(1)) if m else None
    m = re.search(r'"isGlobalPurchase":(true|false)', html)
    out["isGlobalPurchase"] = (m.group(1) == "true") if m else None
    m = re.search(r'"shopName":"([^"]+)"', html)
    out["shopName"] = m.group(1) if m else None
    return out


STOCK_MAP = {33: "有货", 34: "预订", 40: "无货", 39: "无货", 0: "未知"}


def classify_condition(title):
    cond = []
    if "准新机" in title:
        cond.append("准新机")
    if "全新" in title or "未激活" in title or "未使用" in title or "原装未使用" in title:
        cond.append("全新/未激活")
    if "资源机" in title:
        cond.append("资源机")
    if "99成新" in title or "99新" in title:
        cond.append("99成新")
    elif "95成新" in title or "95新" in title:
        cond.append("95成新")
    elif "9成新" in title or "九成" in title or "9.5成新" in title:
        cond.append("9成新")
    elif "8成新" in title or "8成" in title:
        cond.append("8成新")
    elif "7成新" in title or "7成" in title:
        cond.append("7成新")
    elif "5成新" in title or "6成新" in title or "5成" in title or "6成" in title:
        cond.append("5-6成新")
    if not cond and "二手" in title:
        cond.append("二手")
    return cond if cond else ["未知"]


def extract_inspect_list(html):
    cnt_m = re.search(r'"selectableCount":(\d+)', html)
    count = int(cnt_m.group(1)) if cnt_m else 0
    inspects = []
    for m in re.finditer(r'"inspectSkuId":(\d+)[^{]*?"price":"([\d.]+)"', html):
        inspects.append({"inspectSkuId": m.group(1), "price": float(m.group(2))})
    # 去重（同一 inspectSkuId 可能重复出现）
    dedup = {}
    for x in inspects:
        dedup[x["inspectSkuId"]] = x["price"]
    inspects = [{"inspectSkuId": k, "price": v} for k, v in dedup.items()]
    return {"selectableCount": len(inspects) if inspects else count, "inspects": inspects}


def process_one(pair):
    sku, meta = pair
    durl = "https://item.m.jd.com/product/%s.html" % sku
    html = get(durl)
    if not html:
        return None
    d = extract_detail(html)
    title = d["title"]
    if not is_target(title):
        return None
    if d.get("isGlobalPurchase") is True:
        return None
    if not is_self_shop(d.get("shopName")):
        return None
    insp = extract_inspect_list(html)
    return {
        "sku": sku, "title": title,
        "price": (d["jdPrice"] if d["jdPrice"] is not None else None),
        "condition": classify_condition(title), "link": durl,
        "venderId": meta["venderId"], "shopName": d.get("shopName"),
        "jdPrice": d["jdPrice"], "salePrice": d["salePrice"],
        "stockText": STOCK_MAP.get(d["stockState"], "未知"),
        "selectableCount": insp["selectableCount"], "inspects": insp["inspects"],
    }


def extract_brother_skus(html):
    out = []
    for m in re.finditer(r'size":"([^"]+)"[^}]*?"skuId":"(\d+)"', html, re.S):
        size = m.group(1)
        sku = m.group(2)
        if "256" not in size:
            continue
        start = max(0, m.start() - 800)
        block = html[start:m.end()]
        if re.search(r'"color":"[^"]*11英寸[^"]*"', block):
            out.append(sku)
    return list(dict.fromkeys(out))


def main():
    seen = {}
    for kw in KEYWORDS:
        for page in range(1, MAX_PAGES + 1):
            url = "https://so.m.jd.com/ware/search.action?keyword=" + urllib.parse.quote(kw) + "&page=%d" % page
            html = get(url)
            if not html:
                continue
            for it in extract_search_items(html):
                if it["sku"] not in seen:
                    seen[it["sku"]] = it
            time.sleep(SEARCH_SLEEP)

    def collect(bro):
        sku, meta = bro
        html = get("https://item.m.jd.com/product/%s.html" % sku)
        return extract_brother_skus(html) if html else []

    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for bros in ex.map(collect, list(seen.items())):
            for b in bros:
                if b not in seen:
                    seen[b] = {"sku": b, "venderId": None}

    results = []
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for r in ex.map(process_one, list(seen.items())):
            if r:
                results.append(r)

    dedup = {}
    for r in results:
        dedup[r["sku"]] = r
    results = list(dedup.values())

    def lowest(it):
        ps = [x["price"] for x in (it.get("inspects") or []) if x.get("price") is not None]
        if ps:
            return min(ps)
        return it.get("price") if it.get("price") is not None else it.get("jdPrice")
    results.sort(key=lambda x: (lowest(x) if lowest(x) is not None else 0))

    snapshot = {
        "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(results),
        "items": results,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print("done, items:", len(results))


if __name__ == "__main__":
    main()