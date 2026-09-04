#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""GoogleマップURL -> 店舗情報 PoC。

作戦書: 20260904_store-form-url-autofill-poc_作戦書.md
本編:   20260903_store-form-url-autofill_作戦書.md

使い方:
    uv run lookup.py --dry-run                 # Places API を叩かず、展開とパースだけ（無課金）
    uv run lookup.py                           # urls.txt を全部通す
    uv run lookup.py <URL>                     # URL 1本だけ
    uv run lookup.py --radius 50               # 逃げ道ア: locationBias を詰める
    uv run lookup.py --query full              # 逃げ道イ: 店名だけでなく place セグメント全体で検索
    uv run lookup.py --candidates 3            # 逃げ道ウ: 候補を複数見る（⚠ Enterprise 課金が件数分）
    uv run lookup.py --probe-fields <place_id> # ⑥ movedPlace 等が実在するかを1回だけ確かめる
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
URLS_FILE = HERE / "urls.txt"
ENV_FILE = HERE / ".env"

PLACES_BASE = "https://places.googleapis.com/v1"

# ⚠️ searchText の FieldMask は places.id で固定する（定数・引数で足せない）。
# 1つでもフィールドを足すと Text Search Pro（$32/1,000）に跳ねる。本編 §10-2。
SEARCH_FIELD_MASK = "places.id"

# Place Details。本編 §3-3 ＋ PoC §5（⑤⑥のための追加分）。
# 追加分はすべて Essentials / Pro なので、Enterprise が含まれる時点で追加課金は無い。
DETAILS_FIELDS = [
    # --- 本編 §3-3 ---
    "id",
    "displayName",
    "formattedAddress",
    "addressComponents",
    "nationalPhoneNumber",       # Enterprise
    "regularOpeningHours",       # Enterprise
    "googleMapsUri",
    # --- PoC 追加（⑤ 正規化が要らなくなるか） ---
    "shortFormattedAddress",     # Essentials
    "postalAddress",             # Essentials
    # --- PoC 追加（⑥ 閉店検知 / 逃げ道エ の座標検証） ---
    "businessStatus",            # Pro
    "location",                  # Essentials
]

# ⑥ の「移転」系。実在するか未確認なので既定では送らない。
# 1つでも無効なフィールドが混ざると Details のリクエスト全体が 400 になるため。
# --probe-fields で1回だけ個別に確かめる。
EXPERIMENTAL_FIELDS = ["movedPlace", "movedPlaceId", "consumerAlert"]

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.co", "maps.google.com/url"}


# --------------------------------------------------------------------------
# 小物
# --------------------------------------------------------------------------

def load_api_key() -> str | None:
    """.env から GOOGLE_MAPS_API_KEY を読む（依存を httpx だけに保つため自前）。"""
    import os

    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if key:
        return key.strip()
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "GOOGLE_MAPS_API_KEY":
            return v.strip().strip('"').strip("'") or None
    return None


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def is_short_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"maps.app.goo.gl", "goo.gl", "g.co"} or (
        host.endswith("goo.gl") or host == "g.co"
    )


# --------------------------------------------------------------------------
# ② 短縮URLの展開
# --------------------------------------------------------------------------

def expand(url: str, ua: str | None) -> dict[str, Any]:
    """リダイレクトを追い、経路をすべて記録する。consent.google.com も検知する。"""
    headers = {"User-Agent": ua} if ua else {}
    rec: dict[str, Any] = {
        "user_agent": ua or "(none)",
        "chain": [],
        "final_url": url,
        "redirects": 0,
        "elapsed_ms": None,
        "consent": False,
        "error": None,
        "method": None,
    }
    t0 = time.monotonic()
    try:
        with httpx.Client(follow_redirects=True, timeout=15.0, headers=headers) as c:
            try:
                resp = c.head(url)
                rec["method"] = "HEAD"
                # HEAD を拒む相手がいるので、その場合だけ GET に落とす
                if resp.status_code >= 400:
                    resp = c.get(url)
                    rec["method"] = "HEAD->GET"
            except httpx.HTTPError:
                resp = c.get(url)
                rec["method"] = "GET"
        rec["chain"] = [
            {"status": h.status_code, "url": str(h.url), "location": h.headers.get("location")}
            for h in resp.history
        ]
        rec["final_url"] = str(resp.url)
        rec["final_status"] = resp.status_code
        rec["redirects"] = len(resp.history)
    except httpx.HTTPError as e:
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
    host = (urlparse(rec["final_url"]).hostname or "").lower()
    rec["consent"] = "consent." in host or "/sorry/" in rec["final_url"]
    return rec


