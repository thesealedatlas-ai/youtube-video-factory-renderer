#!/usr/bin/env python3
"""Free renderer for The Sealed Atlas video factory.

Input:  render_payload.json (the n8n "Render Job Payload" output)
Output: out/video.mp4, out/narration.wav, out/subtitles.srt, out/render_report.json

Free tools only: Piper (local TTS), FFmpeg, CairoSVG. No paid APIs.
Each beat: Piper voices the beat narration -> the beat lasts exactly as long as its audio
(+ small pad) -> its still image gets a slow Ken Burns move -> optional overlays/CTA card.
"""
import argparse, json, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request, wave

W, H, FPS = 1920, 1080, 30
PAD = 0.6          # seconds of breathing room after each beat's narration
XFADE = 0.5        # crossfade length between beats
THREADS = os.environ.get('FFMPEG_THREADS', '4')  # cap x264 threads (very wide machines can stall)
UA = 'TheSealedAtlasVideoFactory/1.0 (render; thesealedatlas@gmail.com)'


def log(*a):
    print('[render]', *a, flush=True)


def ffmpeg_bin():
    p = os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
    if p:
        return p
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


FF = None


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError('command failed: ' + ' '.join(cmd[:6]) + ' ...\n' + r.stderr[-2000:])
    return r


def wav_seconds(path):
    with wave.open(path) as w:
        return w.getnframes() / float(w.getframerate())


_VOICE = {}


def tts(text, out_wav, voice, data_dir):
    # load the voice model once per run (the CLI reloads ~120 MB on every call)
    from piper import PiperVoice
    if voice not in _VOICE:
        _VOICE[voice] = PiperVoice.load(os.path.join(data_dir, voice + '.onnx'))
    with wave.open(out_wav, 'wb') as wf:
        _VOICE[voice].synthesize_wav(text, wf)
    return wav_seconds(out_wav)


def fetch(url, dest):
    if url.startswith('data:image/svg+xml'):
        svg = urllib.parse.unquote(url.split(',', 1)[1])
        import cairosvg
        cairosvg.svg2png(bytestring=svg.encode('utf-8'), write_to=dest, output_width=W, output_height=H)
        return dest
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, 'wb') as f:
                shutil.copyfileobj(r, f)
            return dest
        except Exception as e:  # Wikimedia occasionally rate-limits; back off and retry
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError('download failed for ' + url + ': ' + str(last))


def svg_text_png(dest, text, sub='', y=930, size=56, box=True, full=False):
    """Lower-third / caption / CTA rendered as a transparent PNG with CairoSVG (no font tools needed)."""
    import cairosvg
    esc = lambda t: str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">' % (W, H)]
    if full:
        parts.append('<rect width="%d" height="%d" fill="#000" fill-opacity="0.55"/>' % (W, H))
        parts.append('<line x1="760" y1="590" x2="1160" y2="590" stroke="#c9a45c" stroke-width="3"/>')
        parts.append('<text x="960" y="560" text-anchor="middle" font-family="Georgia, DejaVu Serif, serif" font-size="96" fill="#e8d3a0">%s</text>' % esc(text))
    else:
        tw = min(1800, int(len(text) * size * 0.52) + 80)
        if box:
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="#0b0f18" fill-opacity="0.62"/>' % (80, y - size - 18, tw, size + 40 + (36 if sub else 0)))
        parts.append('<text x="120" y="%d" font-family="Georgia, DejaVu Serif, serif" font-size="%d" fill="#f1e6cc">%s</text>' % (y, size, esc(text)))
        if sub:
            parts.append('<text x="120" y="%d" font-family="Georgia, DejaVu Serif, serif" font-size="28" font-style="italic" fill="#c9c2b0">%s</text>' % (y + 40, esc(sub)))
    parts.append('</svg>')
    cairosvg.svg2png(bytestring=''.join(parts).encode('utf-8'), write_to=dest, output_width=W, output_height=H)
    return dest


