#!/usr/bin/env python3
"""Signal: collects the newest videos of a few YouTube channels (public RSS feeds)
and writes videos.json. Channel IDs are found automatically from the channel names
(or you can force one with "id": "UC..." in channels.json)."""
import json, os, re, sys, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
COOKIE = "CONSENT=YES+1; SOCS=CAI"
NOW = datetime.now(timezone.utc)
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "m": "http://search.yahoo.com/mrss/"}
ID_RX = re.compile(r"UC[0-9A-Za-z_-]{22}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": COOKIE, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def status_noredirect(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": COOKIE})
    try:
        return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout).status
    except urllib.error.HTTPError as e:
        return e.code


def load(name, default):
    try:
        return json.load(open(os.path.join(HERE, name), encoding="utf-8"))
    except Exception:
        return default


def resolve_id(ch):
    """Return (channel_id, guessed)."""
    if ch.get("id"):
        return ch["id"], False
    handle = ch.get("handle") or ch["name"]
    try:
        html = get("https://www.youtube.com/@" + handle)
        for rx in (r'<meta itemprop="(?:identifier|channelId)" content="(UC[0-9A-Za-z_-]{22})"',
                   r'"externalId":"(UC[0-9A-Za-z_-]{22})"',
                   r'rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"',
                   r'"channelId":"(UC[0-9A-Za-z_-]{22})"'):
            m = re.search(rx, html)
            if m:
                return m.group(1), False
    except Exception as e:
        print("  handle lookup failed for", handle, e, file=sys.stderr)
    try:  # fallback: first channel in YouTube search results (to double-check in the app)
        html = get("https://www.youtube.com/results?search_query=" + urllib.parse.quote(ch["name"]) + "&sp=EgIQAg%253D%253D")
        m = re.search(r'"channelId":"(UC[0-9A-Za-z_-]{22})"', html)
        if m:
            return m.group(1), True
    except Exception as e:
        print("  search lookup failed for", ch["name"], e, file=sys.stderr)
    return None, False


def parse_feed(xml):
    root = ET.fromstring(xml)
    title = (root.findtext("a:title", "", NS) or "").strip()
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", "", NS)
        pub = e.findtext("a:published", "", NS)
        if not vid or not pub:
            continue
        stats = e.find("m:group/m:community/m:statistics", NS)
        out.append({"id": vid, "title": (e.findtext("a:title", "", NS) or "").strip(),
                    "published": datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(),
                    "views": int(stats.get("views")) if stats is not None and (stats.get("views") or "").isdigit() else None})
    return title, out


def is_short(vid, title, state):
    if "#short" in title.lower():
        return True
    if vid in state["short"]:
        return state["short"][vid]
    code = status_noredirect("https://www.youtube.com/shorts/" + vid)
    if code in (200, 303, 302, 301):
        state["short"][vid] = (code == 200)
        return code == 200
    return False  # unknown: do not hide


def pick_rules(ch, entries, state):
    """Keep only the newest video matching each rule (remembered between runs)."""
    out = []
    for rule in ch["rules"]:
        key = ch["name"] + "|" + rule["label"]
        best = state["pick"].get(key)
        for v in entries:
            t = v["title"].lower()
            if re.search(rule["match"], t) and not (rule.get("exclude") and re.search(rule["exclude"], t)):
                if not best or v["published"] > best["published"]:
                    best = dict(v)
                break  # entries are newest-first
        if best:
            state["pick"][key] = best
            out.append({**best, "tag": rule["label"]})
    return out


def process(args):
    ch, cfg, state = args
    name = ch["name"]
    info = {"name": name, "ok": False}
    try:
        hkey = name + "|" + (ch.get("handle") or name)
        cid = ch.get("id") or state["ids"].get(hkey)
        guessed = state.get("guessed", {}).get(hkey, False)
        if not cid:
            cid, guessed = resolve_id(ch)
        if not cid:
            raise RuntimeError("channel not found")
        title, entries = parse_feed(get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid))
        state["ids"][hkey] = cid
        state.setdefault("guessed", {})[hkey] = guessed
        info.update(ok=True, id=cid, found=title, guessed=guessed)
        cutoff = (NOW - timedelta(days=cfg.get("max_age_days", 21))).isoformat()
        entries.sort(key=lambda v: v["published"], reverse=True)
        if ch.get("rules"):
            vids = pick_rules(dict(ch, name=name + "|" + cid), entries, state)
        else:
            vids = []
            for v in entries:
                if v["published"] < cutoff or len(vids) >= cfg.get("per_channel", 5):
                    break
                if not is_short(v["id"], v["title"], state):
                    vids.append(v)
        for v in vids:
            v["channel"] = name
        info["count"] = len(vids)
        return info, vids
    except Exception as e:
        info["error"] = str(e)[:120]
        print("  FAILED", name, e, file=sys.stderr)
        return info, None


def main():
    cfg = load("channels.json", {"channels": []})
    state = load("state.json", {})
    for k, d in (("ids", {}), ("short", {}), ("pick", {}), ("guessed", {})):
        state.setdefault(k, d)
    prev = load("videos.json", {"videos": []})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(process, [(c, cfg, state) for c in cfg["channels"]]))
    videos, chans = [], []
    for info, vids in results:
        chans.append(info)
        if vids is None:  # keep the previous copy if a channel had a hiccup
            vids = [v for v in prev.get("videos", []) if v.get("channel") == info["name"]]
        videos.extend(vids)
    ok = sum(1 for c in chans if c["ok"])
    print(f"channels ok: {ok}/{len(chans)}  videos: {len(videos)}")
    for c in chans:
        print("  ", "OK " if c["ok"] else "ERR", c["name"], "->", c.get("found") or c.get("error"), "(guessed)" if c.get("guessed") else "")
    if ok == 0:
        print("Nothing reachable: keeping the previous videos.json")
        return
    videos.sort(key=lambda v: v["published"], reverse=True)
    for v in videos:
        v["thumb"] = f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg"
    keep = {v["id"] for v in videos}
    state["short"] = {k: s for k, s in state["short"].items() if k in keep or len(state["short"]) < 600}
    json.dump({"updated": NOW.isoformat(), "channels": chans, "videos": videos},
              open(os.path.join(HERE, "videos.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(state, open(os.path.join(HERE, "state.json"), "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    import urllib.parse  # noqa
    main()
