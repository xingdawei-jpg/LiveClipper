"""Pause-aware PCM WAV chunking for local ASR.

SenseVoice's bundled VAD may return one long record for a live stream.  This
module creates short, contiguous input files before inference without changing
the final media timeline.  It intentionally operates on the 16 kHz mono PCM
WAV produced by :mod:`stt`; unsupported inputs simply use the existing single
file inference path.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
from pathlib import Path
import sys
from typing import Iterable
import wave


_FRAME_SECONDS = 0.03
_MIN_SILENCE_SECONDS = 0.42
_MIN_CHUNK_SECONDS = 3.0
_TARGET_CHUNK_SECONDS = 9.0
_MAX_CHUNK_SECONDS = 12.0
_MIN_TAIL_SECONDS = 2.0
_MIN_RMS = 80.0


@dataclass(frozen=True)
class AudioChunk:
    """One contiguous source-time interval to send to the local ASR model."""

    start: float
    end: float
    boundary_reason: str

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end) - float(self.start))


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _pcm_frame_levels(audio_path: str | Path) -> tuple[float, list[tuple[float, float, float]]] | None:
    """Return duration and frame RMS levels for a mono signed-16-bit WAV."""
    try:
        with wave.open(str(audio_path), "rb") as reader:
            if (
                reader.getcomptype() != "NONE"
                or reader.getsampwidth() != 2
                or reader.getnchannels() != 1
                or reader.getframerate() <= 0
            ):
                return None
            sample_rate = int(reader.getframerate())
            frame_samples = max(1, int(round(sample_rate * _FRAME_SECONDS)))
            total_frames = int(reader.getnframes())
            if total_frames <= 0:
                return None

            levels: list[tuple[float, float, float]] = []
            offset = 0
            while offset < total_frames:
                frame_count = min(frame_samples, total_frames - offset)
                payload = reader.readframes(frame_count)
                samples = array("h")
                samples.frombytes(payload)
                if sys.byteorder != "little":
                    samples.byteswap()
                if not samples:
                    break
                rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
                start = offset / sample_rate
                end = (offset + frame_count) / sample_rate
                levels.append((start, end, rms))
                offset += frame_count
            if not levels:
                return None
            return total_frames / sample_rate, levels
    except (OSError, EOFError, wave.Error):
        return None


def _speech_threshold(levels: Iterable[tuple[float, float, float]]) -> float:
    values = [level for _start, _end, level in levels]
    if not values:
        return _MIN_RMS
    noise_floor = _quantile(values, 0.25)
    loud_speech = _quantile(values, 0.85)
    # Let a noisy live room fall back to fixed-duration chunks rather than
    # declaring the whole track silent and losing speech.
    return max(
        _MIN_RMS,
        min(max(noise_floor * 2.4, _MIN_RMS), max(loud_speech * 0.45, _MIN_RMS)),
    )


def _pause_boundaries(levels: list[tuple[float, float, float]]) -> list[float]:
    threshold = _speech_threshold(levels)
    boundaries: list[float] = []
    quiet_start: float | None = None
    quiet_end: float | None = None
    for start, end, level in levels:
        if level < threshold:
            if quiet_start is None:
                quiet_start = start
            quiet_end = end
            continue
        if quiet_start is not None and quiet_end is not None:
            if quiet_end - quiet_start >= _MIN_SILENCE_SECONDS:
                boundaries.append(round((quiet_start + quiet_end) / 2.0, 3))
        quiet_start = None
        quiet_end = None
    if quiet_start is not None and quiet_end is not None and quiet_end - quiet_start >= _MIN_SILENCE_SECONDS:
        boundaries.append(round((quiet_start + quiet_end) / 2.0, 3))
    return boundaries


def build_pause_aware_audio_chunks(audio_path: str | Path) -> list[AudioChunk]:
    """Split a supported WAV at pauses, with a strict 12-second hard limit.

    Returned intervals always cover the source continuously.  A hard time
    boundary is used only when the audio has no usable pause near the target;
    this is still better than feeding one multi-minute record to punctuation
    recovery and lets the later semantic builder rejoin only valid context.
    """
    analysis = _pcm_frame_levels(audio_path)
    if analysis is None:
        return []
    duration, levels = analysis
    if duration <= _MAX_CHUNK_SECONDS + 0.001:
        return [AudioChunk(0.0, round(duration, 3), "source_end")]

    pause_boundaries = _pause_boundaries(levels)
    chunks: list[AudioChunk] = []
    start = 0.0
    while duration - start > 0.001:
        remaining = duration - start
        if remaining <= _MAX_CHUNK_SECONDS + 0.001:
            chunks.append(AudioChunk(round(start, 3), round(duration, 3), "source_end"))
            break

        minimum = start + _MIN_CHUNK_SECONDS
        target = start + _TARGET_CHUNK_SECONDS
        maximum = min(duration, start + _MAX_CHUNK_SECONDS)
        nearby_pauses = [
            boundary for boundary in pause_boundaries
            if minimum <= boundary <= maximum
        ]
        if nearby_pauses:
            end = min(nearby_pauses, key=lambda boundary: abs(boundary - target))
            reason = "pause"
        else:
            end = target
            reason = "hard_limit"
        end = min(duration, max(start + _MIN_CHUNK_SECONDS, end))
        chunks.append(AudioChunk(round(start, 3), round(end, 3), reason))
        start = end

    if len(chunks) >= 2 and chunks[-1].duration < _MIN_TAIL_SECONDS:
        previous = chunks[-2]
        tail = chunks[-1]
        if previous.duration + tail.duration <= _MAX_CHUNK_SECONDS + 0.001:
            chunks[-2] = AudioChunk(previous.start, tail.end, "tail_merge")
            chunks.pop()
    return chunks


def write_audio_chunk(audio_path: str | Path, output_path: str | Path, chunk: AudioChunk) -> None:
    """Write one exact PCM WAV interval without resampling or changing samples."""
    with wave.open(str(audio_path), "rb") as reader:
        sample_rate = int(reader.getframerate())
        total_frames = int(reader.getnframes())
        start_frame = min(total_frames, max(0, int(round(float(chunk.start) * sample_rate))))
        end_frame = min(total_frames, max(start_frame, int(round(float(chunk.end) * sample_rate))))
        reader.setpos(start_frame)
        payload = reader.readframes(end_frame - start_frame)
        params = reader.getparams()
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(payload)
