"""Regenerate the GUI's ringtone assets (assets/ringtones/*.wav).

The seven tones are the ones the user's Get It reminder app plays
(桌面/useful/Get It/get_it_pyqt/源文件/ringtone.py): the same oscillators,
envelopes and note sequences, ported to plain numpy so the WAVs can be written
once with the stdlib `wave` module and committed.

Runtime needs no numpy: fungi/gui/ring.py plays the committed files, so this is
a development tool — run it only when a tone's recipe changes, with any
interpreter that has numpy (the box has one; numpy is NOT a Fungi dependency):

    python scripts/make_ringtones.py

Mono 44.1 kHz 16-bit: the source stacks the same array into two channels, which
plays identically and only doubles the bytes on disk.
"""

import pathlib
import wave

import numpy as np

SAMPLE_RATE = 44100
OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "assets" / "ringtones"


def _pcm(audio: np.ndarray) -> np.ndarray:
    """Float waveform (roughly -1..1) -> int16 samples."""
    return np.clip(audio * 32767, -32768, 32767).astype(np.int16)


def _time(duration: float) -> np.ndarray:
    return np.linspace(0, duration, int(SAMPLE_RATE * duration), False)


def dingdong() -> np.ndarray:
    """Elegant two-note chime (A5 then E5)."""
    duration, samples = 1.2, int(SAMPLE_RATE * 1.2)
    t = _time(duration)
    note_len = int(0.4 * SAMPLE_RATE)

    sound1 = 0.6 * np.sin(2 * np.pi * 880 * t[:note_len])
    sound2 = 0.6 * np.sin(2 * np.pi * 660 * t[:note_len])
    sound1 += 0.2 * np.sin(2 * np.pi * 880 * 2 * t[:note_len])
    sound2 += 0.2 * np.sin(2 * np.pi * 660 * 2 * t[:note_len])

    audio = np.zeros(samples)
    audio[0:note_len] = sound1
    audio[int(0.5 * SAMPLE_RATE) : int(0.5 * SAMPLE_RATE) + note_len] = sound2

    envelope = np.ones(samples)
    attack, decay = int(0.1 * SAMPLE_RATE), int(0.3 * SAMPLE_RATE)
    envelope[:attack] = np.linspace(0, 1, attack)
    envelope[attack : attack + decay] = np.exp(-3 * (t[attack : attack + decay] - 0.1))
    envelope[attack + decay :] = np.exp(-8 * (t[attack + decay :] - 0.4))
    return _pcm(audio * envelope)


def chime() -> np.ndarray:
    """Wind chime: C5 E5 G5 C6, bell harmonics, exponential decay."""
    duration, samples = 2.5, int(SAMPLE_RATE * 2.5)
    audio = np.zeros(samples)
    for i, (freq, amp) in enumerate(
        zip([523.25, 659.25, 783.99, 1046.50], [0.5, 0.4, 0.3, 0.2], strict=False)
    ):
        start = i * 0.3
        if start >= duration:
            continue
        freq_duration = duration - start
        freq_t = np.linspace(0, freq_duration, int(freq_duration * SAMPLE_RATE), False)
        tone = amp * (
            np.sin(2 * np.pi * freq * freq_t)
            + 0.3 * np.sin(2 * np.pi * freq * 2 * freq_t)
            + 0.1 * np.sin(2 * np.pi * freq * 3 * freq_t)
        )
        tone = tone * np.exp(-1.5 * freq_t)
        start_sample = int(start * SAMPLE_RATE)
        end_sample = start_sample + len(tone)
        if end_sample <= samples:
            audio[start_sample:end_sample] += tone
    return _pcm(audio)


def beep() -> np.ndarray:
    """Modern FM beep: square + sine carrier, short attack/sustain/release."""
    duration, samples = 0.6, int(SAMPLE_RATE * 0.6)
    t = _time(duration)
    freq = 1200 + 100 * np.sin(2 * np.pi * 8 * t)
    audio = 0.7 * (0.5 * np.sign(np.sin(2 * np.pi * freq * t))) + 0.3 * np.sin(2 * np.pi * freq * t)

    envelope = np.ones(samples)
    attack, sustain, release = (
        int(0.05 * SAMPLE_RATE),
        int(0.4 * SAMPLE_RATE),
        int(0.15 * SAMPLE_RATE),
    )
    envelope[:attack] = np.linspace(0, 1, attack)
    envelope[attack : attack + sustain] = 0.8
    envelope[attack + sustain :] = np.linspace(0.8, 0, release)
    return _pcm(audio * envelope)


