#!/usr/bin/env python3
"""
IPTV playlist yoxlayıcı və avtomatik əvəzləyici.

1. playlist.m3u-dakı hər kanalı yoxlayır (HLS manifest + ilk seqment, DASH, xam axın).
2. Ardıcıl N dəfə işləməyən kanal "ölü" sayılır.
3. Ölü kanal üçün ehtiyat siyahılardan (sources.txt + backup/*.m3u) adı oxşar olan
   kanalları tapır, onları da yoxlayır və ilk işləyəni əvəz kimi yazır.

Yalnız `requests` lazımdır.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Linux; Android 11; TV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

DEFAULT_CONFIG = {
    "timeout": 8,            # saniyə, hər sorğu üçün
    "workers": 40,           # paralel yoxlama sayı
    "fail_threshold": 2,     # neçə ardıcıl uğursuz işləmədən sonra əvəz olunsun
    "match_threshold": 0.9,  # ad oxşarlığı (0-1)
    "max_candidates": 8,     # bir kanal üçün ən çox neçə namizəd yoxlansın
    "protected": [],         # adında bu sözlər olan kanallara toxunulmur
    "aliases": {},           # {"Xezer TV": ["Khazar TV"]}
    # --- coğrafi məhdudiyyət ---
    "geo_hosts": [],         # host-da bu parça olan linklər AZ/TR-dən kənarda yoxlanmır
    "trusted_countries": ["AZ", "TR"],  # yoxlayıcı bu ölkələrdən işləyirsə geo linklər də yoxlanır
    "geo_proxy": "",         # məs. "socks5h://user:pass@host:port" (AZ/TR proxy), yalnız geo linklər üçün
    "max_dead_ratio": 0.6,   # yoxlananların bu qədərindən çoxu ölü çıxsa, şəbəkə xətası sayılır, heç nə yazılmır
}

# Bu hostlar/domenlər yalnız AZ/TR IP-sindən etibarlı yoxlanır (config.json-dakı geo_hosts bunlara əlavə olunur)
BUILTIN_GEO = [".az", ".com.tr", ".tr", "ercdn.net", "daioncdn.net", "duhnet.tv", "yayin.com.tr",
               "radyotvonline", "onlineradyotv", "powerapp.com.tr", "medya.trt.com.tr", "rocketcdn.com",
               "dygdigital.com", "85.132."]


# --------------------------------------------------------------------------- #
# M3U oxuma
# --------------------------------------------------------------------------- #
@dataclass
class Entry:
    ext_idx: int
    url_idx: int
    extinf: str
    name: str
    group: str
    extras: list = field(default_factory=list)
    url: str = ""
    source: str = ""
    key: str = ""


def split_extinf_name(extinf: str) -> str:
    """#EXTINF sətrində, dırnaqdan kənardakı ilk vergüldən sonrakı hissə = ad."""
    in_q = False
    for i, ch in enumerate(extinf):
        if ch == '"':
            in_q = not in_q
        elif ch == "," and not in_q:
            return extinf[i + 1:].strip()
    return ""


def parse_m3u(text: str, source: str = "") -> list[Entry]:
    lines = text.splitlines()
    out: list[Entry] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("#EXTINF"):
            j, extras, url = i + 1, [], None
            while j < len(lines):
                s = lines[j].strip()
                if not s:
                    j += 1
                elif s.startswith("#EXTINF"):
                    break
                elif s.startswith("#"):
                    extras.append(lines[j].rstrip())
                    j += 1
                else:
                    url = s
                    break
            if url is not None:
                m = re.search(r'group-title="([^"]*)"', lines[i])
                out.append(Entry(i, j, lines[i], split_extinf_name(lines[i]),
                                 m.group(1) if m else "", extras, url, source))
                i = j + 1
                continue
            i = j
            continue
        i += 1
    return out


def assign_keys(entries: list[Entry]) -> None:
    seen: dict[str, int] = {}
    for e in entries:
        base = f"{e.group}|{e.name}"
        seen[base] = seen.get(base, 0) + 1
        e.key = f"{base}|{seen[base]}"


