#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radar na Rota: gera a base única de radares (radares.json).

Junta, nesta ordem de prioridade:
  1. radares que você adicionou nas correções   (pipeline/correcoes/*.json)
  2. ANTT: lista oficial das rodovias federais concedidas
  3. listas extras de prefeituras e DERs          (pipeline/fontes_extras.json)
  4. OpenStreetMap (Overpass)
Depois remove duplicados e os radares que você marcou como inexistentes.
Também estima o sentido dos radares da ANTT ("crescente"/"decrescente") a partir dos
quilômetros dos radares vizinhos na mesma rodovia.

Se uma fonte falhar, reaproveita os radares dela da base anterior (--anterior).

Uso:
  python pipeline/build_radares.py --saida app/data/radares.json
  python pipeline/build_radares.py --saida site/data/radares.json \
      --anterior https://SEU-USUARIO.github.io/radar-na-rota/data/radares.json

Só usa a biblioteca padrão do Python (3.9 ou mais novo).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import io
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
UA = "RadarNaRota/2.0 (gerador da base de radares; github.com)"
BR = dict(s=-34.8, w=-74.2, n=5.4, e=-28.6)
CELL = 0.02
R_TERRA = 6371008.8

ANTT_RESOURCE = "39f9ccb2-86c6-49c9-b30c-77008e305a78"
ANTT_API = "https://dados.antt.gov.br/api/3/action/"
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
Q_FULL = ('[out:json][timeout:240];area(id:3600059470)->.br;'
          'node["highway"="speed_camera"](area.br)->.cams;'
          'rel["type"="enforcement"](area.br)->.enf;'
          'node(r.enf:"device")->.dev;node(r.enf:"from")->.frm;'
          '(.cams;.dev;.frm;);out body qt;.enf out body qt;')
Q_SIMPLE = ('[out:json][timeout:240];area(id:3600059470)->.br;'
            'node["highway"="speed_camera"](area.br);out body qt;')


def log(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------------------- rede
def http_get(url: str, data: bytes | None = None, headers: dict | None = None,
             timeout: int = 180, tries: int = 3) -> bytes:
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:  # noqa: BLE001 (rede instável, timeout etc.)
            last = e
        time.sleep(4 * (i + 1))
    raise RuntimeError(f"{url[:90]}: {last}")


def get_json(url: str, **kw):
    return json.loads(decode(http_get(url, **kw)))


def decode(b: bytes) -> str:
    try:
        return b.decode("utf-8-sig")
    except UnicodeDecodeError:
        return b.decode("cp1252", errors="replace")


# ----------------------------------------------------------------------------- geometria
D2R = math.pi / 180


def hav(a1, o1, a2, o2):
    dla, dlo = (a2 - a1) * D2R, (o2 - o1) * D2R
    s = math.sin(dla / 2) ** 2 + math.cos(a1 * D2R) * math.cos(a2 * D2R) * math.sin(dlo / 2) ** 2
    return 2 * R_TERRA * math.asin(min(1, math.sqrt(s)))


def bearing(a1, o1, a2, o2):
    y = math.sin((o2 - o1) * D2R) * math.cos(a2 * D2R)
    x = math.cos(a1 * D2R) * math.sin(a2 * D2R) - math.sin(a1 * D2R) * math.cos(a2 * D2R) * math.cos((o2 - o1) * D2R)
    return (math.atan2(y, x) / D2R + 360) % 360


def ang_diff(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


# ----------------------------------------------------------------------------- leitura de valores
def num(v):
    if v is None:
        return math.nan
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"\s", "", str(v))
    if not t:
        return math.nan
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+,\d+", t):
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return math.nan


def fix_deg(v, mx):
    if not math.isfinite(v):
        return math.nan
    g = 0
    while abs(v) > mx and g < 12:
        v /= 10
        g += 1
    return round(v, 7)


def in_br(lat, lon):
    return BR["s"] <= lat <= BR["n"] and BR["w"] <= lon <= BR["e"]


def norm_coords(lat, lon):
    lat, lon = fix_deg(num(lat), 90), fix_deg(num(lon), 180)
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if in_br(lat, lon):
        return lat, lon
    if in_br(lon, lat):
        return lon, lat
    return None


def parse_speed(s) -> int:
    if s is None or s == "":
        return 0
    if isinstance(s, (int, float)):
        return int(round(s)) if 10 <= s <= 140 else 0
    m = re.search(r"(\d{2,3})(?:[.,]\d+)?\s*(mph)?", str(s), re.I)
    if not m:
        return 0
    v = int(m.group(1))
    if m.group(2):
        v = round(v * 1.609)
    return v if 10 <= v <= 140 else 0


def speed_from_text(s) -> int:
    if not s:
        return 0
    s = str(s)
    m = re.search(r"(\d{2,3})\s*km\s*/?\s*h", s, re.I) or \
        re.search(r"(?:vel(?:ocidade)?|limite|radar|max(?:speed)?|@)\D{0,10}?(\d{2,3})\b", s, re.I)
    return parse_speed(m.group(1)) if m else 0


CARD = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5, "S": 180,
        "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
        "L": 90, "O": 270, "NO": 315, "SO": 225, "NNO": 337.5, "SSO": 202.5, "ONO": 292.5, "OSO": 247.5}