# --------------------------------------------------------------------------
# ③ URLのパース（本編 §3-1 の表を実測で確かめる部分）
# --------------------------------------------------------------------------

RE_PLACE_SEG = re.compile(r"/maps/place/([^/@?]+)")
RE_AT = re.compile(r"/@(-?\d+\.\d+),(-?\d+\.\d+)")
RE_D34 = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
RE_FTID = re.compile(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
RE_16S = re.compile(r"!16s(%2F[^!?&]+)")
RE_PLACE_ID_IN_Q = re.compile(r"place_id:([A-Za-z0-9_\-]+)")


def parse_maps_url(url: str) -> dict[str, Any]:
    """URLから取れるものを全部取る。取れなかったものは None のまま残す。"""
    p = urlparse(url)
    q = parse_qs(p.query)
    out: dict[str, Any] = {
        "kind": None,
        "place_id": None,
        "place_id_source": None,
        "place_segment": None,   # `店名` or `店名,+住所`
        "name": None,
        "address_hint": None,
        "viewport_latlng": None,  # /@lat,lng（地図の中心。店の位置とは限らない）
        "place_latlng": None,     # !3d!4d（店そのものの座標。こちらが正確）
        "ftid": None,
        "gid": None,
        "cid": None,
        "text_query_param": None,
        "notes": [],
    }

    # 1) place_id が直接入っている系
    if "query_place_id" in q:
        out["place_id"] = q["query_place_id"][0]
        out["place_id_source"] = "query_place_id"
        out["kind"] = "api1_with_place_id"
    elif "place_id" in q:
        out["place_id"] = q["place_id"][0]
        out["place_id_source"] = "place_id"
        out["kind"] = "place_id_param"
    else:
        for key in ("q", "query"):
            if key in q:
                m = RE_PLACE_ID_IN_Q.search(q[key][0])
                if m:
                    out["place_id"] = m.group(1)
                    out["place_id_source"] = f"{key}=place_id:"
                    out["kind"] = "q_place_id"
                    break

    # 2) cid 系（本編 §3-1: place_id を引く公式手段は無い）
    for key in ("cid", "ludocid"):
        if key in q:
            out["cid"] = q[key][0]
            if not out["kind"]:
                out["kind"] = "cid"
                out["notes"].append("cid から place_id を引く公式手段は無い（本編 §3-1）")

    # 3) /maps/place/<店名>/@lat,lng 系
    m = RE_PLACE_SEG.search(p.path)
    if m:
        seg = unquote_plus(m.group(1))
        out["place_segment"] = seg
        # `店名,+住所` の形なら最初のカンマで割る
        if "," in seg:
            head, _, rest = seg.partition(",")
            out["name"] = head.strip()
            out["address_hint"] = rest.strip()
        else:
            out["name"] = seg.strip()
        if not out["kind"]:
            out["kind"] = "expanded_place"

    # 4) テキストクエリだけの検索URL
    for key in ("query", "q"):
        if key in q and not out["place_id"]:
            out["text_query_param"] = q[key][0]
            if not out["name"]:
                out["name"] = q[key][0]
            if not out["kind"]:
                out["kind"] = "search_query"

    # 5) 座標
    m = RE_AT.search(url)
    if m:
        out["viewport_latlng"] = [float(m.group(1)), float(m.group(2))]
    m = RE_D34.search(url)
    if m:
        out["place_latlng"] = [float(m.group(1)), float(m.group(2))]

    # 6) data= の中身（参考。Places API では使えない）
    m = RE_FTID.search(url)
    if m:
        out["ftid"] = m.group(1)
        out["notes"].append("ftid から Places API (New) を引く公式手段は無い（本編 §3-1）")
    m = RE_16S.search(url)
    if m:
        out["gid"] = unquote_plus(m.group(1))

    # 7) 店舗ではないURL（H1）
    if not out["kind"]:
        if re.search(r"/maps/@-?\d", p.path) or p.path.rstrip("/") in ("/maps", ""):
            out["kind"] = "no_place"
            out["notes"].append("店舗を指していないURL。API を叩かずに失敗させるべき（本編 §5-2）")
        else:
            out["kind"] = "unknown"

    return out


def best_latlng(parsed: dict[str, Any]) -> list[float] | None:
    return parsed.get("place_latlng") or parsed.get("viewport_latlng")


# --------------------------------------------------------------------------
# Places API
# --------------------------------------------------------------------------

def search_text(
    client: httpx.Client,
    key: str,
    text_query: str,
    latlng: list[float] | None,
    radius: float,
    max_results: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "textQuery": text_query,
        "languageCode": "ja",
        "regionCode": "JP",
        "maxResultCount": max_results,
    }
    if latlng:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": latlng[0], "longitude": latlng[1]},
                "radius": radius,
            }
        }
    t0 = time.monotonic()
    r = client.post(
        f"{PLACES_BASE}/places:searchText",
        headers={
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": SEARCH_FIELD_MASK,  # ⚠ 定数。足さないこと
            "Content-Type": "application/json",
        },
        json=body,
    )
    return {
        "request": body,
        "field_mask": SEARCH_FIELD_MASK,
        "status": r.status_code,
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
        "response": _safe_json(r),
    }


