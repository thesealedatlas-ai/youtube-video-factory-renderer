#!/usr/bin/env python3
"""Finalize a rendered video for upload (free: FFmpeg + ImageMagick only, no AI, no paid APIs).

Reuses an existing render artifact (no re-render):
  art/video.mp4, art/render_report.json, art/subtitles.srt  (from the render run)
  the committed render job JSON (beats, credits, title, tags)
Writes to --out:
  video.mp4        - unchanged copy of the render
  thumbnail.jpg    - 1280x720, strongest approved free visual + large title text
  contact_sheet.jpg- one frame from the middle of every beat (for a quick human check)
  subtitles.srt    - original per-beat captions
  subtitles_sentences.srt - same timing split into sentence-sized cues
  qc_report.json   - automated checks (voice pace, timing, black/frozen frames, loudness, captions)
  summary.json     - small payload for n8n (qc, scene starts, credits)
"""
import argparse, json, os, re, shutil, subprocess, urllib.request

XFADE = 0.5  # must match render.py
W, H = 1280, 720
UA = 'TheSealedAtlasVideoFactory/1.0 (finalize; thesealedatlas@gmail.com)'
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def run(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError('command failed: ' + ' '.join(cmd[:8]) + '\n' + r.stderr[-2000:])
    return r


def im():
    return shutil.which('magick') or shutil.which('convert')


def probe(path):
    r = run(['ffprobe', '-v', 'error', '-show_entries',
             'format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels',
             '-of', 'json', path])
    return json.loads(r.stdout)


def ffmpeg_log(args):
    return run(['ffmpeg', '-hide_banner', '-nostats'] + args + ['-f', 'null', '-'], check=False).stderr


def srt_parse(path):
    if not os.path.exists(path):
        return []
    cues = []
    for block in re.split(r'\n\s*\n', open(path, encoding='utf-8').read().strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)', lines[1])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        s = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        cues.append((s, e, ' '.join(lines[2:])))
    return cues


def srt_time(t):
    ms = int(round(t * 1000))
    return '%02d:%02d:%02d,%03d' % (ms // 3600000, (ms // 60000) % 60, (ms // 1000) % 60, ms % 1000)


def split_sentences(cues):
    out = []
    for s, e, txt in cues:
        parts = [p.strip() for p in re.split(r'(?<=[.!?])\s+(?=[A-Z"\u201c])', txt) if p.strip()]
        total = sum(len(p) for p in parts) or 1
        t = s
        for p in parts:
            d = (e - s) * len(p) / total
            out.append((t, t + d, p))
            t += d
    return out


def pick_thumbnail_source(job):
    override = ((job.get('thumbnail') or {}).get('source_beat') or '')
    best, best_score = None, -1
    for b in job.get('beats', []):
        url = str(b.get('asset_url') or '')
        key = '%s.%s' % (b.get('scene'), b.get('beat') or 1)
        if not url.startswith('http'):
            continue  # local SVG cards / diagrams are not thumbnail material
        title = str(b.get('asset_title') or '').lower()
        lic = str(b.get('asset_license') or '').lower()
        score = 0.0
        if override and key == override:
            score += 100
        if 'atlantis' in title:
            score += 5
        vt = b.get('visual_type')
        score += 3 if vt in ('archival_photo', 'map') else 1 if vt == 'document' else 0
        if 'public domain' in lic or lic.startswith('pd'):
            score += 2
        m = re.match(r'(\d+)x(\d+)', str(b.get('asset_resolution') or ''))
        if m:
            score += min(int(m.group(1)), 4000) / 1000.0
        if score > best_score:
            best, best_score = b, score
    return best


def wrap_title(title):
    words = title.upper().split()
    if len(words) <= 2:
        return [' '.join(words)]
    best = None
    for i in range(1, len(words)):
        a, b = ' '.join(words[:i]), ' '.join(words[i:])
        cost = max(len(a), len(b))
        if best is None or cost < best[0]:
            best = (cost, [a, b])
    return best[1]


def make_thumbnail(job, out, work):
    src = pick_thumbnail_source(job)
    if not src:
        raise RuntimeError('no free photographic asset available for the thumbnail')
    url = src['asset_url']
    raw = os.path.join(work, 'thumb_src' + (os.path.splitext(url.split('?')[0])[1] or '.jpg'))
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=90) as r, open(raw, 'wb') as f:
        shutil.copyfileobj(r, f)
    title = (job.get('thumbnail') or {}).get('title_text') or job.get('title') or ''
    lines = wrap_title(title)
    longest = max(len(l) for l in lines)
    size = int(max(70, min(132, 1160 / (longest * 0.66))))
    dest = os.path.join(out, 'thumbnail.jpg')
    cmd = [im(), raw, '-auto-orient', '-resize', '%dx%d^' % (W, H), '-gravity', 'center', '-extent', '%dx%d' % (W, H),
           '-modulate', '95,115', '-sigmoidal-contrast', '3x50%',
           '(', '-size', '%dx%d' % (W, int(H * 0.62)), 'gradient:rgba(0,0,0,0)-rgba(0,0,0,0.88)', ')',
           '-gravity', 'south', '-composite', '-font', FONT, '-pointsize', str(size)]
    line_h = int(size * 1.08)
    colors = ['white', '#FFD34D']
    for idx, text in enumerate(lines):
        y = 48 + (len(lines) - 1 - idx) * line_h
        color = colors[min(idx, 1)] if len(lines) > 1 else '#FFD34D'
        cmd += ['-gravity', 'south', '-stroke', 'black', '-strokewidth', str(max(6, size // 14)), '-fill', 'black',
                '-annotate', '+0+%d' % y, text,
                '-stroke', 'none', '-fill', color, '-annotate', '+0+%d' % y, text]
    cmd += ['-strip', '-quality', '90', dest]
    run(cmd)
    return {
        'path': dest, 'size_bytes': os.path.getsize(dest), 'title_lines': lines, 'pointsize': size,
        'source_beat': '%s.%s' % (src.get('scene'), src.get('beat') or 1), 'source_title': src.get('asset_title'),
        'source_author': src.get('asset_author'), 'source_license': src.get('asset_license'),
        'source_page_url': src.get('asset_page_url')
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--art', required=True)
    ap.add_argument('--job', required=True)
    ap.add_argument('--out', default='final')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    work = os.path.join(a.out, '_work')
    os.makedirs(work, exist_ok=True)

    job = json.load(open(a.job, encoding='utf-8'))
    report = json.load(open(os.path.join(a.art, 'render_report.json'), encoding='utf-8'))
    video = os.path.join(a.out, 'video.mp4')
    shutil.copyfile(os.path.join(a.art, 'video.mp4'), video)
    flags, hard = [], []

    # --- container / streams
    pr = probe(video)
    dur = float(pr['format']['duration'])
    vs = [s for s in pr['streams'] if s['codec_type'] == 'video']
    aus = [s for s in pr['streams'] if s['codec_type'] == 'audio']
    if not vs or not aus:
        hard.append('missing video or audio stream')
    if vs and (vs[0].get('width'), vs[0].get('height')) != (1920, 1080):
        flags.append('video is not 1920x1080')

    # --- per-beat timing and voice pace
    beats_job = sorted(job['beats'], key=lambda b: (int(b['scene']), int(b.get('beat') or 1)))
    beats_rep = report.get('beats', [])
    if len(beats_rep) != len(beats_job):
        hard.append('render report has %d beats, job has %d' % (len(beats_rep), len(beats_job)))
    t, timeline, scene_starts = 0.0, [], {}
    total_words, total_speech = 0, 0.0
    for i, (bj, br) in enumerate(zip(beats_job, beats_rep)):
        words = len(str(bj.get('narration', '')).split())
        speech = float(br.get('speech_seconds') or 0)
        clip = float(br.get('clip_seconds') or 0)
        wpm = round(words / (speech / 60.0), 1) if speech else None
        key = '%s.%s' % (bj['scene'], bj.get('beat') or 1)
        timeline.append({'beat': key, 'start': round(t, 2), 'clip_seconds': clip, 'speech_seconds': speech,
                         'words': words, 'wpm': wpm, 'visual_type': bj.get('visual_type'),
                         'asset': bj.get('asset_title'), 'overlay': bj.get('text_overlay') or ''})
        scene_starts.setdefault(str(bj['scene']), round(t, 2))
        if clip < 6:
            flags.append('beat %s is on screen only %.1fs' % (key, clip))
        if clip > 30:
            flags.append('beat %s holds one still for %.1fs' % (key, clip))
        total_words += words
        total_speech += speech
        t += clip - (XFADE if i < len(beats_rep) - 1 else 0)
    overall_wpm = round(total_words / (total_speech / 60.0), 1) if total_speech else None
    if overall_wpm and overall_wpm > 170:
        flags.append('narration pace %.0f wpm is fast for documentary narration (typical 140-160)' % overall_wpm)
    if abs(t - dur) > 1.5:
        flags.append('timeline sum %.1fs differs from file duration %.1fs' % (t, dur))

    # --- black frames, frozen picture, long silences, loudness
    black = [float(x) for x in re.findall(r'black_start:([\d.]+)', ffmpeg_log(['-i', video, '-vf', 'blackdetect=d=0.4:pix_th=0.08', '-an']))]
    freeze = [float(x) for x in re.findall(r'freeze_start: ([\d.]+)', ffmpeg_log(['-i', video, '-vf', 'freezedetect=n=0.001:d=6', '-an']))]
    silence = re.findall(r'silence_start: ([\d.]+)[\s\S]*?silence_duration: ([\d.]+)', ffmpeg_log(['-i', video, '-vn', '-af', 'silencedetect=n=-45dB:d=2']))
    loud = ffmpeg_log(['-i', video, '-vn', '-af', 'ebur128=peak=true'])
    m_i = re.findall(r'I:\s+(-?[\d.]+) LUFS', loud)
    m_tp = re.findall(r'Peak:\s+(-?[\d.]+) dBFS', loud)
    lufs = float(m_i[-1]) if m_i else None
    true_peak = float(m_tp[-1]) if m_tp else None
    if black:
        flags.append('black frames at %s' % ', '.join('%.1fs' % x for x in black[:6]))
    if freeze:
        flags.append('picture frozen 6s+ at %s' % ', '.join('%.1fs' % x for x in freeze[:6]))
    if silence:
        flags.append('silence 2s+ at %s' % ', '.join('%.1fs' % float(s) for s, _ in silence[:6]))
    if lufs is not None and (lufs < -20 or lufs > -10):
        flags.append('integrated loudness %.1f LUFS (YouTube normalises to about -14)' % lufs)
    if true_peak is not None and true_peak > -0.5:
        flags.append('audio peaks at %.1f dBFS (possible clipping)' % true_peak)

    # --- captions
    cues = srt_parse(os.path.join(a.art, 'subtitles.srt'))
    long_cues = [c for c in cues if c[1] - c[0] > 7]
    if not cues:
        flags.append('no subtitles.srt in the render')
    elif long_cues:
        flags.append('%d of %d caption cues last over 7s (one cue per beat); sentence-split captions generated' % (len(long_cues), len(cues)))
    if os.path.exists(os.path.join(a.art, 'subtitles.srt')):
        shutil.copyfile(os.path.join(a.art, 'subtitles.srt'), os.path.join(a.out, 'subtitles.srt'))
    sent = split_sentences(cues)
    with open(os.path.join(a.out, 'subtitles_sentences.srt'), 'w', encoding='utf-8') as f:
        for k, (s, e, txt) in enumerate(sent, 1):
            f.write('%d\n%s --> %s\n%s\n\n' % (k, srt_time(s), srt_time(e), txt))
    over_cps = [c for c in sent if (c[1] - c[0]) > 0 and len(c[2]) / (c[1] - c[0]) > 21]

    # --- contact sheet: middle frame of every beat
    frames = []
    for row in timeline:
        mid = row['start'] + row['clip_seconds'] / 2.0
        fp = os.path.join(work, '%s.jpg' % row['beat'])
        run(['ffmpeg', '-y', '-loglevel', 'error', '-ss', '%.2f' % mid, '-i', video, '-frames:v', '1', '-vf', 'scale=480:-1', fp])
        frames.append(fp)
    montage = shutil.which('montage')
    sheet = os.path.join(a.out, 'contact_sheet.jpg')
    if montage and frames:
        run([montage] + frames + ['-tile', '4x', '-geometry', '+6+6', '-label', '%t', '-pointsize', '18', '-background', '#222', '-fill', 'white', sheet])

    # --- thumbnail
    thumb = make_thumbnail(job, a.out, work)
    if thumb['size_bytes'] > 2 * 1024 * 1024:
        hard.append('thumbnail exceeds 2 MB')

    credits_detail = []
    seen = set()
    for b in beats_job:
        url = str(b.get('asset_url') or '')
        if not url.startswith('http') or b.get('asset_page_url') in seen:
            continue
        seen.add(b.get('asset_page_url'))
        credits_detail.append({'beat': '%s.%s' % (b['scene'], b.get('beat') or 1), 'title': b.get('asset_title'),
                               'author': b.get('asset_author'), 'license': b.get('asset_license'),
                               'source': b.get('asset_source'), 'page_url': b.get('asset_page_url')})

    qc = {
        'qc_status': 'failed' if hard else ('review' if flags else 'ok'),
        'hard_failures': hard, 'flags': flags,
        'duration_seconds': round(dur, 2), 'size_bytes': os.path.getsize(video),
        'video': vs[0] if vs else None, 'audio': aus[0] if aus else None,
        'beats': len(timeline), 'words': total_words, 'overall_wpm': overall_wpm,
        'loudness_lufs': lufs, 'true_peak_dbfs': true_peak,
        'black_frames': black, 'frozen_picture': freeze,
        'silences': [{'start': float(s), 'seconds': float(d)} for s, d in silence],
        'transitions': {'type': 'crossfade', 'seconds': XFADE, 'count': max(0, len(timeline) - 1)},
        'captions': {'cues': len(cues), 'long_cues': len(long_cues), 'sentence_cues': len(sent), 'sentence_cues_over_21cps': len(over_cps), 'burned_in': False},
        'timeline': timeline, 'scene_starts': scene_starts, 'thumbnail': thumb
    }
    json.dump(qc, open(os.path.join(a.out, 'qc_report.json'), 'w'), indent=2)
    summary = {k: qc[k] for k in ('qc_status', 'hard_failures', 'flags', 'duration_seconds', 'size_bytes', 'beats', 'words',
                                   'overall_wpm', 'loudness_lufs', 'true_peak_dbfs', 'transitions', 'captions', 'scene_starts')}
    summary['timeline'] = [{k: r[k] for k in ('beat', 'start', 'clip_seconds', 'wpm')} for r in timeline]
    summary['thumbnail'] = {k: thumb[k] for k in ('source_beat', 'source_title', 'source_author', 'source_license', 'title_lines', 'size_bytes')}
    summary.update({'title': job.get('title'), 'tags': job.get('tags') or [], 'description': job.get('description') or '',
                    'credits_detail': credits_detail})
    json.dump(summary, open(os.path.join(a.out, 'summary.json'), 'w'), indent=1)
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({k: summary[k] for k in ('qc_status', 'flags', 'duration_seconds', 'overall_wpm', 'loudness_lufs')}, indent=1))
    if hard:
        raise SystemExit('QC hard failure: ' + '; '.join(hard))


if __name__ == '__main__':
    main()