def parse_dir(s):
    if s is None or s == "":
        return None
    out = []
    for p in re.split(r"[;,|]", str(s)):
        t = p.strip().upper()
        if not t:
            continue
        if re.fullmatch(r"-?\d+(\.\d+)?", t):
            out.append(round(float(t) % 360))
        elif t in CARD:
            out.append(round(CARD[t]))
    return out or None


def type_from_text(s) -> str:
    s = str(s or "").lower()
    if re.search(r"lombada|redutor|\brev\b", s):
        return "lom"
    if re.search(r"sem[aá]foro|avan[cç]o|sinal vermelho|fotossensor|traffic_signals|red.?light|\bcem\b", s):
        return "sem"
    if re.search(r"m[ée]dia|average|section", s):
        return "med"
    return "vel"


def norm_key(k) -> str:
    k = unicodedata.normalize("NFD", str(k))
    k = "".join(c for c in k if unicodedata.category(c) != "Mn").lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", k).strip("_")


def parse_km(s):
    if s is None:
        return None
    t = re.sub(r"\s", "", str(s))
    if not t:
        return None
    m = re.fullmatch(r"(\d+)\+(\d{1,3})", t)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 1000
    v = num(t)
    if not math.isfinite(v):
        m = re.search(r"\d+(?:[.,]\d+)?", t)
        if not m:
            return None
        v = num(m.group(0))
    if v > 5000:  # veio em metros
        v /= 1000
    return v


def norm_rod(s) -> str:
    t = str(s or "").upper()
    m = re.search(r"\b([A-Z]{2})\s*-?\s*(\d{3})\b", t)
    return f"{m.group(1)}-{m.group(2)}" if m else t.strip()


def parse_sentido(s):
    t = str(s or "").lower()
    if "decresc" in t:
        return "D"
    if "cresc" in t:
        return "C"
    return None


def radar(lat, lon, v=0, t="vel", d=None, info="", vp=0, src="", **extra):
    r = dict(lat=lat, lon=lon, v=v or 0, t=t, dir=d, info=(info or "")[:160], vp=vp or 0, src=src, also=[])
    r.update(extra)
    return r


# ----------------------------------------------------------------------------- ANTT
def parse_antt(records) -> list:
    out = []
    for rec in records or []:
        r = {norm_key(k): v for k, v in rec.items()}
        sit = str(r.get("situacao") or "")
        inat = str(r.get("data_da_inativacao") if r.get("data_da_inativacao") is not None else "").strip()
        if re.search(r"inativ|desativ|deslig|removid|retirad", sit, re.I):
            continue
        if inat and not re.fullmatch(r"(nan|null|none|-|0)", inat, re.I):
            continue
        c = norm_coords(r.get("latitude"), r.get("longitude"))
        if not c:
            continue
        vl, vp = parse_speed(r.get("velocidade_leve")), parse_speed(r.get("velocidade_pesado"))
        rod = norm_rod(r.get("rodovia"))
        uf = str(r.get("uf") or "").strip().upper()
        km_txt = str(r.get("km_m") if r.get("km_m") is not None else "").strip()
        sent = r.get("sentido")
        info = " · ".join(x for x in [
            "/".join(x for x in [rod, uf] if x) + (f" km {km_txt}" if km_txt else ""),
            f"sentido {str(sent).lower()}" if sent else "",
            str(r.get("municipio") or "").strip(),
            str(r.get("concessionaria") or "").strip()] if x)
        out.append(radar(c[0], c[1], vl or vp, type_from_text(r.get("tipo_de_radar")), None, info,
                         vp if (vp and vp != vl) else 0, "antt",
                         rod=rod, uf=uf, km=parse_km(km_txt), sent=parse_sentido(sent)))
    return out