def get_details(
    client: httpx.Client, key: str, place_id: str, fields: list[str]
) -> dict[str, Any]:
    t0 = time.monotonic()
    r = client.get(
        f"{PLACES_BASE}/places/{place_id}",
        params={"languageCode": "ja", "regionCode": "JP"},
        headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": ",".join(fields)},
    )
    return {
        "place_id": place_id,
        "field_mask": ",".join(fields),
        "status": r.status_code,
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
        "response": _safe_json(r),
    }


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"_raw_text": r.text[:2000]}


# --------------------------------------------------------------------------
# 1URL分の処理
# --------------------------------------------------------------------------

def process(
    url: str,
    label: str,
    expected: str,
    args: argparse.Namespace,
    key: str | None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "label": label,
        "input_url": url,
        "expected_name": expected,
        "expand": None,
        "parsed": None,
        "search": None,
        "details": [],
        "verdict": {},
    }

    # --- ② 展開 ---
    target = url
    if is_short_url(url) and not args.no_expand:
        attempts = []
        uas: list[str | None] = [None, BROWSER_UA] if args.ua == "both" else (
            [None] if args.ua == "none" else [BROWSER_UA]
        )
        for ua in uas:
            attempts.append(expand(url, ua))
        rec["expand"] = attempts
        ok = next((a for a in attempts if not a["error"] and not a["consent"]), None)
        target = (ok or attempts[-1])["final_url"]
    elif not args.no_expand and urlparse(url).hostname:
        rec["expand"] = "skipped (短縮URLではない)"

    # --- ③ パース ---
    parsed = parse_maps_url(target)
    parsed["parsed_from"] = target
    rec["parsed"] = parsed

    if parsed["kind"] == "no_place":
        rec["verdict"]["outcome"] = "expected_failure（店舗URLではない）"
        return rec
    if parsed["kind"] == "cid" and not parsed["place_id"]:
        rec["verdict"]["outcome"] = "cid のみ → 解決不能（本編 §3-1 どおり）"
        return rec

    if args.dry_run:
        rec["verdict"]["outcome"] = "dry-run（ここまで）"
        return rec
    if not key:
        rec["verdict"]["outcome"] = "APIキー未設定のため中断"
        return rec

    place_ids: list[str] = []
    with httpx.Client(timeout=20.0) as client:
        # --- ③' searchText（place_id が直接あればスキップ） ---
        if parsed["place_id"]:
            place_ids = [parsed["place_id"]]
            rec["verdict"]["resolution"] = f"URLに place_id が直接あり（{parsed['place_id_source']}）"
        else:
            name = parsed["name"]
            if not name:
                rec["verdict"]["outcome"] = "店名が取れず searchText できない"
                return rec
            # 逃げ道イ: 店名だけか、place セグメント全体（店名＋住所片）か
            text_query = parsed["place_segment"] if args.query == "full" and parsed["place_segment"] else name
            s = search_text(
                client, key, text_query, best_latlng(parsed), args.radius, args.candidates
            )
            rec["search"] = s
            if s["status"] != 200:
                rec["verdict"]["outcome"] = f"searchText 失敗 HTTP {s['status']}"
                return rec
            place_ids = [p["id"] for p in s["response"].get("places", [])]
            if not place_ids:
                rec["verdict"]["outcome"] = "searchText 0件"
                return rec
            rec["verdict"]["resolution"] = f"searchText で {len(place_ids)} 件"

        # --- ④ Place Details ---
        fields = DETAILS_FIELDS + (EXPERIMENTAL_FIELDS if args.with_experimental else [])
        for pid in place_ids:
            d = get_details(client, key, pid, fields)
            if d["status"] == 400 and args.with_experimental:
                d["_note"] = "実験フィールドを外して再試行"
                d = get_details(client, key, pid, DETAILS_FIELDS)
            rec["details"].append(d)

    # --- 判定材料 ---
    first = rec["details"][0]["response"] if rec["details"] else {}
    if isinstance(first, dict) and "displayName" in first:
        got = first.get("displayName", {}).get("text", "")
        rec["verdict"]["got_name"] = got
        rec["verdict"]["name_match"] = _name_match(expected, got)
        rec["verdict"]["businessStatus"] = first.get("businessStatus")
        loc = first.get("location")
        src = best_latlng(parsed)
        if loc and src:
            rec["verdict"]["distance_m"] = round(
                haversine_m(src[0], src[1], loc["latitude"], loc["longitude"]), 1
            )
        # ① の決定的な検証: URL の ftid 後半は16進のCID。googleMapsUri の cid= と
        # 突き合わせれば、店名一致より強く「同じ店か」が判る。別の店なら必ず食い違う。
        ftid = parsed.get("ftid")
        m = re.search(r"[?&]cid=(\d+)", first.get("googleMapsUri") or "")
        if ftid and ":" in ftid and m:
            url_cid = str(int(ftid.split(":")[-1], 16))
            rec["verdict"]["url_cid"] = url_cid
            rec["verdict"]["details_cid"] = m.group(1)
            rec["verdict"]["cid_match"] = url_cid == m.group(1)
    return rec