# --------------------------------------------------------------------------- #
# Axın yoxlaması
# --------------------------------------------------------------------------- #
def split_kodi_headers(url: str) -> tuple[str, dict]:
    """url|User-Agent=x&Referer=y  ->  (url, {headers})"""
    if "|" not in url:
        return url, {}
    base, _, tail = url.partition("|")
    headers = {}
    for part in tail.split("&"):
        if "=" in part:
            k, _, v = part.partition("=")
            headers[k.strip()] = urllib.parse.unquote(v.strip())
    return base, headers


def fetch(url, headers, timeout, max_bytes=65536, rng=None, proxies=None):
    h = {"User-Agent": UA, **headers}
    if rng:
        h["Range"] = f"bytes=0-{rng}"
    deadline = time.monotonic() + timeout * 2
    r = requests.get(url, headers=h, timeout=(timeout, timeout), stream=True, allow_redirects=True, proxies=proxies)
    data = b""
    try:
        for chunk in r.iter_content(8192):
            data += chunk
            if len(data) >= max_bytes or time.monotonic() > deadline:
                break
    finally:
        r.close()
    return r.status_code, r.headers.get("content-type", ""), data, r.url


def looks_html(ct: str, data: bytes) -> bool:
    head = data[:64].lstrip().lower()
    return "text/html" in ct.lower() or head.startswith((b"<!doctype", b"<html"))


def check_hls(text: str, base_url: str, headers: dict, timeout: int, depth: int, proxies=None):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        if depth >= 3:
            return False, "çox dərin master"
        for idx, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF"):
                for nxt in lines[idx + 1:]:
                    if not nxt.startswith("#"):
                        return check_stream(urllib.parse.urljoin(base_url, nxt), headers, timeout, depth + 1, proxies)
        return False, "variant yoxdur"
    segs = [l for l in lines if not l.startswith("#")]
    if not segs:
        return False, "seqment yoxdur"
    seg_url = urllib.parse.urljoin(base_url, segs[-1])
    try:
        st, ct, data, _ = fetch(seg_url, headers, timeout, max_bytes=4096, rng=4095, proxies=proxies)
    except requests.RequestException as e:
        return False, f"seqment: {type(e).__name__}"
    if st not in (200, 206):
        return False, f"seqment HTTP {st}"
    if not data or looks_html(ct, data):
        return False, "seqment boş/HTML"
    return True, "hls"


def check_stream(url: str, headers: dict | None = None, timeout: int = 8, depth: int = 0, proxies=None):
    base, kodi = split_kodi_headers(url)
    headers = {**(headers or {}), **kodi}
    if not re.match(r"https?://", base, re.I):
        return False, "dəstəklənməyən sxem"
    try:
        st, ct, data, final = fetch(base, headers, timeout, proxies=proxies)
    except requests.RequestException as e:
        return False, type(e).__name__
    if st not in (200, 206):
        return False, f"HTTP {st}"
    if b"#EXTM3U" in data[:512]:
        return check_hls(data.decode("utf-8", "replace"), final, headers, timeout, depth, proxies)
    if b"<MPD" in data[:4096]:
        return True, "dash"
    if looks_html(ct, data):
        return False, "HTML cavabı"
    if len(data) < 188:
        return False, "boş/qısa cavab"
    return True, "raw"


def check_with_retry(url: str, timeout: int, proxies=None):
    ok, why = check_stream(url, None, timeout, 0, proxies)
    if ok:
        return ok, why
    time.sleep(1.5)
    return check_stream(url, None, timeout, 0, proxies)