def infer_dir_antt(lst: list) -> int:
    """Estima a direção de tráfego (graus) dos radares com sentido crescente/decrescente.
    O rumo do km crescente sai dos radares vizinhos na mesma rodovia e UF."""
    grupos: dict = {}
    for r in lst:
        if r.get("rod") and r.get("km") is not None and math.isfinite(r["km"]):
            grupos.setdefault(f'{r["rod"]}/{r.get("uf", "")}', []).append(r)
    n = 0
    for g in grupos.values():
        for r in g:
            if r.get("dir") or r.get("sent") not in ("C", "D"):
                continue
            lo = hi = None
            for q in g:
                if q is r:
                    continue
                dk = q["km"] - r["km"]
                adk = abs(dk)
                if adk < 0.3 or adk > 40:
                    continue
                d = hav(r["lat"], r["lon"], q["lat"], q["lon"])
                if d < 200 or d > adk * 1500 + 1500:
                    continue
                if dk > 0 and (hi is None or dk < hi[1]):
                    hi = (q, dk)
                if dk < 0 and (lo is None or adk < lo[1]):
                    lo = (q, adk)
            bs = []
            if hi:
                bs.append((bearing(r["lat"], r["lon"], hi[0]["lat"], hi[0]["lon"]), 1 / hi[1]))
            if lo:
                bs.append((bearing(lo[0]["lat"], lo[0]["lon"], r["lat"], r["lon"]), 1 / lo[1]))
            if not bs:
                continue
            if len(bs) == 2 and ang_diff(bs[0][0], bs[1][0]) > 90:
                h = max(bs, key=lambda b: b[1])[0]
            else:
                x = sum(math.cos(b * D2R) * w for b, w in bs)
                y = sum(math.sin(b * D2R) * w for b, w in bs)
                h = (math.atan2(y, x) / D2R + 360) % 360
            r["dir"] = [round(h if r["sent"] == "C" else (h + 180) % 360) % 360]
            n += 1
    return n


def fonte_antt() -> list:
    recs = None
    try:
        j = get_json(f"{ANTT_API}datastore_search?resource_id={ANTT_RESOURCE}&limit=32000")
        recs = j["result"]["records"]
        total = j["result"].get("total", len(recs))
        while len(recs) < total:
            j = get_json(f"{ANTT_API}datastore_search?resource_id={ANTT_RESOURCE}&limit=32000&offset={len(recs)}")
            more = j["result"]["records"]
            if not more:
                break
            recs += more
    except Exception as e:  # noqa: BLE001
        log(f"  API da ANTT falhou ({e}); tentando o CSV")
        j = get_json(f"{ANTT_API}resource_show?id={ANTT_RESOURCE}")
        recs = rows_to_objects(parse_csv(decode(http_get(j["result"]["url"]))))
    lst = parse_antt(recs)
    n = infer_dir_antt(lst)
    log(f"  sentido estimado para {n} de {len(lst)} radares da ANTT")
    return lst


# ----------------------------------------------------------------------------- OpenStreetMap
def parse_overpass(js) -> list:
    nodes, rels = {}, []
    for e in js.get("elements", []):
        if e.get("type") == "node":
            nodes[e["id"]] = e
        elif e.get("type") == "relation":
            rels.append(e)
    rel_of = {}
    for r in rels:
        devs = [m["ref"] for m in r.get("members", []) if m.get("type") == "node" and m.get("role") == "device"]
        froms = [m["ref"] for m in r.get("members", []) if m.get("type") == "node" and m.get("role") == "from"]
        for d in devs:
            rel_of[d] = (r.get("tags", {}), froms[0] if froms else None)
    out = []
    for n in nodes.values():
        tg = n.get("tags", {})
        rel = rel_of.get(n["id"])
        if tg.get("highway") != "speed_camera" and not rel:
            continue
        lat, lon = n.get("lat"), n.get("lon")
        if lat is None or not in_br(lat, lon):
            continue
        rt = rel[0] if rel else {}
        enf = str(rt.get("enforcement") or tg.get("enforcement") or "")
        if "traffic_signals" in enf or "traffic_signals" in str(tg.get("speed_camera") or ""):
            t = "sem"
        elif "average_speed" in enf:
            t = "med"
        else:
            t = type_from_text(" ".join(str(tg.get(k) or "") for k in ("speed_camera", "description", "name", "note")))
        d = parse_dir(tg.get("direction") or tg.get("camera:direction"))
        if not d and rel and rel[1] in nodes:
            f = nodes[rel[1]]
            d = [round(bearing(f["lat"], f["lon"], lat, lon))]
        out.append(radar(lat, lon, parse_speed(tg.get("maxspeed") or rt.get("maxspeed")) or speed_from_text(tg.get("name")),
                         t, d, str(tg.get("name") or tg.get("description") or "")[:120],
                         parse_speed(tg.get("maxspeed:hgv") or rt.get("maxspeed:hgv")), "osm"))
    return out