def _name_match(expected: str, got: str) -> str:
    if not expected:
        return "?（期待値未記入）"
    e = re.sub(r"\s+", "", expected)
    g = re.sub(r"\s+", "", got)
    if e == g:
        return "◯"
    if e in g or g in e:
        return "△（部分一致）"
    return "✗"


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------

def print_record(rec: dict[str, Any]) -> None:
    print(f"\n{'=' * 78}")
    print(f"[{rec['label']}] {rec['input_url']}")
    if rec["expected_name"]:
        print(f"  期待する店名: {rec['expected_name']}")
    print("-" * 78)

    exp = rec["expand"]
    if isinstance(exp, list):
        for a in exp:
            flag = " ⚠ CONSENT" if a["consent"] else ""
            err = f" ERROR={a['error']}" if a["error"] else ""
            print(f"  ② 展開 UA={a['user_agent'][:30]:<30} "
                  f"{a['method']} {a['redirects']}回 {a['elapsed_ms']}ms{flag}{err}")
            print(f"     -> {a['final_url']}")
    elif exp:
        print(f"  ② 展開: {exp}")

    p = rec["parsed"]
    if p:
        print(f"  ③ 系統       : {p['kind']}")
        print(f"     店名       : {p['name']}")
        if p["address_hint"]:
            print(f"     住所片     : {p['address_hint']}")
        print(f"     place_id   : {p['place_id']} ({p['place_id_source']})")
        print(f"     座標(店)   : {p['place_latlng']}   座標(地図中心): {p['viewport_latlng']}")
        print(f"     ftid       : {p['ftid']}   gid: {p['gid']}   cid: {p['cid']}")
        for n in p["notes"]:
            print(f"     ⚠ {n}")

    s = rec["search"]
    if s:
        ids = [x["id"] for x in s["response"].get("places", [])] if s["status"] == 200 else []
        print(f"  ③' searchText: textQuery={s['request']['textQuery']!r} "
              f"radius={s['request'].get('locationBias', {}).get('circle', {}).get('radius')} "
              f"HTTP {s['status']} {s['elapsed_ms']}ms -> {ids}")
        if s["status"] != 200:
            print(f"     {json.dumps(s['response'], ensure_ascii=False)[:400]}")

    for i, d in enumerate(rec["details"]):
        print(f"  ④ Details[{i}] {d['place_id']} HTTP {d['status']} {d['elapsed_ms']}ms")
        r = d["response"]
        if d["status"] != 200:
            print(f"     {json.dumps(r, ensure_ascii=False)[:400]}")
            continue
        print(f"     displayName          : {r.get('displayName', {}).get('text')}")
        print(f"     businessStatus       : {r.get('businessStatus')}")
        print(f"     formattedAddress     : {r.get('formattedAddress')}")
        print(f"     shortFormattedAddress: {r.get('shortFormattedAddress')}")
        print(f"     postalAddress        : {json.dumps(r.get('postalAddress'), ensure_ascii=False)}")
        print(f"     postal_code(component): {_component(r, 'postal_code')}")
        print(f"     nationalPhoneNumber  : {r.get('nationalPhoneNumber')}")
        print(f"     googleMapsUri        : {r.get('googleMapsUri')}")
        wd = (r.get("regularOpeningHours") or {}).get("weekdayDescriptions")
        if wd:
            print("     weekdayDescriptions  :")
            for line in wd:
                print(f"       {line}")
        else:
            print("     weekdayDescriptions  : （無し）")

    v = rec["verdict"]
    if v:
        print(f"  判定: {json.dumps(v, ensure_ascii=False)}")


