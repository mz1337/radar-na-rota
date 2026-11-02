#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gera amostras de voz para escolher a voz dos alertas (roda no GitHub Actions).
- Chatterbox Multilingual (Resemble AI, licença MIT): voz feminina e masculina, 2 variações cada.
- Piper (voz atual) para comparação.
Saída: pasta com MP3s e um index.html para ouvir no navegador.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
import wave

FRASES = [
    "Radar à frente. Limite sessenta.",
    "Atenção! Lombada eletrônica próxima. Limite quarenta. Reduza a velocidade.",
    "Alertas de radar ligados. Boa viagem.",
]
REF_MASC = "https://storage.googleapis.com/chatterbox-demo-samples/mtl_prompts/pt_m1.flac"


def log(*a):
    print(*a, flush=True)


def mp3(wav_path: str, mp3_path: str) -> str:
    if shutil.which("ffmpeg"):
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path, "-af", "loudnorm=I=-14:TP=-1.5",
                            "-ac", "1", "-ar", "24000", "-b:a", "96k", mp3_path])
        if r.returncode == 0:
            os.remove(wav_path)
            return os.path.basename(mp3_path)
    return os.path.basename(wav_path)


def chatterbox(saida: str) -> list:
    import torch
    import torchaudio as ta
    _load = torch.load
    torch.load = lambda *a, **k: _load(*a, **{**k, "map_location": k.get("map_location", "cpu")})  # checkpoints salvos em GPU
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    t0 = time.time()
    model = ChatterboxMultilingualTTS.from_pretrained(device="cpu")
    log(f"Chatterbox carregado em {time.time() - t0:.0f}s")
    ref = os.path.join(saida, "_ref_masc.flac")
    urllib.request.urlretrieve(REF_MASC, ref)

    vozes = [
        ("chatterbox-feminina-a", "Feminina A", None, 0.5, 0.5),
        ("chatterbox-feminina-b", "Feminina B (sotaque mais neutro)", None, 0.0, 0.5),
        ("chatterbox-masculina-a", "Masculina A", ref, 0.5, 0.5),
        ("chatterbox-masculina-b", "Masculina B (sotaque mais neutro)", ref, 0.0, 0.5),
    ]
    feitos = []
    for chave, nome, prompt, cfg, exag in vozes:
        try:
            t0 = time.time()
            partes = []
            for fr in FRASES:
                kw = {"language_id": "pt", "cfg_weight": cfg, "exaggeration": exag}
                if prompt:
                    kw["audio_prompt_path"] = prompt
                w = model.generate(fr, **kw)
                partes.append(w)
                partes.append(torch.zeros(1, int(model.sr * 0.6)))
            wav = os.path.join(saida, chave + ".wav")
            ta.save(wav, torch.cat(partes, dim=1), model.sr)
            arq = mp3(wav, os.path.join(saida, chave + ".mp3"))
            feitos.append({"arquivo": arq, "nome": nome, "motor": "Chatterbox Multilingual",
                           "segundos": round(time.time() - t0)})
            log(f"{nome}: ok em {time.time() - t0:.0f}s")
        except Exception:  # noqa: BLE001
            log(f"{nome}: FALHOU\n{traceback.format_exc()}")
    os.remove(ref)
    return feitos


def piper(saida: str) -> list:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gerar_voz as g
    onnx = g.baixar_modelo(g.VOZ, os.path.join(saida, "_piper"))
    falar = g.sintetizador(onnx)
    frames, params = [], None
    for fr in FRASES:
        tmp = os.path.join(saida, "_p.wav")
        falar(fr, tmp)
        with wave.open(tmp) as w:
            params = w.getparams()
            frames.append(w.readframes(w.getnframes()))
            frames.append(b"\x00\x00" * int(w.getframerate() * 0.6))
        os.remove(tmp)
    wav = os.path.join(saida, "piper-atual.wav")
    with wave.open(wav, "wb") as w:
        w.setparams(params)
        w.writeframes(b"".join(frames))
    shutil.rmtree(os.path.join(saida, "_piper"), ignore_errors=True)
    return [{"arquivo": mp3(wav, os.path.join(saida, "piper-atual.mp3")), "nome": "Voz atual (Piper)", "motor": "Piper TTS"}]


def pagina(saida: str, itens: list) -> None:
    linhas = "\n".join(
        f'<li><b>{html.escape(i["nome"])}</b><small>{html.escape(i["motor"])}</small>'
        f'<audio controls preload="none" src="{html.escape(i["arquivo"])}"></audio></li>' for i in itens)
    txt = " / ".join(FRASES)
    doc = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Amostras de voz · Radar na Rota</title>
<style>:root{{color-scheme:light dark;--g:#00704a}}body{{margin:0;font:16px/1.45 system-ui,Roboto,sans-serif;background:Canvas;color:CanvasText}}
main{{max-width:560px;margin:0 auto;padding:20px 16px 40px}}h1{{font-size:22px;margin:0 0 6px}}p{{opacity:.75;margin:0 0 18px}}
ul{{list-style:none;padding:0;margin:0;display:grid;gap:12px}}li{{border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:14px;padding:14px;display:grid;gap:6px}}
small{{opacity:.65}}audio{{width:100%}}</style></head><body><main>
<h1>Amostras de voz</h1><p>Frases: “{html.escape(txt)}”</p><ul>{linhas}</ul></main></body></html>"""
    with open(os.path.join(saida, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(doc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default="amostras")
    a = ap.parse_args()
    shutil.rmtree(a.saida, ignore_errors=True)
    os.makedirs(a.saida)
    itens = []
    try:
        itens += chatterbox(a.saida)
    except Exception:  # noqa: BLE001
        log("Chatterbox indisponível:\n" + traceback.format_exc())
    try:
        itens += piper(a.saida)
    except Exception:  # noqa: BLE001
        log("Piper indisponível:\n" + traceback.format_exc())
    pagina(a.saida, itens)
    with open(os.path.join(a.saida, "amostras.json"), "w", encoding="utf-8") as fh:
        json.dump(itens, fh, ensure_ascii=False, indent=1)
    log(f"{len(itens)} amostras prontas")
    return 0 if itens else 1


if __name__ == "__main__":
    sys.exit(main())
