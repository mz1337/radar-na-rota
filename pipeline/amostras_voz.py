#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gera amostras de voz para escolher a voz dos alertas (roda no GitHub Actions, CPU).

Motor principal: OmniVoice (k2-fsa), o mesmo motor padrão do VoiceStudio.
- Vozes "desenhadas" (voice design): o modelo cria a voz a partir de atributos
  (sexo, idade, tom). Para a voz ficar idêntica em todas as frases, a voz desenhada
  fala uma frase de referência uma única vez e depois é clonada nas frases de teste.
- Vozes com sotaque brasileiro garantido: o Kokoro (vozes pt-BR pf_dora / pm_alex)
  grava a frase de referência e o OmniVoice clona o timbre com a fala mais natural dele.
- Kokoro puro e Piper (voz atual) entram para comparação.

Saída: pasta com MP3s, as referências (ref-*.wav, para reproduzir a voz aprovada
nas 101 frases do app) e um index.html para ouvir no navegador.
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
import wave

import numpy as np

SR = 24000
FRASES = [
    "Radar à frente. Limite sessenta.",
    "Atenção! Lombada eletrônica próxima. Limite quarenta. Reduza a velocidade.",
    "Alertas de radar ligados. Boa viagem.",
]
REF_TXT = "Olá! Eu vou te avisar sempre que houver um radar no seu caminho."

# chave, nome, origem da referência, parâmetro, semente
VOZES = [
    ("omni-feminina-a", "Feminina A · jovem, tom médio", "design", "female, young adult, moderate pitch", 11),
    ("omni-feminina-b", "Feminina B · adulta, tom grave", "design", "female, middle-aged, low pitch", 12),
    ("omni-feminina-c", "Feminina C · sotaque brasileiro (base Kokoro Dora)", "kokoro", "pf_dora", 13),
    ("omni-masculina-a", "Masculina A · jovem, tom médio", "design", "male, young adult, moderate pitch", 21),
    ("omni-masculina-b", "Masculina B · adulto, tom grave", "design", "male, middle-aged, low pitch", 22),
    ("omni-masculina-c", "Masculina C · sotaque brasileiro (base Kokoro Alex)", "kokoro", "pm_alex", 23),
]
KOKORO_PURO = [
    ("kokoro-feminina", "Kokoro Dora (sem OmniVoice)", "pf_dora"),
    ("kokoro-masculina", "Kokoro Alex (sem OmniVoice)", "pm_alex"),
]


def log(*a):
    print(*a, flush=True)


def salvar_wav(caminho: str, audio: np.ndarray, sr: int = SR) -> None:
    a = np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1, 1)
    with wave.open(caminho, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((a * 32767).astype("<i2").tobytes())


def juntar(partes: list, pausa: float = 0.6) -> np.ndarray:
    sil = np.zeros(int(SR * pausa), dtype=np.float32)
    out = []
    for p in partes:
        out += [np.asarray(p, dtype=np.float32).reshape(-1), sil]
    return np.concatenate(out)


def mp3(wav_path: str, mp3_path: str) -> str:
    if shutil.which("ffmpeg"):
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path, "-af", "loudnorm=I=-14:TP=-1.5",
                            "-ac", "1", "-ar", "24000", "-b:a", "96k", mp3_path])
        if r.returncode == 0:
            os.remove(wav_path)
            return os.path.basename(mp3_path)
    return os.path.basename(wav_path)


def para_np(x) -> np.ndarray:
    if hasattr(x, "detach"):
        x = x.detach().cpu().float().numpy()
    return np.asarray(x, dtype=np.float32).reshape(-1)


# ---------------- Kokoro (vozes pt-BR) ----------------
_KOKORO = None


def kokoro(texto: str, voz: str) -> np.ndarray:
    global _KOKORO
    if _KOKORO is None:
        from kokoro import KPipeline
        _KOKORO = KPipeline(lang_code="p", repo_id="hexgrad/Kokoro-82M")
    partes = [para_np(a) for _, _, a in _KOKORO(texto, voice=voz, speed=1.0)]
    return np.concatenate(partes)