def _component(r: dict[str, Any], t: str) -> str | None:
    for c in r.get("addressComponents", []) or []:
        if t in c.get("types", []):
            return c.get("longText")
    return None


def print_summary(records: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 78}\n① 正解率（先頭1件が、そのURLの店だったか）\n{'-' * 78}")
    ok = total = 0
    for rec in records:
        v = rec["verdict"]
        m = v.get("name_match", "—")
        if m in ("◯", "△（部分一致）"):
            ok += 1
        if "name_match" in v:
            total += 1
        dist = v.get("distance_m")
        dist_s = f"{dist}m" if dist is not None else "—"
        cid = v.get("cid_match")
        cid_s = {True: "CID一致", False: "⚠CID不一致", None: "CID—"}[cid]
        print(f"  {rec['label']:<4} {m:<8} 距離={dist_s:<8} {cid_s:<12} "
              f"期待={rec['expected_name'] or '—'} / 実際={v.get('got_name') or v.get('outcome') or '—'}")
    if total:
        print(f"\n  一致: {ok}/{total}")
    else:
        print("\n  （Places API を叩いていないため未判定）")


# --------------------------------------------------------------------------
# 入力
# --------------------------------------------------------------------------

def read_urls() -> list[tuple[str, str, str]]:
    """urls.txt を読む。1行 `分類<TAB>URL<TAB>期待する店名`。"""
    if not URLS_FILE.exists():
        sys.exit(f"{URLS_FILE} が無い")
    rows = []
    for raw in URLS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [c.strip() for c in raw.split("\t")]
        parts += [""] * (3 - len(parts))
        label, url, expected = parts[0], parts[1], parts[2]
        if not url:
            print(f"  (skip) {label}: URL未記入", file=sys.stderr)
            continue
        rows.append((label, url, expected))
    return rows