MOTIONS = {
    'in':    ("1.0+0.08*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    'out':   ("1.08-0.08*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    'left':  ("1.08", "(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"),
    'right': ("1.08", "(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),
    'up':    ("1.08", "iw/2-(iw/zoom/2)", "(ih-ih/zoom)*(1-on/{n})"),
    'still': ("1.0", "0", "0"),
}


def pick_motion(text, idx):
    t = (text or '').lower()
    if 'static' in t:
        return 'still'
    if 'out' in t:
        return 'out'
    if 'left' in t:
        return 'left'
    if 'right' in t:
        return 'right'
    if 'scroll' in t:
        return 'up'
    if 'push' in t or 'zoom in' in t or 'reveal' in t:
        return 'in'
    return ['in', 'right', 'out', 'left'][idx % 4]


def make_clip(img, dur, motion, overlays, out_mp4):
    """Pass 1: Ken Burns move on the still. Pass 2 (only if needed): timed overlays on that clip."""
    n = max(1, int(round(dur * FPS)))
    z, x, y = [s.format(n=n) for s in MOTIONS[motion]]
    SW, SH = int(W * 1.5), int(H * 1.5)
    vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1,"
          "zoompan=z='%s':x='%s':y='%s':d=%d:s=%dx%d:fps=%d,format=yuv420p") % (SW, SH, SW, SH, z, x, y, n, W, H, FPS)
    base = out_mp4 if not overlays else out_mp4.replace('.mp4', '_base.mp4')
    run([FF, '-y', '-loglevel', 'error', '-i', img, '-vf', vf, '-frames:v', str(n), '-r', str(FPS),
         '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', THREADS, '-pix_fmt', 'yuv420p', base])
    if not overlays:
        return
    cmd = [FF, '-y', '-loglevel', 'error', '-i', base]
    chain, last = [], '0:v'
    for i, ov in enumerate(overlays):
        cmd += ['-i', ov['png']]
        en = "between(t,%.2f,%.2f)" % (ov['start'], ov['end'])
        chain.append("[%s][%d:v]overlay=0:0:eof_action=repeat:enable='%s'[o%d]" % (last, i + 1, en, i + 1))
        last = 'o%d' % (i + 1)
    cmd += ['-filter_complex', ';'.join(chain), '-map', '[' + last + ']', '-frames:v', str(n),
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', THREADS, '-pix_fmt', 'yuv420p', out_mp4]
    run(cmd)


def srt_time(s):
    ms = int(round(s * 1000))
    return '%02d:%02d:%02d,%03d' % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def main():
    global FF
    ap = argparse.ArgumentParser()
    ap.add_argument('--payload', default='render_payload.json')
    ap.add_argument('--out', default='out')
    ap.add_argument('--voices', default='voices')
    ap.add_argument('--limit-beats', type=int, default=0, help='render only the first N beats (testing)')
    a = ap.parse_args()
    FF = ffmpeg_bin()
    os.makedirs(a.out, exist_ok=True)
    work = os.path.join(a.out, 'work')
    os.makedirs(work, exist_ok=True)
    p = json.load(open(a.payload, encoding='utf-8'))
    if p.get('visual_plan_status') and p['visual_plan_status'] != 'approved':
        raise SystemExit('visual plan is not approved; refusing to render')
    if p.get('asset_match_status') and p['asset_match_status'] != 'approved':
        raise SystemExit('asset match is not approved; refusing to render')
    voice = (p.get('voice') or {}).get('piper_voice') or 'en_US-ryan-high'
    beats = sorted(p['beats'], key=lambda b: (int(b['scene']), int(b.get('beat') or 1)))
    if a.limit_beats:
        beats = beats[:a.limit_beats]
    if not os.path.exists(os.path.join(a.voices, voice + '.onnx')):
        os.makedirs(a.voices, exist_ok=True)
        run([sys.executable, '-m', 'piper.download_voices', '--data-dir', a.voices, voice])
    report = {'voice': voice, 'beats': [], 'warnings': []}
    clips, wavs, subs, t0 = [], [], [], 0.0
    for i, b in enumerate(beats):
        key = '%s.%s' % (b['scene'], b.get('beat') or 1)
        wav = os.path.join(work, 'b%02d.wav' % i)
        speech = tts(b['narration'], wav, voice, a.voices)
        dur = round(speech + PAD, 3)
        ext = '.png' if str(b.get('asset_url', '')).startswith('data:') else os.path.splitext(urllib.parse.urlparse(b['asset_url']).path)[1] or '.jpg'
        img = fetch(b['asset_url'], os.path.join(work, 'b%02d%s' % (i, ext)))
        overlays = []
        if b.get('text_overlay') and b.get('visual_type') != 'text_graphic' and not str(b.get('asset_url', '')).startswith('data:'):
            overlays.append({'png': svg_text_png(os.path.join(work, 'b%02d_lt.png' % i), b['text_overlay']), 'start': 0.6, 'end': min(dur, 7.5)})
        ctx = b.get('visual_context') or ''
        if ctx and not str(b.get('asset_url', '')).startswith('data:'):
            overlays.append({'png': svg_text_png(os.path.join(work, 'b%02d_ctx.png' % i), ctx, y=1040, size=30), 'start': 0.6, 'end': dur})
        for j, o in enumerate(b.get('timed_overlays') or []):
            if o.get('type') == 'cta_card':
                # retime the CTA to the real Piper audio: it is the last sentence of the beat
                cta_words = len(str(o.get('narration', '')).split())
                all_words = max(1, len(b['narration'].split()))
                start = round(speech * (1 - cta_words / all_words), 2)
                overlays.append({'png': svg_text_png(os.path.join(work, 'b%02d_cta%d.png' % (i, j)), o.get('text') or 'Subscribe', full=True), 'start': start, 'end': dur})
        clip = os.path.join(work, 'b%02d.mp4' % i)
        make_clip(img, dur, pick_motion(b.get('motion'), i), overlays, clip)
        clips.append((clip, dur))
        wavs.append((wav, dur))
        subs.append((t0, t0 + speech, b['narration']))
        report['beats'].append({'beat': key, 'speech_seconds': round(speech, 2), 'clip_seconds': dur, 'motion': pick_motion(b.get('motion'), i), 'overlays': len(overlays), 'asset': b.get('asset_title')})
        log(key, 'speech %.1fs' % speech, 'clip %.1fs' % dur, b.get('asset_title', '')[:60])
        t0 += dur - (XFADE if i < len(beats) - 1 else 0)

    # audio: each beat's speech padded to its clip length, then joined with matching crossfades
    padded = []
    for i, (w, d) in enumerate(wavs):
        pw = os.path.join(work, 'p%02d.wav' % i)
        run([FF, '-y', '-loglevel', 'error', '-i', w, '-af', 'apad', '-t', '%.3f' % d, '-ar', '48000', '-ac', '2', pw])
        padded.append(pw)

    def chain_xfade(inputs, kind):
        if len(inputs) == 1:
            return [], '[0:%s]' % ('v' if kind == 'v' else 'a')
        parts, prev, offset = [], '[0:%s]' % kind, 0.0
        for k in range(1, len(inputs)):
            offset += clips[k - 1][1] - XFADE
            tag = '[%s%d]' % (kind, k)
            if kind == 'v':
                parts.append('%s[%d:v]xfade=transition=fade:duration=%.2f:offset=%.3f%s' % (prev, k, XFADE, offset, tag))
            else:
                parts.append('%s[%d:a]acrossfade=d=%.2f%s' % (prev, k, XFADE, tag))
            prev = tag
        return parts, prev

    vparts, vout = chain_xfade([c for c, _ in clips], 'v')
    if vparts:
        norm = ['[%d:v]settb=AVTB,fps=%d[n%d]' % (k, FPS, k) for k in range(len(clips))]
        vparts = norm + [re.sub(r'\[(\d+):v\]', r'[n\1]', part) for part in vparts]
    aparts, aout = chain_xfade(padded, 'a')
    silent = os.path.join(work, 'video_noaudio.mp4')
    cmd = [FF, '-y', '-loglevel', 'error', '-filter_threads', '2', '-filter_complex_threads', '2']
    for c, _ in clips:
        cmd += ['-threads', '2', '-i', c]
    if vparts:
        cmd += ['-filter_complex', ';'.join(vparts), '-map', vout]
    cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', THREADS, '-pix_fmt', 'yuv420p', silent]
    run(cmd)
    narr = os.path.join(a.out, 'narration.wav')
    cmd = [FF, '-y', '-loglevel', 'error']
    for pw in padded:
        cmd += ['-i', pw]
    if aparts:
        cmd += ['-filter_complex', ';'.join(aparts), '-map', aout]
    cmd += ['-ar', '48000', narr]
    run(cmd)
    final = os.path.join(a.out, 'video.mp4')
    run([FF, '-y', '-loglevel', 'error', '-i', silent, '-i', narr, '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', final])

    with open(os.path.join(a.out, 'subtitles.srt'), 'w', encoding='utf-8') as f:
        for k, (s, e, txt) in enumerate(subs, 1):
            f.write('%d\n%s --> %s\n%s\n\n' % (k, srt_time(s), srt_time(e), txt))
    probe = run([FF, '-i', final, '-f', 'null', '-'])
    m = re.search(r'Duration: (\d+):(\d+):([\d.]+)', probe.stderr)
    total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    report.update({'video': final, 'duration_seconds': round(total, 2) if total else None, 'beat_count': len(beats), 'size_bytes': os.path.getsize(final), 'title': p.get('title'), 'credits': p.get('credits', [])})
    json.dump(report, open(os.path.join(a.out, 'render_report.json'), 'w'), indent=2)
    log('done', final, report['duration_seconds'], 's', report['size_bytes'], 'bytes')


if __name__ == '__main__':
    main()
