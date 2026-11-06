"""Génère les sons discrets de l'interface (WAV 44,1 kHz mono, quelques Ko) : python tools/make_sounds.py"""
import math
import struct
import wave
from pathlib import Path

RATE = 44100
OUT = Path(__file__).resolve().parent.parent / "assets" / "sounds"


def tone(freq, dur, vol=0.35, attack=0.004, decay=6.0, harmonics=((2, 0.18), (3, 0.06))):
    n = int(RATE * dur)
    out = []
    for i in range(n):
        t = i / RATE
        env = min(1.0, t / attack) * math.exp(-decay * t)
        s = math.sin(2 * math.pi * freq * t) + sum(a * math.sin(2 * math.pi * freq * h * t) for h, a in harmonics)
        out.append(vol * env * s / 1.3)
    return out


def seq(*parts, gap=0.0):
    out = []
    for p in parts:
        out += p + [0.0] * int(RATE * gap)
    return out


def save(name, samples):
    OUT.mkdir(parents=True, exist_ok=True)
    tail = [0.0] * int(RATE * 0.03)
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32000)) for s in samples + tail))


if __name__ == "__main__":
    save("succes.wav", seq(tone(1318.5, 0.11, 0.28, decay=14), tone(1760.0, 0.32, 0.30, decay=9)))
    save("erreur.wav", seq(tone(392.0, 0.14, 0.38, decay=10), tone(311.1, 0.30, 0.38, decay=8)))
    save("attention.wav", tone(880.0, 0.28, 0.30, decay=10))
    save("ajout.wav", tone(2093.0, 0.07, 0.22, decay=40, harmonics=()))
    save("impression.wav", seq(tone(1046.5, 0.08, 0.24, decay=18), tone(1318.5, 0.08, 0.24, decay=18),
                               tone(1568.0, 0.30, 0.26, decay=9)))
    print(sorted(p.name for p in OUT.glob("*.wav")))