def fonte_osm() -> list:
    ultimo = None
    for q in (Q_FULL, Q_SIMPLE):
        for ep in OVERPASS:
            try:
                body = urllib.parse.urlencode({"data": q}).encode()
                js = json.loads(decode(http_get(ep, data=body, timeout=300, tries=2,
                                                headers={"Content-Type": "application/x-www-form-urlencoded"})))
                lst = parse_overpass(js)
                if lst:
                    return lst
                ultimo = "resposta sem radares"
            except Exception as e:  # noqa: BLE001
                ultimo = e
                log(f"  {ep.split('/')[2]} falhou: {e}")
    raise RuntimeError(f"Overpass indisponível ({ultimo})")


# ----------------------------------------------------------------------------- listas genéricas
def parse_csv(text: str) -> list:
    text = text.lstrip("﻿")
    first = text.split("\n", 1)[0]
    delim = ";" if ";" in first else "\t" if "\t" in first else "|" if ("|" in first and "," not in first) else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    return [r for r in rows if any(str(c).strip() for c in r)]


def rows_to_objects(rows: list) -> list:
    if not rows:
        return []
    h = rows[0]
    return [{h[i]: (r[i] if i < len(r) else "") for i in range(len(h))} for r in rows[1:]]


NOME_RE = re.compile(r"^(nome|name|descricao|description|desc|local|localizacao|endereco|logradouro|numero|bairro|referencia)$")


def rows_to_radars(rows: list, src: str) -> list:
    if not rows:
        return []
    head = [norm_key(h) for h in rows[0]]

    def f(rx):
        for i, h in enumerate(head):
            if re.search(rx, h):
                return i
        return -1

    i_lat = f(r"^(lat|latitude|y|coord_?y|lat_?dd)$")
    i_lon = f(r"^(lon|lng|long|longitude|x|coord_?x|lon_?dd)$")
    if i_lat < 0:
        i_lat = f(r"^lat")
    if i_lon < 0:
        i_lon = f(r"^(lon|lng)")
    i_v = f(r"^(velocidade|velocidade_leve|vel|speed|maxspeed|limite|velocidade_maxima|vel_max|limite_de_velocidade|velocidade_permitida)$")
    if i_v < 0:
        i_v = f(r"veloc|speed|limite")
    i_t = f(r"^(tipo|subtipo|tipo_de_radar|tipo_equipamento|type|categoria|equipamento)$")
    i_d = f(r"^(direction|direcao|heading|bearing|azimute|angulo)$")
    i_s = f(r"^(situacao|status)$")
    prio = ["nome", "name", "logradouro", "endereco", "local", "numero", "localizacao", "bairro",
            "referencia", "descricao", "description", "desc"]
    nomes = sorted((i for i, h in enumerate(head) if NOME_RE.match(h)), key=lambda i: prio.index(head[i]))
    start = 1
    if i_lat < 0 or i_lon < 0:
        start = 0
        a, b = num(rows[0][0]), num(rows[0][1]) if len(rows[0]) > 1 else math.nan
        if not (math.isfinite(a) and math.isfinite(b)):
            raise ValueError("não encontrei colunas de latitude e longitude")
        i_lon, i_lat = 0, 1
        if a > -28.6 or b < -34.9:
            i_lat, i_lon = 0, 1
        nomes = [2] if len(rows[0]) > 2 else []
        i_v = i_t = -1
    out = []
    for row in rows[start:]:
        g = lambda i: (row[i] if 0 <= i < len(row) else "")  # noqa: E731
        if i_s >= 0 and re.search(r"inativ|desativ|deslig", str(g(i_s)), re.I):
            continue
        c = norm_coords(g(i_lat), g(i_lon))
        if not c:
            continue
        nome = ", ".join(dict.fromkeys(str(g(i)).strip() for i in nomes if str(g(i)).strip()))
        v = (parse_speed(g(i_v)) if i_v >= 0 else 0) or speed_from_text(nome)
        out.append(radar(c[0], c[1], v, type_from_text(f"{g(i_t) if i_t >= 0 else ''} {nome}"),
                         parse_dir(g(i_d)) if i_d >= 0 else None, nome, 0, src))
    return out