# --------------------------------------------------------------------------- #
# Coğrafi məhdudiyyət
# --------------------------------------------------------------------------- #
def runner_country(timeout: int = 8) -> str:
    """Yoxlayıcının hansı ölkədən işlədiyini tapır. Env IPTV_COUNTRY=AZ ilə əl ilə də verilə bilər."""
    import os
    forced = os.environ.get("IPTV_COUNTRY", "").strip().upper()
    if forced:
        return forced
    for url in ("https://ipinfo.io/country", "https://ipapi.co/country/", "https://api.country.is/"):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "curl/8"})
            txt = r.text.strip()
            if url.endswith("country.is/"):
                txt = r.json().get("country", "")
            if re.fullmatch(r"[A-Za-z]{2}", txt):
                return txt.upper()
        except (requests.RequestException, ValueError):
            continue
    return ""


def is_geo(url: str, patterns: list[str]) -> bool:
    host = urllib.parse.urlparse(split_kodi_headers(url)[0]).netloc.lower().split(":")[0]
    return any(p.lower() in host for p in patterns)


# --------------------------------------------------------------------------- #
# Ad uyğunlaşdırma
# --------------------------------------------------------------------------- #
_CYR = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r",
     "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))
_NOISE = {"hd", "fhd", "uhd", "sd", "hq", "4k", "hevc", "h265"}


def norm_tokens(name: str) -> list[str]:
    s = name.lower()
    s = re.sub(r"[\[\(].*?[\]\)]", " ", s)
    s = "".join(_CYR.get(c, c) for c in s)
    s = s.replace("ə", "e").replace("ı", "i")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t not in _NOISE and not re.fullmatch(r"\d{3,4}p", t)]


def digits_of(toks: list[str]) -> list[str]:
    return re.findall(r"\d+", " ".join(toks))


def name_variants(toks: list[str]) -> list[str]:
    full = "".join(toks)
    notv = "".join(t for t in toks if t != "tv")
    return [full] + ([notv] if notv and notv != full else [])


def find_candidates(name: str, aliases: list[str], pool: list[dict], threshold: float):
    """pool elementləri: {entry, toks, variants, digits}. (score, entry) siyahısı qaytarır."""
    results: dict[str, tuple[float, Entry]] = {}
    for q in [name, *aliases]:
        qt = norm_tokens(q)
        if not qt:
            continue
        qd = digits_of(qt)
        qv = name_variants(qt)
        for c in pool:
            if c["digits"] != qd:
                continue
            best = 0.0
            for a in qv:
                for b in c["variants"]:
                    if a == b:
                        best = 1.0
                        break
                    sm = difflib.SequenceMatcher(None, a, b)
                    if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                        continue
                    best = max(best, sm.ratio())
                if best == 1.0:
                    break
            if best >= threshold:
                e = c["entry"]
                prev = results.get(e.url)
                if not prev or best > prev[0]:
                    results[e.url] = (best, e)
    return sorted(results.values(), key=lambda x: -x[0])


def build_pool(entries: list[Entry]) -> list[dict]:
    pool, seen = [], set()
    for e in entries:
        if e.url in seen:
            continue
        seen.add(e.url)
        t = norm_tokens(e.name)
        if t:
            pool.append({"entry": e, "toks": t, "variants": name_variants(t), "digits": digits_of(t)})
    return pool


# --------------------------------------------------------------------------- #
# Ehtiyat mənbələr
# --------------------------------------------------------------------------- #
def to_raw(url: str) -> str:
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
    return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else url


def load_backup_entries(root: Path, timeout: int) -> list[Entry]:
    entries: list[Entry] = []
    src = root / "sources.txt"
    if src.exists():
        for line in src.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = to_raw(line)
            try:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=max(timeout, 30))
                r.raise_for_status()
                got = parse_m3u(r.content.decode("utf-8", "replace"), source=url)
                print(f"  mənbə OK  : {url}  ({len(got)} kanal)")
                entries += got
            except requests.RequestException as e:
                print(f"  mənbə XƏTA: {url}  ({type(e).__name__})")
    for f in sorted((root / "backup").glob("*.m3u*")):
        got = parse_m3u(f.read_text(encoding="utf-8", errors="replace"), source=f.name)
        print(f"  lokal     : {f.name}  ({len(got)} kanal)")
        entries += got
    return entries


