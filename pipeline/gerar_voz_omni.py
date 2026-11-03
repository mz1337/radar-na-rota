#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radar na Rota: gera as 101 falas dos alertas com o OmniVoice (motor padrão do VoiceStudio),
clonando uma voz de referência aprovada (vozes/<id>/ref.wav ou amostras/ref-<id>.wav).

Roda no GitHub Actions (CPU). Se a pasta de saída já tiver um manifest.json com a mesma
assinatura (frases + referência + parâmetros), não gera nada de novo.

Uso: python pipeline/gerar_voz_omni.py --voz omni-masculina-c --ref amostras/ref-omni-masculina-c.wav --saida vozes/omni-masculina-c
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gerar_voz as g  # noqa: E402  (mesma lista de frases da voz Piper)

SR = 24000
REF_TXT = "Olá! Eu vou te avisar sempre que houver um radar no seu caminho."
PASSOS = 32
LOTE = 6

VOZES = {
    "omni-feminina-a": ("Feminina A · jovem", "f"),
    "omni-feminina-b": ("Feminina B · grave", "f"),
    "omni-feminina-c": ("Feminina C · sotaque BR", "f"),
    "omni-masculina-a": ("Masculina A · jovem", "m"),
    "omni-masculina-b": ("Masculina B · grave", "m"),
    "omni-masculina-c": ("Masculina C · sotaque BR", "m"),
}


def log(*a):
    print(*a, flush=True)


def ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def salvar_wav(caminho: str, audio) -> None:
    a = np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1, 1)
    with wave.open(caminho, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((a * 32767).astype("<i2").tobytes())


def para_mp3(exe: str | None, wav: str, mp3: str) -> bool:
    if not exe:
        return False
    r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", wav, "-af",
                        "silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
                        "silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
                        "loudnorm=I=-14:TP=-1.5:LRA=7,apad=pad_dur=0.12",
                        "-ac", "1", "-ar", "24000", "-b:a", "64k", mp3])
    return r.returncode == 0 and os.path.exists(mp3)


def para_np(x) -> np.ndarray:
    if hasattr(x, "detach"):
        x = x.detach().cpu().float().numpy()
    return np.asarray(x, dtype=np.float32).reshape(-1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voz", required=True, choices=sorted(VOZES))
    ap.add_argument("--ref", required=True)
    ap.add_argument("--saida", required=True)
    a = ap.parse_args(argv)

    fr = g.frases()
    with open(a.ref, "rb") as fh:
        ref_hash = hashlib.sha1(fh.read()).hexdigest()
    assinatura = hashlib.sha1(json.dumps([a.voz, ref_hash, REF_TXT, PASSOS, fr], ensure_ascii=False,
                                         sort_keys=True).encode()).hexdigest()[:12]
    man = os.path.join(a.saida, "manifest.json")
    if os.path.exists(man):
        try:
            with open(man, encoding="utf-8") as fh:
                if json.load(fh).get("versao") == assinatura:
                    log("falas já geradas com esta voz, nada a fazer")
                    return 0
        except Exception:  # noqa: BLE001
            pass

    import torch
    from omnivoice import OmniVoice

    torch.set_num_threads(os.cpu_count() or 4)
    t0 = time.time()
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cpu", dtype=torch.float32)
    prompt = model.create_voice_clone_prompt(ref_audio=a.ref, ref_text=REF_TXT)
    log(f"OmniVoice pronto em {time.time() - t0:.0f}s")

    exe = ffmpeg()
    log("ffmpeg:", exe or "não encontrado (as falas ficam em WAV)")
    tmp = a.saida + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    chaves = list(fr)
    arquivos = {}
    for i in range(0, len(chaves), LOTE):
        lote = chaves[i:i + LOTE]
        t1 = time.time()
        torch.manual_seed(1000 + i)
        try:
            audios = model.generate(text=[fr[k] for k in lote], language="pt",
                                    voice_clone_prompt=[prompt] * len(lote), num_step=PASSOS)
            if len(audios) != len(lote):
                raise RuntimeError(f"lote devolveu {len(audios)} áudios para {len(lote)} frases")
        except Exception as e:  # noqa: BLE001  (lote falhou: tenta uma por uma)
            log("lote falhou, gerando uma por vez:", e)
            audios = []
            for k in lote:
                torch.manual_seed(1000 + chaves.index(k))
                audios.append(model.generate(text=fr[k], language="pt", voice_clone_prompt=prompt,
                                             num_step=PASSOS)[0])
        for k, au in zip(lote, audios):
            wav = os.path.join(tmp, k + ".wav")
            salvar_wav(wav, para_np(au))
            if para_mp3(exe, wav, os.path.join(tmp, k + ".mp3")):
                os.remove(wav)
                arquivos[k] = f"{k}.mp3?v={assinatura}"
            else:
                arquivos[k] = f"{k}.wav?v={assinatura}"
        log(f"{min(i + LOTE, len(chaves))}/{len(chaves)} falas ({time.time() - t1:.0f}s)")

    nome, sexo = VOZES[a.voz]
    shutil.copyfile(a.ref, os.path.join(tmp, "ref.wav"))
    with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": a.voz, "voz": nome, "sexo": sexo, "motor": "OmniVoice (VoiceStudio)", "versao": assinatura,
                   "arquivos": arquivos, "frases": fr}, fh, ensure_ascii=False)
    shutil.rmtree(a.saida, ignore_errors=True)
    os.rename(tmp, a.saida)
    log(f"{len(arquivos)} falas geradas com a voz {nome} em {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
