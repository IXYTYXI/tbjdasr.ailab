import os
import subprocess
import wave
from pathlib import Path


def read_pcm(path, max_seconds=60):
    with wave.open(str(path), 'rb') as f:
        if (f.getnchannels(), f.getsampwidth(), f.getframerate(), f.getcomptype()) != (1, 2, 16000, 'NONE'):
            raise ValueError('ASR requires mono 16 kHz PCM16')
        if not 0 < f.getnframes() <= 16000 * max_seconds:
            raise ValueError('Audio duration outside ASR limit')
        data = f.readframes(f.getnframes())
        if len(data) != f.getnframes() * 2:
            raise ValueError('Truncated WAV')
        return data


def split_wav(source, folder, seconds=45):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    output = []
    with wave.open(str(source), 'rb') as f:
        if (f.getnchannels(), f.getsampwidth(), f.getframerate()) != (1, 2, 16000):
            raise ValueError('Expected mono PCM16 16k')
        offset, index = 0, 0
        while data := f.readframes(int(seconds * 16000)):
            path = folder / f'{index:05d}.wav'
            temp = path.with_suffix('.tmp')
            with wave.open(str(temp), 'wb') as out:
                out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                out.writeframes(data)
            os.replace(temp, path)
            duration = len(data) / 32000
            output.append((path, offset, duration))
            offset += duration
            index += 1
    if not output:
        raise ValueError('No audio frames')
    return output


def extract_audio(source, target):
    temp = Path(target).with_suffix('.tmp.wav')
    try:
        result = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                                 '-i', str(source), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
                                 '-c:a', 'pcm_s16le', str(temp)], capture_output=True, timeout=300)
        if result.returncode:
            raise ValueError('FFmpeg audio extraction failed: ' + result.stderr.decode(errors='replace')[-1200:])
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