# --------------------------------------------------------------------------- #
# Əsas axın
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--playlist", default=str(ROOT / "playlist.m3u"))
    ap.add_argument("--root", default=str(ROOT), help="config.json/sources.txt/backup/state.json qovluğu")
    ap.add_argument("--now", action="store_true", help="fail_threshold=1 (dərhal əvəz et)")
    ap.add_argument("--dry-run", action="store_true", help="heç nə yazma, yalnız göstər")
    ap.add_argument("--check-only", action="store_true", help="yalnız yoxla, əvəz etmə")
    args = ap.parse_args()

    root = Path(args.root)
    cfg = dict(DEFAULT_CONFIG)
    cfg_path = root / "config.json"
    if cfg_path.exists():
        cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
    if args.now:
        cfg["fail_threshold"] = 1
    timeout = int(cfg["timeout"])

    pl_path = Path(args.playlist)
    text = pl_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    entries = parse_m3u(text, source="playlist")
    assign_keys(entries)
    print(f"Playlist: {len(entries)} kanal")

    state_path = root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    # coğrafi vəziyyət
    geo_patterns = BUILTIN_GEO + list(cfg["geo_hosts"])
    country = runner_country()
    in_region = country in [c.upper() for c in cfg["trusted_countries"]]
    geo_proxy = (cfg.get("geo_proxy") or "").strip()
    proxy_map = {"http": geo_proxy, "https": geo_proxy} if geo_proxy else None
    can_verify_geo = in_region or bool(proxy_map)
    print(f"Yoxlayıcının ölkəsi: {country or 'bilinmir'} | geo linklər yoxlanır: {'bəli' if can_verify_geo else 'XEYR (atlanır, toxunulmur)'}")

    def px(url):
        return proxy_map if (proxy_map and not in_region and is_geo(url, geo_patterns)) else None

    def run_check(e):
        if not can_verify_geo and is_geo(e.url, geo_patterns):
            return None  # yoxlanmadı
        return check_with_retry(e.url, timeout, px(e.url))

    # 1) hamısını yoxla
    print("Yoxlanılır...")
    with ThreadPoolExecutor(max_workers=int(cfg["workers"])) as ex:
        results = list(ex.map(run_check, entries))

    checked = [r for r in results if r is not None]
    n_bad = sum(1 for ok, _ in checked if not ok)
    if len(checked) >= 10 and n_bad / len(checked) > float(cfg["max_dead_ratio"]):
        print(f"DAYAN: yoxlananların {n_bad}/{len(checked)}-i ölü çıxdı. Bu, şəbəkə/blok problemidir, heç nə dəyişdirilmir.")
        return 1

    new_state, alive, failing, skipped = {}, [], [], []
    for e, res in zip(entries, results):
        old = state.get(e.key, {})
        if res is None:
            new_state[e.key] = old or {"fails": 0, "last_ok": "-"}
            skipped.append(e)
            continue
        ok, why = res
        if ok:
            new_state[e.key] = {"fails": 0, "last_ok": now_iso()}
            alive.append(e)
        else:
            new_state[e.key] = {"fails": old.get("fails", 0) + 1, "last_ok": old.get("last_ok", "-"), "why": why}
            failing.append((e, why))

    dead = [(e, why) for e, why in failing if new_state[e.key]["fails"] >= cfg["fail_threshold"]]
    pending = [(e, why) for e, why in failing if new_state[e.key]["fails"] < cfg["fail_threshold"]]
    print(f"İşləyir: {len(alive)} | Gözləmədə: {len(pending)} | Ölü: {len(dead)} | Yoxlanmadı (geo): {len(skipped)}")

    protected = [p.lower() for p in cfg["protected"]]
    replaced, unfixed = [], []

    # 2) ölüləri əvəz et
    if dead and not args.check_only:
        print("Ehtiyat siyahılar yüklənir...")
        backups = load_backup_entries(root, timeout)
        dead_urls = {e.url for e, _ in dead}
        # playlist-in öz sağlam kanalları da namizəd ola bilər
        # yoxlana bilməyən (geo) namizədlər seçilmir: işlədiyi sübut olunmayan linki yazmaq olmaz
        pool = build_pool([c for c in backups if can_verify_geo or not is_geo(c.url, geo_patterns)] + alive
                         )
        tested: dict[str, tuple[bool, str]] = {}

        with ThreadPoolExecutor(max_workers=int(cfg["workers"])) as ex:
            for e, why in dead:
                if any(p in e.name.lower() for p in protected):
                    unfixed.append((e, why + " (qorunur)"))
                    continue
                cands = [(s, c) for s, c in find_candidates(
                    e.name, cfg["aliases"].get(e.name, []), pool, float(cfg["match_threshold"]))
                    if c.url != e.url and c.url not in dead_urls][: int(cfg["max_candidates"])]
                todo = [c for _, c in cands if c.url not in tested]
                for c, res in zip(todo, ex.map(lambda c: check_with_retry(c.url, timeout, px(c.url)), todo)):
                    tested[c.url] = res
                winner = next(((s, c) for s, c in cands if tested[c.url][0]), None)
                if not winner:
                    unfixed.append((e, why + (f", {len(cands)} namizəd işləmir" if cands else ", namizəd tapılmadı")))
                    continue
                s, c = winner
                replaced.append((e, c, s))
                new_state[e.key] = {"fails": 0, "last_ok": now_iso(), "replaced_from": e.url}
                print(f"  ƏVƏZ: {e.name}  <-  {c.name} [{c.source}] (oxşarlıq {s:.2f})")
    else:
        unfixed = list(dead)

    # 3) yaz
    if replaced and not args.dry_run:
        for e, c, _ in sorted(replaced, key=lambda x: -x[0].ext_idx):
            lines[e.ext_idx + 1: e.url_idx + 1] = list(c.extras) + [c.url]
        pl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not args.dry_run:
        state_path.write_text(json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8")
        rep = root / "reports"
        rep.mkdir(exist_ok=True)
        write_report(rep / "status.md", len(entries), alive, pending, replaced, unfixed, skipped, country)
        if replaced:
            with (rep / "replacements.log").open("a", encoding="utf-8") as f:
                for e, c, s in replaced:
                    f.write(f"{now_iso()} | {e.group} | {e.name} | {e.url} -> {c.url} ({c.source}, {s:.2f})\n")

    print(f"Əvəz olundu: {len(replaced)} | Həll olunmadı: {len(unfixed)}")
    return 0


def write_report(path: Path, total, alive, pending, replaced, unfixed, skipped=(), country=""):
    md = [f"# Playlist statusu\n", f"Son yoxlama: **{now_iso()}**\n",
          f"- Cəmi: **{total}**  \n- İşləyir: **{len(alive)}**  \n- Gözləmədə (ilk uğursuzluq): **{len(pending)}**  \n"
          f"- Bu dəfə əvəz olundu: **{len(replaced)}**  \n- Əvəzi tapılmadı: **{len(unfixed)}**  \n"
          f"- Yoxlanmadı (AZ/TR linki, yoxlayıcı ölkəsi: {country or '?'}): **{len(skipped)}**\n"]
    if replaced:
        md += ["\n## Əvəz olunanlar\n", "| Kanal | Yeni link | Mənbə |", "|---|---|---|"]
        md += [f"| {e.name} | `{c.url[:70]}` | {c.source} |" for e, c, _ in replaced]
    if unfixed:
        md += ["\n## Əvəzi tapılmayanlar (ölü)\n", "| Qrup | Kanal | Səbəb |", "|---|---|---|"]
        md += [f"| {e.group} | {e.name} | {why} |" for e, why in unfixed]
    if pending:
        md += ["\n## Gözləmədə (növbəti yoxlamada yenə işləməsə əvəz olunacaq)\n"]
        md += [f"- {e.name} — {why}" for e, why in pending]
    path.write_text("\n".join(md) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
