#!/usr/bin/env python3
"""Daily Investment Podcast: find new episodes and save full transcripts.

Source of truth for "latest episode" = each show's official RSS feed (no page cache).
Transcript order: RSS transcript tag > official episode page > YouTube captions > Whisper on official audio.
A .md file is saved ONLY if the transcript covers the whole episode. Otherwise: FAILED flag, no file.
"""
import os, re, json, html, tempfile, traceback, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state.json")
OUT_DIR = os.path.join(ROOT, "transcripts")
STATUS_DIR = os.path.join(ROOT, "status")
HKT = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime.now(dt.timezone.utc)
HDR = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
       "Cache-Control": "no-cache", "Pragma": "no-cache"}

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "4"))
NEW_HOURS = 48
STALE_DAYS = 8
MAX_ATTEMPTS = 4
WHISPER_AFTER_HOURS = float(os.getenv("WHISPER_AFTER_HOURS", "12"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small.en")

SHOWS = [
    {"key": "All-In", "name": "All-In", "apple_id": "1502871393",
     "search": "All-In with Chamath Jason Sacks Friedberg", "name_has": "All-In", "artist_has": "All-In",
     "title_re": None, "page": "https://allin.com/episodes"},
    {"key": "Forward-Guidance", "name": "Forward Guidance (Blockworks)", "apple_id": None,
     "search": "Forward Guidance Blockworks", "name_has": "Forward Guidance", "artist_has": "Blockworks",
     "title_re": None, "page": "https://blockworks.com/podcast/forwardguidance"},
    {"key": "Systematic-Investor", "name": "Systematic Investor (Top Traders Unplugged)", "apple_id": None,
     "search": "Top Traders Unplugged", "name_has": "Top Traders Unplugged", "artist_has": "",
     "title_re": r"^\s*SI\s?\d+", "page": "https://www.toptradersunplugged.com/podcasts/systematic-investor/"},
]

MARKER = re.compile(r"((?:[A-Z][A-Za-z.'\-]*)(?: [A-Z][A-Za-z.'\-]*){0,2}): ?\(?(\d{1,2}:\d{2}:\d{2})\)?")
YT = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^\"'\s<>]*&)?v=|embed/|live/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def get(url, **kw):
    r = requests.get(url, headers=HDR, timeout=kw.pop("timeout", 60), **kw)
    r.raise_for_status()
    return r


def bust(url):
    return url + ("&" if "?" in url else "?") + "t=" + NOW.strftime("%Y%m%d%H%M")


def secs(ts):
    p = [float(x) for x in str(ts).replace(",", ".").split(":")]
    s = 0.0
    for x in p:
        s = s * 60 + x
    return s


def hms(s):
    s = int(s)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"feeds": {}, "episodes": {}}


def save_state(st):
    with open(STATE, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)


# ---------- feed ----------
def resolve_feed(show, st):
    env = os.getenv("FEED_" + show["key"].replace("-", "_").upper())
    if env:
        return env
    if st["feeds"].get(show["key"]):
        return st["feeds"][show["key"]]
    if show["apple_id"]:
        res = get("https://itunes.apple.com/lookup", params={"id": show["apple_id"]}).json()
    else:
        res = get("https://itunes.apple.com/search",
                  params={"term": show["search"], "media": "podcast", "entity": "podcast", "limit": 25}).json()
    for x in res.get("results", []):
        if (show["name_has"].lower() in x.get("collectionName", "").lower()
                and show["artist_has"].lower() in x.get("artistName", "").lower() and x.get("feedUrl")):
            st["feeds"][show["key"]] = x["feedUrl"]
            return x["feedUrl"]
    raise RuntimeError("RSS feed not found via Apple lookup")


def child(it, suffix):
    for c in it:
        if c.tag == suffix or c.tag.endswith("}" + suffix):
            yield c


def ctext(it, suffix):
    for c in child(it, suffix):
        return (c.text or "").strip()
    return ""


def parse_feed(data):
    root = ET.fromstring(data)
    items = []
    for it in root.iter("item"):
        try:
            d = parsedate_to_datetime(ctext(it, "pubDate")).astimezone(dt.timezone.utc)
        except Exception:
            continue
        dur = ctext(it, "duration")
        enc = next(child(it, "enclosure"), None)
        items.append({
            "title": html.unescape(ctext(it, "title")),
            "date": d,
            "guid": ctext(it, "guid") or ctext(it, "link") or ctext(it, "title"),
            "link": ctext(it, "link"),
            "duration": secs(dur) if dur and re.fullmatch(r"[\d:.]+", dur) else None,
            "audio": enc.get("url") if enc is not None else None,
            "desc": ctext(it, "description") + " " + ctext(it, "encoded"),
            "transcripts": [(c.get("url"), (c.get("type") or "").lower())
                            for c in child(it, "transcript") if c.get("url")],
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


# ---------- transcript parsers ----------
def parse_cues(text):
    segs, start, spk, buf = [], None, None, []

    def flush():
        if start is not None and buf:
            segs.append((start, spk, " ".join(buf)))
    for line in text.splitlines():
        line = line.strip()
        if "-->" in line:
            flush()
            start, spk, buf = secs(line.split("-->")[0].strip()), None, []
            continue
        if not line or line.isdigit() or line.startswith(("WEBVTT", "NOTE", "STYLE", "Kind:", "Language:")):
            continue
        if start is None:
            continue
        m = re.match(r"<v\s+([^>]+)>", line)
        if m:
            spk = m.group(1).strip()
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line:
            buf.append(line)
    flush()
    return segs


def parse_marked_text(text):
    parts = MARKER.split(text)
    segs = []
    for i in range(1, len(parts) - 2, 3):
        body = re.sub(r"\s+", " ", parts[i + 2]).strip()
        if body:
            segs.append((secs(parts[i + 1]), parts[i].strip(), body))
    return segs


def from_rss_tag(item):
    pref = ["json", "vtt", "srt", "html", "plain"]
    tags = sorted(item["transcripts"], key=lambda t: next((i for i, p in enumerate(pref) if p in t[1]), 9))
    for url, typ in tags:
        try:
            r = get(url)
            if "json" in typ:
                segs = [(float(s.get("startTime", 0)), s.get("speaker"), s.get("body", "").strip())
                        for s in r.json().get("segments", []) if s.get("body")]
            elif "vtt" in typ or "srt" in typ or "-->" in r.text[:5000]:
                segs = parse_cues(r.text)
            else:
                segs = parse_marked_text(BeautifulSoup(r.text, "html.parser").get_text("\n"))
            if segs:
                return segs, "RSS transcript tag (" + (typ or "unknown") + ")"
        except Exception as e:
            print("  rss transcript error:", e)
    return None, None


def from_page(item, page_html):
    if not page_html:
        return None, None
    soup = BeautifulSoup(page_html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    best, best_n, best_len = None, 0, 0
    cands = [(el, len(MARKER.findall(el.get_text(" ")))) for el in soup.find_all(["article", "section", "div", "main", "body"])]
    top = max([n for _, n in cands] or [0])
    if top < 5:
        return None, None
    for el, n in cands:
        if n >= 0.95 * top:
            L = len(el.get_text(" "))
            if best is None or L < best_len:
                best, best_len = el, L
    segs = parse_marked_text(best.get_text("\n"))
    return (segs, "Official episode page") if segs else (None, None)


def from_youtube(vid):
    from youtube_transcript_api import YouTubeTranscriptApi
    try:
        ft = YouTubeTranscriptApi().fetch(vid, languages=["en", "en-US", "en-GB"])
        raw = [(s.start, s.text) for s in ft]
    except AttributeError:
        raw = [(s["start"], s["text"]) for s in YouTubeTranscriptApi.get_transcript(vid, languages=["en", "en-US"])]
    segs, last = [], None
    for st_, tx in raw:
        tx = html.unescape(tx).replace("\n", " ").strip()
        if tx and tx != last and tx not in ("[Music]", "[Applause]"):
            segs.append((st_, None, tx))
        last = tx
    return segs, "YouTube captions (https://www.youtube.com/watch?v=" + vid + ")"


_model = None


def from_whisper(audio_url):
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    fd, path = tempfile.mkstemp(suffix=".mp3")
    with os.fdopen(fd, "wb") as f, requests.get(audio_url, headers=HDR, stream=True, timeout=300) as r:
        r.raise_for_status()
        for c in r.iter_content(1 << 20):
            f.write(c)
    try:
        segments, info = _model.transcribe(path, language="en", vad_filter=True, beam_size=1)
        segs, end = [], 0.0
        for s in segments:
            if s.text.strip():
                segs.append((s.start, None, s.text.strip()))
            end = s.end
    finally:
        os.remove(path)
    return segs, end, info.duration


# ---------- completeness ----------
def complete(segs, duration, lenient=False):
    if not segs:
        return False, "empty"
    last = max(s[0] for s in segs)
    if not duration:
        return False, "episode length unknown, cannot verify"
    need = min(duration - 300, 0.85 * duration) if lenient else duration - 300
    if last < need:
        return False, f"stops at {hms(last)} of {hms(duration)}"
    return True, "ok"


def render(segs):
    out, cur = [], None
    for start, spk, text in segs:
        if cur and ((spk and spk == cur[1]) or (not spk and not cur[1] and start - cur[0] < 60)):
            cur[2].append(text)
        else:
            cur = [start, spk, [text]]
            out.append(cur)
    lines = []
    for start, spk, texts in out:
        tag = f"**[{hms(start)}] {spk}:**" if spk else f"**[{hms(start)}]**"
        lines.append(tag + " " + re.sub(r"\s+", " ", " ".join(texts)).strip())
    return "\n\n".join(lines) + "\n"


def slug(t):
    return re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-")[:80]


# ---------- per episode ----------
def process(show, item, ep):
    age_h = (NOW - item["date"]).total_seconds() / 3600
    page_html = ""
    if item["link"]:
        try:
            page_html = get(bust(item["link"])).text
        except Exception as e:
            print("  page error:", e)
    m = YT.search(item["desc"] + " " + page_html)
    vid = m.group(1) if m else None
    link = f"https://www.youtube.com/watch?v={vid}" if vid else item["link"]
    ep["link"] = link
    reasons = []

    def try_(name, fn, lenient=False):
        try:
            segs, src = fn()
            if not segs:
                reasons.append(name + ": not found")
                return None
            ok, why = complete(segs, item["duration"], lenient)
            if ok:
                return segs, src
            reasons.append(name + ": " + why)
        except Exception as e:
            reasons.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
        return None

    got = None
    if item["transcripts"]:
        got = try_("RSS tag", lambda: from_rss_tag(item))
    if not got:
        got = try_("Episode page", lambda: from_page(item, page_html))
    if not got and vid:
        got = try_("YouTube", lambda: from_youtube(vid), lenient=True)
    elif not vid:
        reasons.append("YouTube: no link on episode page or feed")
    if not got and item["audio"]:
        if age_h < WHISPER_AFTER_HOURS:
            ep["status"] = "pending"
            ep["reason"] = f"waiting for official transcript (Whisper fallback after {WHISPER_AFTER_HOURS:.0f}h); " + "; ".join(reasons)
            return
        try:
            segs, end, adur = from_whisper(item["audio"])
            if segs and end >= adur - 120:
                got = (segs, f"Machine transcription of official audio (faster-whisper {WHISPER_MODEL}); no speaker names")
            else:
                reasons.append(f"Whisper: stops at {hms(end)} of {hms(adur)}")
        except Exception as e:
            reasons.append(f"Whisper: {type(e).__name__}: {str(e)[:120]}")

    ep["attempts"] = ep.get("attempts", 0) + 1
    if got:
        segs, src = got
        d = item["date"].astimezone(HKT).strftime("%Y-%m-%d")
        fn = f"{d}_{show['key']}_{slug(item['title'])}.md"
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, fn), "w", encoding="utf-8") as f:
            f.write(f"# {item['title']}\n{show['name']} | {d} | {link} | Transcript source: {src}\n---\n\n")
            f.write(render(segs))
        ep.update(status="saved", file="transcripts/" + fn, source=src, reason="")
    else:
        ep.update(status="failed" if ep["attempts"] >= MAX_ATTEMPTS else "retrying", reason="; ".join(reasons))


def open_issue(title, body):
    tok, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not (tok and repo):
        return
    try:
        requests.post(f"https://api.github.com/repos/{repo}/issues", timeout=30,
                      headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"},
                      json={"title": title, "body": body})
    except Exception as e:
        print("issue error:", e)


def main():
    st = load_state()
    report, alerts = [], []
    for show in SHOWS:
        row = {"show": show["name"]}
        try:
            feed = resolve_feed(show, st)
            items = parse_feed(get(feed, timeout=90).content)
            if show["title_re"]:
                items = [i for i in items if re.match(show["title_re"], i["title"])]
            if not items:
                raise RuntimeError("feed has no matching episodes")
            for item in items:
                if item["date"] < NOW - dt.timedelta(days=LOOKBACK_DAYS):
                    break
                ep = st["episodes"].setdefault(item["guid"], {"title": item["title"], "show": show["key"],
                                                              "date": item["date"].isoformat()})
                if ep.get("status") in ("saved", "failed"):
                    continue
                print(f"[{show['key']}] {item['title']}")
                process(show, item, ep)
                save_state(st)
                if ep["status"] == "failed":
                    alerts.append(f"FAILED transcript: {show['name']} | {item['title']} | {ep['reason']}")
            top = items[0]
            ep = st["episodes"].get(top["guid"], {})
            age_h = (NOW - top["date"]).total_seconds() / 3600
            row.update(title=top["title"], date=top["date"].astimezone(HKT).strftime("%Y-%m-%d %H:%M HKT"),
                       link=ep.get("link") or top["link"], feed=feed,
                       status="NEW" if age_h <= NEW_HOURS else ("UNVERIFIED: feed newest is over 8 days old" if age_h > STALE_DAYS * 24 else "No new episode"),
                       transcript=(f"Saved: {ep['file']} ({ep['source']})" if ep.get("status") == "saved"
                                   else f"{ep.get('status', 'n/a').upper()}: {ep.get('reason', '')}" if ep.get("status") else "n/a"))
            if row["status"].startswith("UNVERIFIED"):
                alerts.append(f"{show['name']}: {row['status']} (newest: {top['title']})")
        except Exception as e:
            traceback.print_exc()
            row.update(status=f"UNVERIFIED: {type(e).__name__}: {str(e)[:150]}")
            alerts.append(f"{show['name']}: {row['status']}")
        report.append(row)
    save_state(st)

    os.makedirs(STATUS_DIR, exist_ok=True)
    stamp = NOW.astimezone(HKT).strftime("%Y-%m-%d %H:%M HKT")
    md = [f"# Podcast status\nLast run: {stamp}\n"]
    for r in report:
        md.append(f"## {r['show']}\n- Status: {r['status']}")
        if r.get("title"):
            md.append(f"- Title: {r['title']} ({r['date']})\n- Link: {r['link']}\n- Transcript: {r['transcript']}")
        md.append("")
    with open(os.path.join(STATUS_DIR, "latest.md"), "w") as f:
        f.write("\n".join(md))
    with open(os.path.join(STATUS_DIR, "latest.json"), "w") as f:
        json.dump({"last_run": stamp, "shows": report}, f, indent=1)
    if alerts:
        open_issue(f"Podcast alert {stamp}", "\n".join("- " + a for a in alerts))
    print("\n".join(md))


if __name__ == "__main__":
    main()