# ---------------- OmniVoice ----------------
def omnivoice(saida: str) -> list:
    import torch
    from omnivoice import OmniVoice

    torch.set_num_threads(os.cpu_count() or 4)
    t0 = time.time()
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cpu", dtype=torch.float32)
    sr = getattr(model, "sampling_rate", None) or SR
    if sr != SR:
        log(f"aviso: taxa do modelo {sr}")
    log(f"OmniVoice carregado em {time.time() - t0:.0f}s")

    feitos = []
    for chave, nome, origem, param, semente in VOZES:
        try:
            t0 = time.time()
            ref = os.path.join(saida, f"ref-{chave}.wav")
            torch.manual_seed(semente)
            np.random.seed(semente)
            if origem == "design":
                a = model.generate(text=REF_TXT, language="pt", instruct=param, num_step=32)[0]
            else:
                a = kokoro(REF_TXT, param)
            salvar_wav(ref, para_np(a))
            prompt = model.create_voice_clone_prompt(ref_audio=ref, ref_text=REF_TXT)
            partes = []
            for i, fr in enumerate(FRASES):
                torch.manual_seed(semente * 100 + i)
                partes.append(para_np(model.generate(text=fr, language="pt", voice_clone_prompt=prompt, num_step=32)[0]))
            wav = os.path.join(saida, chave + ".wav")
            salvar_wav(wav, juntar(partes))
            feitos.append({"arquivo": mp3(wav, os.path.join(saida, chave + ".mp3")), "nome": nome,
                           "motor": "OmniVoice (motor do VoiceStudio)", "referencia": os.path.basename(ref),
                           "origem": origem, "parametro": param, "semente": semente,
                           "segundos": round(time.time() - t0)})
            log(f"{nome}: ok em {time.time() - t0:.0f}s")
        except Exception:  # noqa: BLE001
            log(f"{nome}: FALHOU\n{traceback.format_exc()}")
    return feitos


def kokoro_puro(saida: str) -> list:
    feitos = []
    for chave, nome, voz in KOKORO_PURO:
        try:
            wav = os.path.join(saida, chave + ".wav")
            salvar_wav(wav, juntar([kokoro(fr, voz) for fr in FRASES]))
            feitos.append({"arquivo": mp3(wav, os.path.join(saida, chave + ".mp3")), "nome": nome, "motor": "Kokoro 82M"})
            log(f"{nome}: ok")
        except Exception:  # noqa: BLE001
            log(f"{nome}: FALHOU\n{traceback.format_exc()}")
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


def pagina(saida: str, grupos: list) -> None:
    blocos = []
    for titulo, itens in grupos:
        if not itens:
            continue
        lis = "\n".join(
            f'<li><b>{html.escape(i["nome"])}</b><small>{html.escape(i["motor"])}</small>'
            f'<audio controls preload="none" src="{html.escape(i["arquivo"])}"></audio></li>' for i in itens)
        blocos.append(f"<h2>{html.escape(titulo)}</h2><ul>{lis}</ul>")
    txt = " / ".join(FRASES)
    doc = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Amostras de voz · Radar na Rota</title>
<style>:root{{color-scheme:light dark;--g:#00704a}}body{{margin:0;font:16px/1.45 system-ui,Roboto,sans-serif;background:Canvas;color:CanvasText}}
main{{max-width:560px;margin:0 auto;padding:20px 16px 40px}}h1{{font-size:22px;margin:0 0 6px}}h2{{font-size:17px;margin:26px 0 10px}}p{{opacity:.75;margin:0 0 8px}}
ul{{list-style:none;padding:0;margin:0;display:grid;gap:12px}}li{{border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:14px;padding:14px;display:grid;gap:6px}}
small{{opacity:.65}}audio{{width:100%}}</style></head><body><main>
<h1>Amostras de voz</h1><p>Frases: {html.escape(txt)}</p><p>Escolha uma feminina e/ou uma masculina e diga a letra.</p>
{''.join(blocos)}
</main></body></html>"""
    with open(os.path.join(saida, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default="amostras")
    args = ap.parse_args()
    if os.path.isdir(args.saida):
        shutil.rmtree(args.saida)
    os.makedirs(args.saida)

    omni, kok, pip_ = [], [], []
    try:
        omni = omnivoice(args.saida)
    except Exception:  # noqa: BLE001
        log("OmniVoice indisponível\n" + traceback.format_exc())
    try:
        kok = kokoro_puro(args.saida)
    except Exception:  # noqa: BLE001
        log("Kokoro indisponível\n" + traceback.format_exc())
    try:
        pip_ = piper(args.saida)
    except Exception:  # noqa: BLE001
        log("Piper indisponível\n" + traceback.format_exc())
    shutil.rmtree(os.path.join(args.saida, "_piper"), ignore_errors=True)

    pagina(args.saida, [
        ("Femininas · OmniVoice", [i for i in omni if "feminina" in i["arquivo"]]),
        ("Masculinas · OmniVoice", [i for i in omni if "masculina" in i["arquivo"]]),
        ("Para comparar", kok + pip_),
    ])
    with open(os.path.join(args.saida, "amostras.json"), "w", encoding="utf-8") as f:
        json.dump({"frases": FRASES, "ref_txt": REF_TXT, "itens": omni + kok + pip_}, f, ensure_ascii=False, indent=1)
    if not omni:
        sys.exit("nenhuma amostra OmniVoice foi gerada")


if __name__ == "__main__":
    main()