def probe_fields(key: str, place_id: str) -> None:
    """⑥ movedPlace 等が実在するかを、1フィールドずつ確かめる（1回だけ実行する用）。"""
    print(f"実験フィールドの実在確認: place_id={place_id}")
    with httpx.Client(timeout=20.0) as client:
        for f in EXPERIMENTAL_FIELDS:
            d = get_details(client, key, place_id, ["id", f])
            mark = "OK " if d["status"] == 200 else "NG "
            body = json.dumps(d["response"], ensure_ascii=False)
            print(f"  {mark} {f:<16} HTTP {d['status']}  {body[:220]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="GoogleマップURL -> 店舗情報 PoC")
    ap.add_argument("url", nargs="?", help="URL 1本だけ試す（省略時 urls.txt）")
    ap.add_argument("--dry-run", action="store_true",
                    help="Places API を叩かない（展開とパースまで。無課金）")
    ap.add_argument("--no-expand", action="store_true", help="短縮URLの展開もしない")
    ap.add_argument("--ua", choices=["both", "none", "browser"], default="both",
                    help="展開時の User-Agent（既定: 両方試して比べる）")
    ap.add_argument("--radius", type=float, default=200.0, help="locationBias の半径m（逃げ道ア）")
    ap.add_argument("--query", choices=["name", "full"], default="name",
                    help="textQuery に店名だけ／place セグメント全体（逃げ道イ）")
    ap.add_argument("--candidates", type=int, default=1,
                    help="searchText の maxResultCount（逃げ道ウ）⚠ 件数分 Enterprise 課金")
    ap.add_argument("--with-experimental", action="store_true",
                    help="FieldMask に movedPlace 等を足す（400 なら自動で外して再試行）")
    ap.add_argument("--probe-fields", metavar="PLACE_ID",
                    help="実験フィールドの実在だけ確かめて終了")
    args = ap.parse_args()

    key = load_api_key()

    if args.probe_fields:
        if not key:
            sys.exit("APIキーが無い（.env の GOOGLE_MAPS_API_KEY）")
        probe_fields(key, args.probe_fields)
        return

    if not args.dry_run and not key:
        print("⚠ APIキーが無い（.env の GOOGLE_MAPS_API_KEY）。--dry-run 相当で進む\n",
              file=sys.stderr)
        args.dry_run = True

    rows = [("ARG", args.url, "")] if args.url else read_urls()
    if not rows:
        sys.exit("処理するURLが無い（urls.txt が空）")

    mode = "dry-run（Places API を叩かない）" if args.dry_run else "本番（Places API を叩く）"
    print(f"モード: {mode} / radius={args.radius}m / query={args.query} / "
          f"candidates={args.candidates} / 件数={len(rows)}")

    records = []
    for label, url, expected in rows:
        rec = process(url, label, expected, args, key)
        records.append(rec)
        print_record(rec)

    print_summary(records)

    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"{ts}{'-dryrun' if args.dry_run else ''}.json"
    out.write_text(
        json.dumps(
            {
                "run_at": ts,
                "options": vars(args),
                "search_field_mask": SEARCH_FIELD_MASK,
                "details_fields": DETAILS_FIELDS,
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n生データ: {out}")


if __name__ == "__main__":
    main()