def parse_geojson(js, src: str) -> list:
    feats = js.get("features") if isinstance(js, dict) and js.get("type") == "FeatureCollection" else (js if isinstance(js, list) else [js])
    out = []
    for ft in feats or []:
        g = (ft or {}).get("geometry") or {}
        if g.get("type") != "Point" or not g.get("coordinates"):
            continue
        pr = {norm_key(k): v for k, v in (ft.get("properties") or {}).items()}
        c = norm_coords(g["coordinates"][1], g["coordinates"][0])
        if not c:
            continue
        txt = " ".join(str(pr[k]) for k in ("name", "nome", "description", "descricao", "logradouro", "endereco") if pr.get(k))
        v = parse_speed(pr.get("maxspeed") or pr.get("velocidade") or pr.get("speed") or pr.get("limite") or pr.get("velocidade_leve")) or speed_from_text(txt)
        t = "sem" if pr.get("enforcement") == "traffic_signals" else type_from_text(" ".join(str(pr.get(k) or "") for k in ("tipo", "subtipo", "type", "tipo_de_radar")) + " " + txt)
        if re.search(r"inativ|desativ|deslig", str(pr.get("situacao") or pr.get("status") or ""), re.I):
            continue
        out.append(radar(c[0], c[1], v, t, parse_dir(pr.get("direction") or pr.get("direcao")), txt, 0, src))
    return out


def fonte_extra(cfg: dict) -> list:
    tipo, src = cfg.get("tipo", "csv"), cfg["id"]
    if tipo == "csv":
        return rows_to_radars(parse_csv(decode(http_get(cfg["url"]))), src)
    if tipo == "geojson":
        return parse_geojson(get_json(cfg["url"]), src)
    if tipo == "ckan":
        portal = cfg["portal"].rstrip("/")
        if cfg.get("pacote"):
            pkg = get_json(f"{portal}/api/3/action/package_show?id={urllib.parse.quote(cfg['pacote'])}")["result"]
        else:
            res = get_json(f"{portal}/api/3/action/package_search?rows=5&q={urllib.parse.quote(cfg['busca'])}")["result"]["results"]
            if not res:
                raise RuntimeError("conjunto de dados não encontrado no portal")
            pkg = res[0]
        fmt = cfg.get("formato", "CSV").upper()
        recursos = [r for r in pkg.get("resources", []) if str(r.get("format", "")).upper() == fmt]
        if not recursos:
            raise RuntimeError(f"nenhum arquivo {fmt} no conjunto “{pkg.get('title')}”")
        url = recursos[0]["url"]
        if fmt in ("GEOJSON", "JSON"):
            return parse_geojson(get_json(url), src)
        return rows_to_radars(parse_csv(decode(http_get(url))), src)
    if tipo == "arcgis":
        base, out, off = cfg["url"].rstrip("/"), [], 0
        while True:
            js = get_json(f"{base}/query?where=1%3D1&outFields=*&outSR=4326&f=geojson&resultOffset={off}&resultRecordCount=2000")
            parte = parse_geojson(js, src)
            out += parte
            n = len(js.get("features") or [])
            if n < 2000:
                return out
            off += n
    raise ValueError(f"tipo de fonte desconhecido: {tipo}")


