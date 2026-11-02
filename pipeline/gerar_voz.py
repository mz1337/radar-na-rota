#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radar na Rota: gera as falas dos alertas com uma voz neural gratuita e de código aberto
(Piper TTS, roda offline, sem chave de API) e grava em MP3 com um manifest.json.

Roda no GitHub Actions a cada atualização. Para trocar a voz, mude VOZ abaixo ou passe --voz.
Vozes em português do Brasil do Piper: pt_BR-faber-medium, pt_BR-cadu-medium, pt_BR-jeff-medium.

Uso local:  pip install piper-tts  &&  python pipeline/gerar_voz.py --saida app/audio
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import wave

VOZ = "pt_BR-faber-medium"
HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/{nome}/{qual}/{voz}.onnx"
VELOCIDADE = 0.95  # <1 fala um pouco mais rápido (length_scale do Piper)

TIPOS = {
    "vel": ("Radar", "o"),
    "sem": ("Radar de semáforo", "o"),
    "lom": ("Lombada eletrônica", "a"),
    "med": ("Controle de velocidade média", "o"),
}
NUM = {20: "vinte", 30: "trinta", 40: "quarenta", 50: "cinquenta", 60: "sessenta", 70: "setenta",
       80: "oitenta", 90: "noventa", 100: "cem", 110: "cento e dez", 120: "cento e vinte"}
LIMITES = [0] + sorted(NUM)


def frases() -> dict:
    f = {}
    for t, (nome, g) in TIPOS.items():
        for v in LIMITES:
            lim = f" Limite {NUM[v]}." if v else ""
            f[f"a1-{t}-{v}"] = f"{nome} à frente.{lim}"
            f[f"a2-{t}-{v}"] = f"Atenção! {nome} próxim{g}.{lim}"
    f["reduza"] = "Reduza a velocidade."
    f["ligados"] = "Alertas de radar ligados. Boa viagem."
    f["chegou"] = "Você chegou ao destino."
    f["saiu"] = "Você saiu da rota."
    f["teste"] = "Teste de som. Radar à frente. Limite sessenta."
    return f


def baixar_modelo(voz: str, pasta: str) -> str:
    _, nome, qual = voz.split("-", 2)
    os.makedirs(pasta, exist_ok=True)
    onnx = os.path.join(pasta, voz + ".onnx")
    for url, dest in ((HF.format(nome=nome, qual=qual, voz=voz), onnx),
                      (HF.format(nome=nome, qual=qual, voz=voz) + ".json", onnx + ".json")):
        if not os.path.exists(dest) or os.path.getsize(dest) < 100:
            print("baixando", url, flush=True)
            req = urllib.request.Request(url, headers={"User-Agent": "RadarNaRota/2.0"})
            with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as fh:
                shutil.copyfileobj(r, fh)
    return onnx


def sintetizador(onnx: str):
    """Devolve uma função (texto, caminho_wav). Funciona com piper-tts 1.2 e 1.3+."""
    try:
        from piper import PiperVoice  # type: ignore
        voz = PiperVoice.load(onnx)
        cfg = None
        try:
            from piper import SynthesisConfig  # type: ignore  (1.3+)
            cfg = SynthesisConfig(length_scale=VELOCIDADE)
        except Exception:  # noqa: BLE001
            pass

        def falar(texto: str, wav_path: str) -> None:
            with wave.open(wav_path, "wb") as wf:
                if hasattr(voz, "synthesize_wav"):
                    if cfg is not None:
                        voz.synthesize_wav(texto, wf, syn_config=cfg)
                    else:
                        voz.synthesize_wav(texto, wf)
                else:
                    voz.synthesize(texto, wf, length_scale=VELOCIDADE)
        return falar
    except Exception as e:  # noqa: BLE001
        print("API do piper indisponível, usando a linha de comando:", e, flush=True)

        def falar_cli(texto: str, wav_path: str) -> None:
            subprocess.run([sys.executable, "-m", "piper", "-m", onnx, "-f", wav_path],
                           input=texto.encode("utf-8"), check=True)
        return falar_cli


def para_mp3(wav: str, mp3: str) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav,
                        "-af", "silenceremove=start_periods=1:start_threshold=-50dB,loudnorm=I=-14:TP=-1.5:LRA=7,apad=pad_dur=0.12",
                        "-ac", "1", "-ar", "24000", "-b:a", "64k", mp3])
    return r.returncode == 0 and os.path.exists(mp3)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gera as falas dos alertas do Radar na Rota.")
    ap.add_argument("--saida", required=True)
    ap.add_argument("--voz", default=VOZ)
    ap.add_argument("--cache", default=None, help="pasta para guardar modelo e falas entre execuções")
    a = ap.parse_args(argv)

    fr = frases()
    assinatura = hashlib.sha1(json.dumps([a.voz, VELOCIDADE, fr], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
    cache = a.cache or tempfile.mkdtemp()
    pronto = os.path.join(cache, "audio-" + assinatura)
    if os.path.exists(os.path.join(pronto, "manifest.json")):
        print("falas já geradas antes, reaproveitando", flush=True)
    else:
        onnx = baixar_modelo(a.voz, os.path.join(cache, "modelo"))
        falar = sintetizador(onnx)
        tmp = pronto + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        arquivos = {}
        for k, texto in fr.items():
            wav = os.path.join(tmp, k + ".wav")
            falar(texto, wav)
            mp3 = os.path.join(tmp, k + ".mp3")
            if para_mp3(wav, mp3):
                os.remove(wav)
                arquivos[k] = f"{k}.mp3?v={assinatura}"
            else:
                arquivos[k] = f"{k}.wav?v={assinatura}"
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"voz": a.voz, "motor": "Piper TTS", "versao": assinatura, "arquivos": arquivos, "frases": fr},
                      fh, ensure_ascii=False)
        shutil.rmtree(pronto, ignore_errors=True)
        os.rename(tmp, pronto)
        print(f"{len(arquivos)} falas geradas com a voz {a.voz}", flush=True)

    shutil.rmtree(a.saida, ignore_errors=True)
    shutil.copytree(pronto, a.saida)
    total = sum(os.path.getsize(os.path.join(a.saida, f)) for f in os.listdir(a.saida))
    print(f"falas copiadas para {a.saida} ({total / 1024:.0f} KB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