def alert() -> np.ndarray:
    """Professional alert: high/low alternation six times a second."""
    duration, samples = 1.2, int(SAMPLE_RATE * 1.2)
    t = _time(duration)
    audio = np.zeros(samples)
    for i in range(int(duration * 6)):
        start_sample = int(i * SAMPLE_RATE / 6)
        end_sample = int((i + 0.5) * SAMPLE_RATE / 6)
        if i % 2 == 0:
            segment = 0.5 * (
                np.sin(2 * np.pi * 1600 * t[start_sample:end_sample])
                + 0.2 * np.sin(2 * np.pi * 3200 * t[start_sample:end_sample])
            )
        else:
            segment = 0.5 * np.sin(2 * np.pi * 1000 * t[start_sample:end_sample])
        if end_sample <= samples:
            audio[start_sample:end_sample] = segment
    envelope = np.exp(-1.2 * t) * (0.8 + 0.2 * np.sin(2 * np.pi * 2 * t))
    return _pcm(audio * envelope)


def notification() -> np.ndarray:
    """Rising 800 -> 1200 Hz slide with a fast attack and slow release."""
    duration, samples = 0.8, int(SAMPLE_RATE * 0.8)
    t = _time(duration)
    freq = np.linspace(800, 1200, samples)
    audio = 0.7 * np.sin(2 * np.pi * freq * t)

    envelope = np.ones(samples)
    attack, release_start = int(0.1 * SAMPLE_RATE), int(0.3 * SAMPLE_RATE)
    envelope[:attack] = np.linspace(0, 1, attack)
    envelope[release_start:] = np.exp(-3 * (t[release_start:] - 0.3))
    return _pcm(audio * envelope)


def piano() -> np.ndarray:
    """Piano tone: four harmonics with per-partial decay and a percussive attack."""
    duration, samples = 1.5, int(SAMPLE_RATE * 1.5)
    t = _time(duration)
    audio = np.zeros(samples)
    for freq, amp in zip([261.63, 523.25, 784.88, 1046.50], [0.7, 0.5, 0.3, 0.2], strict=False):
        audio += amp * np.sin(2 * np.pi * freq * t) * np.exp(-(1.0 + 0.5 * (freq / 261.63)) * t)
    return _pcm(audio * np.exp(-15 * t))


def synth() -> np.ndarray:
    """Synthesizer: two detuned oscillators with harmonics and an ADSR envelope."""
    duration, samples = 1.0, int(SAMPLE_RATE * 1.0)
    t = _time(duration)
    audio = (
        0.5 * np.sin(2 * np.pi * 440 * t)
        + 0.3 * np.sin(2 * np.pi * 554.37 * t)
        + 0.1 * np.sin(2 * np.pi * 880 * t)
        + 0.1 * np.sin(2 * np.pi * 1108.74 * t)
    )
    audio = audio * (0.8 + 0.2 * np.exp(-2 * t))

    envelope = np.ones(samples)
    attack, decay, release = int(0.05 * SAMPLE_RATE), int(0.3 * SAMPLE_RATE), int(0.2 * SAMPLE_RATE)
    sustain = 0.7
    envelope[:attack] = np.linspace(0, 1, attack)
    envelope[attack : attack + decay] = np.linspace(1, sustain, decay)
    envelope[attack + decay : -release] = sustain
    envelope[-release:] = np.linspace(sustain, 0, release)
    return _pcm(audio * envelope)


TONES = {
    "dingdong": dingdong,
    "chime": chime,
    "beep": beep,
    "alert": alert,
    "notification": notification,
    "piano": piano,
    "synth": synth,
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, make in TONES.items():
        path = OUT_DIR / f"{name}.wav"
        with wave.open(str(path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(SAMPLE_RATE)
            fh.writeframes(make().tobytes())
        print(f"{path.name}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