# ----------------------------------------------------------------------------- correções
def carregar_correcoes(pasta: str):
    rem, add = [], []
    for arq in sorted(glob.glob(os.path.join(pasta, "*.json"))):
        try:
            with open(arq, encoding="utf-8") as fh:
                j = json.load(fh)
        except Exception as e:  # noqa: BLE001
            log(f"  correção ignorada ({os.path.basename(arq)}): {e}")
            continue
        for x in j.get("remover", []):
            c = norm_coords(x.get("lat"), x.get("lon"))
            if c:
                rem.append(c)
        for x in j.get("adicionar", []):
            c = norm_coords(x.get("lat"), x.get("lon"))
            if c:
                add.append(radar(c[0], c[1], parse_speed(x.get("v")), x.get("t") if x.get("t") in ("vel", "sem", "lom", "med") else "vel",
                                 parse_dir(x.get("dir") if not isinstance(x.get("dir"), list) else ";".join(map(str, x["dir"]))),
                                 x.get("info") or "Adicionado nas correções", 0, "fix"))
    return rem, add


# ----------------------------------------------------------------------------- junção
def key(i, j):
    return i * 100000 + j


def merge_sources(listas: list) -> list:
    out, g = [], {}
    for lst in listas:
        for r in lst:
            i0, j0 = math.floor(r["lat"] / CELL), math.floor(r["lon"] / CELL)
            dup = None
            for i in (i0 - 1, i0, i0 + 1):
                for j in (j0 - 1, j0, j0 + 1):
                    for ix in g.get(key(i, j), ()):
                        q = out[ix]
                        if q["src"] == r["src"]:
                            continue
                        if q["dir"] and r["dir"] and all(ang_diff(x, y) > 90 for x in q["dir"] for y in r["dir"]):
                            continue
                        d = hav(q["lat"], q["lon"], r["lat"], r["lon"])
                        if d < 20 or (d < 40 and q["t"] == r["t"]):
                            dup = q
                            break
                    if dup:
                        break
                if dup:
                    break
            if dup:
                if not dup["v"] and r["v"]:
                    dup["v"] = r["v"]
                if not dup["dir"] and r["dir"]:
                    dup["dir"] = r["dir"]
                if not dup["info"] and r["info"]:
                    dup["info"] = r["info"]
                if r["src"] not in dup["also"] and r["src"] != dup["src"]:
                    dup["also"].append(r["src"])
                continue
            g.setdefault(key(i0, j0), []).append(len(out))
            nr = dict(r)
            nr["also"] = []
            out.append(nr)
    return out


def aplicar_remocoes(lst: list, rem: list, raio=30) -> tuple:
    if not rem:
        return lst, 0
    g = {}
    for lat, lon in rem:
        g.setdefault(key(math.floor(lat / CELL), math.floor(lon / CELL)), []).append((lat, lon))
    out, n = [], 0
    for r in lst:
        if r["src"] == "fix":  # o que você adicionou nunca é removido pelas suas próprias remoções
            out.append(r)
            continue
        i0, j0 = math.floor(r["lat"] / CELL), math.floor(r["lon"] / CELL)
        perto = any(hav(r["lat"], r["lon"], a, b) <= raio
                    for i in (i0 - 1, i0, i0 + 1) for j in (j0 - 1, j0, j0 + 1) for a, b in g.get(key(i, j), ()))
        if perto:
            n += 1
        else:
            out.append(r)
    return out, n


# ----------------------------------------------------------------------------- base anterior
def carregar_anterior(ref: str | None) -> dict:
    """Devolve {fonte: [radares]} da base publicada antes, para reaproveitar fontes que falharem."""
    if not ref:
        return {}
    try:
        if re.match(r"https?://", ref):
            js = get_json(ref, tries=1, timeout=60)
        else:
            with open(ref, encoding="utf-8") as fh:
                js = json.load(fh)
    except Exception as e:  # noqa: BLE001
        log(f"Base anterior indisponível ({e}); seguindo sem ela.")
        return {}
    por_fonte: dict = {}
    for a in js.get("radares", []):
        srcs = str(a[7] if len(a) > 7 else "osm").split("+")
        r = radar(a[0], a[1], a[2] or 0, a[3] or "vel", a[4] or None, a[5] or "", a[6] or 0, srcs[0])
        por_fonte.setdefault(srcs[0], []).append(r)
    log(f"Base anterior carregada: {sum(len(v) for v in por_fonte.values())} radares de {js.get('gerado_em', '?')}")
    return por_fonte


# ----------------------------------------------------------------------------- principal
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gera a base única de radares do Radar na Rota.")
    ap.add_argument("--saida", default=os.path.join(AQUI, "..", "app", "data", "radares.json"))
    ap.add_argument("--anterior", help="URL ou arquivo da base publicada antes (usada se uma fonte falhar)")
    ap.add_argument("--extras", default=os.path.join(AQUI, "fontes_extras.json"))
    ap.add_argument("--correcoes", default=os.path.join(AQUI, "correcoes"))
    ap.add_argument("--pular", action="append", default=[], help="pular uma fonte: antt, osm ou o id de uma extra")
    a = ap.parse_args(argv)

    anterior = carregar_anterior(a.anterior)
    fontes: dict = {}
    listas: dict = {}

    def rodar(fid, nome, fn):
        if fid in a.pular:
            return
        log(f"→ {nome}")
        t0 = time.time()
        try:
            lst = fn()
            if not lst:
                raise RuntimeError("nenhum radar com coordenadas")
            listas[fid] = lst
            fontes[fid] = dict(nome=nome, ok=True, n=len(lst), erro=None, reaproveitado=False)
            log(f"  {len(lst)} radares em {time.time() - t0:.0f}s")
        except Exception as e:  # noqa: BLE001
            velho = anterior.get(fid, [])
            listas[fid] = velho
            fontes[fid] = dict(nome=nome, ok=False, n=len(velho), erro=str(e)[:200], reaproveitado=bool(velho))
            log(f"  FALHOU: {e}" + (f" (reaproveitando {len(velho)} da base anterior)" if velho else ""))

    rodar("antt", "ANTT · rodovias federais concedidas", fonte_antt)
    extras = []
    if os.path.exists(a.extras):
        with open(a.extras, encoding="utf-8") as fh:
            extras = [x for x in json.load(fh).get("fontes", []) if x.get("ativo", True)]
    for cfg in extras:
        rodar(cfg["id"], cfg.get("nome", cfg["id"]), lambda cfg=cfg: fonte_extra(cfg))
    rodar("osm", "OpenStreetMap", fonte_osm)

    rem, add = carregar_correcoes(a.correcoes)
    ordem = [add, listas.get("antt", [])] + [listas.get(c["id"], []) for c in extras] + [listas.get("osm", [])]
    final = merge_sources(ordem)
    final, n_rem = aplicar_remocoes(final, rem)
    final.sort(key=lambda r: (round(r["lat"], 2), r["lon"]))
    if add or rem:
        fontes["fix"] = dict(nome="Suas correções", ok=True, n=len(add), removidos=n_rem, erro=None, reaproveitado=False)

    if not final:
        log("Nenhum radar obtido. A base não foi gerada.")
        return 1

    agora = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    saida = {
        "versao": 1,
        "gerado_em": agora,
        "fontes": fontes,
        "campos": ["lat", "lon", "v", "t", "dir", "info", "vp", "fontes"],
        "radares": [[round(r["lat"], 6), round(r["lon"], 6), r["v"] or 0, r["t"], r["dir"] or 0, r["info"] or 0,
                     r["vp"] or 0, "+".join([r["src"]] + r["also"])] for r in final],
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.saida)), exist_ok=True)
    with open(a.saida, "w", encoding="utf-8") as fh:
        json.dump(saida, fh, ensure_ascii=False, separators=(",", ":"))

    com_dir = sum(1 for r in final if r["dir"])
    com_v = sum(1 for r in final if r["v"])
    log(f"\n{len(final)} radares gravados em {a.saida} ({os.path.getsize(a.saida) / 1024:.0f} KB)")
    log(f"  com limite conhecido: {com_v} · com sentido: {com_dir} · removidos pelas correções: {n_rem}")
    resumo = os.environ.get("GITHUB_STEP_SUMMARY")
    if resumo:
        with open(resumo, "a", encoding="utf-8") as fh:
            fh.write(f"### Base de radares: {len(final)} radares\n\n| Fonte | Radares | Situação |\n|---|---:|---|\n")
            for f in fontes.values():
                sit = "ok" if f["ok"] else (f"falhou, reaproveitada a base anterior: {f['erro']}" if f["reaproveitado"] else f"falhou: {f['erro']}")
                fh.write(f"| {f['nome']} | {f['n']} | {sit} |\n")
            fh.write(f"\nCom limite conhecido: {com_v} · com sentido: {com_dir} · removidos pelas correções: {n_rem}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
