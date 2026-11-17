"""Local Kokoro narration and an extensible, evidence-first FFmpeg presentation."""

import argparse
from bisect import bisect_right
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import subprocess
import textwrap


WIDTH, HEIGHT, FPS = 1280, 720, 24


def sentences(text):
    return re.split(r"(?<=[.!?])\s+", text.strip())


def local(root, value):
    if not isinstance(value, str) or not value:
        raise ValueError("A local relative asset path is required")
    relative = Path(value)
    if relative.is_absolute() or relative.drive or ".." in relative.parts or ":" in value:
        raise ValueError("Assets must stay within the review package")
    path = root / relative
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("Asset aliases or missing files are not allowed")
    return path


def load_storyboard(root):
    path = root / "storyboard.json"
    if path.stat().st_size > 256 * 1024:
        raise ValueError("Storyboard exceeds 256 KiB")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("version") != 1 or not 3 <= len(value.get("scenes", [])) <= 12:
        raise ValueError("Storyboard version 1 needs 3-12 scenes")
    for scene in value["scenes"]:
        for field, limit in (("title", 90), ("takeaway", 180), ("narration", 3000)):
            text = scene.get(field)
            if not isinstance(text, str) or not text.strip() or len(text) > limit:
                raise ValueError(f"Scene {field} is missing or exceeds {limit} characters")
        if sum(key in scene for key in ("image", "frames", "video")) != 1:
            raise ValueError("Each scene needs exactly one image, frames or video")
        if "frames" in scene and not 2 <= len(scene["frames"]) <= 120:
            raise ValueError("Animated scenes need 2-120 frames")
        if "frame_sentences" in scene:
            starts = scene["frame_sentences"]
            if ("frames" not in scene or not isinstance(starts, list)
                    or len(starts) != len(scene["frames"])
                    or any(type(value) is not int for value in starts)
                    or starts[0] != 0 or starts != sorted(set(starts))
                    or starts[-1] >= len(sentences(scene["narration"]))):
                raise ValueError("frame_sentences must map every frame to increasing sentence indices starting at zero")
        visuals = scene.get("frames", [scene.get("image", scene.get("video"))])
        for asset in visuals + scene.get("sources", []):
            local(root, asset)
        if not scene.get("sources"):
            raise ValueError("Every scene needs actual captured source references")
        for item in scene.get("highlights", []):
            box = item["box"]
            if (len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
                    or not 0 <= box[0] <= 1 or not 0 <= box[1] <= 1
                    or not 0 < box[2] <= 1 - box[0] or not 0 < box[3] <= 1 - box[1]
                    or not 0 <= item.get("start", 0) < item.get("end", 1) <= 1):
                raise ValueError("Highlight coordinates/timing must be normalized")
    if sum(len(scene["narration"]) for scene in value["scenes"]) > 12000:
        raise ValueError("Narration exceeds the basic renderer's 12000-character limit")
    return value


def font(size):
    from PIL import ImageFont
    for name in (str(Path(os.environ.get("WINDIR", "")) / "Fonts" / "segoeui.ttf"),
                 "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def wrapped(draw, text, xy, face, color, width, max_lines=3):
    words, lines, line = text.split(), [], ""
    for word in words:
        candidate = (line + " " + word).strip()
        if draw.textlength(candidate, font=face) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        raise ValueError(f"Text does not fit the presentation: {text[:80]}")
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=face, fill=color)
        y += face.size + 7


def timestamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}.{milliseconds:03}"


def ready(models):
    import imageio_ffmpeg
    import numpy
    import soundfile
    from PIL import Image
    from kokoro_onnx import Kokoro
    model_manifest = json.loads((models / "models.json").read_text())
    if set(model_manifest) != {"kokoro-v1.0.onnx", "voices-v1.0.bin"}:
        raise ValueError("Both Kokoro model and voices must have recorded preparation hashes")
    for name, entry in model_manifest.items():
        with (models / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                raise ValueError(f"Local model differs from its manifest: {name}")
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-version"], check=True, stdout=subprocess.DEVNULL)
    return model_manifest


def narrate(root, story, models, theme):
    import numpy as np
    import soundfile as sf
    from kokoro_onnx import Kokoro
    model_manifest = ready(models)
    voice = story.get("voice", theme.get("voice", "af_heart"))
    speed = theme.get("speed", 1.0)
    kokoro = Kokoro(str(models / "kokoro-v1.0.onnx"), str(models / "voices-v1.0.bin"))
    rate, elapsed, audio, cues, timings = 24000, 0.0, [], [], []
    cache = root / "audio"
    cache.mkdir(exist_ok=True)
    for index, scene in enumerate(story["scenes"]):
        begin = elapsed
        chunks = sentences(scene["narration"])
        sentence_starts = []
        for text in chunks:
            sentence_starts.append(elapsed)
            spoken = text
            for term, replacement in story.get("pronunciations", {}).items():
                spoken = spoken.replace(term, replacement)
            key = hashlib.sha256(json.dumps(
                [spoken, voice, speed, model_manifest], sort_keys=True).encode()).hexdigest()
            path = cache / f"{key}.wav"
            if path.exists():
                samples, sample_rate = sf.read(path, dtype="float32")
            else:
                samples, sample_rate = kokoro.create(spoken, voice=voice, speed=speed, lang="en-us")
                sf.write(path, samples, sample_rate, subtype="PCM_16")
            if sample_rate != rate or not len(samples) or not np.isfinite(samples).all():
                raise ValueError("Invalid synthesized speech")
            duration = len(samples) / rate
            cues.append({"start": elapsed, "end": elapsed + duration, "text": text})
            audio.extend([samples, np.zeros(int(rate * .18), dtype="float32")])
            elapsed += duration + .18
        audio.append(np.zeros(int(rate * .4), dtype="float32"))
        elapsed += .4
        timings.append({"scene": index, "start": begin, "end": elapsed,
                        "sentence_starts": sentence_starts})
        print(f"Narration {index + 1}/{len(story['scenes'])}: {elapsed:.1f}s", flush=True)
    sf.write(root / "narration.wav", np.concatenate(audio), rate, subtype="PCM_16")
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines += [f"{timestamp(cue['start'])} --> {timestamp(cue['end'])}", cue["text"], ""]
    (root / "transcript.vtt").write_text("\n".join(lines), encoding="utf-8")
    (root / "timing.json").write_text(json.dumps({"scenes": timings, "captions": cues}, indent=2))
    return timings, cues, elapsed


def visual_frames(root, scene, ffmpeg):
    from PIL import Image
    if "video" not in scene:
        paths = [local(root, item) for item in scene.get("frames", [scene.get("image")])]
        for path in paths:
            with Image.open(path) as image:
                if image.width * image.height > 32_000_000:
                    raise ValueError("Visual exceeds 32 megapixels")
        return paths
    directory = root / "clip-frames" / hashlib.sha256(scene["video"].encode()).hexdigest()[:16]
    directory.mkdir(parents=True, exist_ok=True)
    # Bounded illustrative sampling; never silently present this as original playback speed.
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(local(root, scene["video"])),
                    "-vf", "fps=2,scale=1120:448:force_original_aspect_ratio=decrease",
                    "-frames:v", "120", str(directory / "%04d.png")], check=True)
    paths = sorted(directory.glob("*.png"))
    if not paths:
        raise ValueError("Video asset has no decodable frames")
    return paths


def visual_index(scene, timing, now, count):
    if "frame_sentences" in scene:
        starts = [timing["sentence_starts"][index] for index in scene["frame_sentences"]]
        return max(0, bisect_right(starts, now) - 1)
    fraction = (now - timing["start"]) / (timing["end"] - timing["start"])
    return max(0, min(count - 1, int(fraction * count)))


def render(root, models, theme_path):
    from PIL import Image, ImageDraw, ImageOps
    import imageio_ffmpeg
    root, models = root.resolve(strict=True), models.resolve(strict=True)
    story = load_storyboard(root)
    theme = json.loads(theme_path.read_text(encoding="utf-8"))
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    timings, cues, duration = narrate(root, story, models, theme)
    if not 30 <= duration <= 600:
        raise ValueError("Narrated briefing must be between 30 seconds and 10 minutes")
    temporary = root / "review.partial.mp4"
    target = root / "review.mp4"
    if target.exists() or temporary.exists():
        raise FileExistsError("Never overwrite an existing render; use a new review revision")
    command = [ffmpeg, "-nostdin", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
               "-i", str(root / "narration.wav"), "-c:v", "libx264", "-preset", "veryfast",
               "-threads", "4", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(temporary)]
    faces = [font(size) for size in (18, 30, 22, 23)]
    sources = [visual_frames(root, scene, ffmpeg) for scene in story["scenes"]]
    images = {}
    previews = []
    scene_index, cue_index = 0, 0
    with subprocess.Popen(command, stdin=subprocess.PIPE) as encoder:
        try:
            for frame in range(math.ceil(duration * FPS)):
                now = frame / FPS
                while scene_index + 1 < len(timings) and now >= timings[scene_index]["end"]:
                    scene_index += 1
                while cue_index + 1 < len(cues) and now >= cues[cue_index]["end"]:
                    cue_index += 1
                scene, timing = story["scenes"][scene_index], timings[scene_index]
                fraction = (now - timing["start"]) / (timing["end"] - timing["start"])
                paths = sources[scene_index]
                path = paths[visual_index(scene, timing, now, len(paths))]
                if path not in images:
                    with Image.open(path) as original:
                        images = {path: ImageOps.contain(original.convert("RGB"), (1160, 440))}
                image = images[path]
                canvas = Image.new("RGB", (WIDTH, HEIGHT), theme["background"])
                draw = ImageDraw.Draw(canvas)
                draw.text((38, 18), str(story["title"])[:95], font=faces[0], fill=theme["muted"])
                draw.text((1170, 18), f"{scene_index + 1}/{len(timings)}",
                          font=faces[0], fill=theme["muted"])
                wrapped(draw, scene["title"], (38, 49), faces[1], theme["foreground"], 1200, 2)
                x, y = (WIDTH - image.width) // 2, 132 + (440 - image.height) // 2
                canvas.paste(image, (x, y))
                for item in scene.get("highlights", []):
                    if item.get("start", 0) <= fraction <= item.get("end", 1):
                        bx, by, bw, bh = item["box"]
                        draw.rectangle((x + bx * image.width, y + by * image.height,
                                        x + (bx + bw) * image.width, y + (by + bh) * image.height),
                                       outline=theme["accent"], width=4)
                wrapped(draw, scene["takeaway"], (38, 584), faces[2], theme["accent"], 1200, 2)
                cue = cues[cue_index]
                if cue["start"] <= now < cue["end"]:
                    # Sentence captions can be long; reveal readable phrases in sequence.
                    phrases = textwrap.wrap(cue["text"], width=94, break_long_words=False)
                    part = min(len(phrases) - 1, int(
                        (now - cue["start"]) / (cue["end"] - cue["start"]) * len(phrases)))
                    wrapped(draw, phrases[part], (38, 657), faces[3], theme["foreground"], 1200, 1)
                draw.rectangle((0, HEIGHT - 4, round(WIDTH * now / duration), HEIGHT),
                               fill=theme["accent"])
                if len(previews) == scene_index and fraction >= .5:
                    previews.append(canvas.copy())
                encoder.stdin.write(canvas.tobytes())
            encoder.stdin.close()
            if encoder.wait() != 0:
                raise RuntimeError("FFmpeg encoder failed")
        except BaseException:
            encoder.kill()
            encoder.wait()
            raise
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(temporary),
                    "-f", "null", "-"], check=True)
    temporary.rename(target)
    chapters = "".join(
        f'<button onclick="const v=document.querySelector(\'video\');v.currentTime={item["start"]:.3f};v.play()">'
        f'{timestamp(item["start"])} - {html.escape(story["scenes"][item["scene"]]["title"])}</button>'
        for item in timings)
    (root / "watch.html").write_text(
        '<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">'
        '<meta http-equiv=Content-Security-Policy content="default-src \'none\'; media-src \'self\' file:; '
        'style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">'
        '<title>Campaign review video</title><style>body{background:#101820;color:#edf5fa;'
        'max-width:1280px;margin:2rem auto;padding:0 1rem;font:18px/1.5 system-ui}'
        'video{width:100%}button{display:block;background:#20394b;color:#edf5fa;border:0;'
        'padding:.8rem;margin:.5rem 0;text-align:left;cursor:pointer}a{color:#7edbd1}</style>'
        f'<h1>{html.escape(story["title"])}</h1>'
        '<video controls preload=metadata src="review.mp4"></video>'
        f'<h2>Chapters</h2>{chapters}<p><a href="report.html">Full illustrated review</a> | '
        '<a href="transcript.vtt">Transcript</a> | <a href="../index.html">Review journal</a></p>',
        encoding="utf-8")
    sheet = Image.new("RGB", (640 * 2, 360 * math.ceil(len(previews) / 2)), theme["background"])
    for index, preview in enumerate(previews):
        sheet.paste(preview.resize((640, 360)), ((index % 2) * 640, (index // 2) * 360))
    sheet.save(root / "contact-sheet.jpg", quality=90)
    result = {"version": 1, "duration_seconds": duration, "width": WIDTH, "height": HEIGHT, "fps": FPS,
              "scenes": len(timings), "voice": story.get("voice", theme.get("voice")),
              "synthesis": "local Kokoro ONNX", "decode_verified": True,
              "video_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    (root / "media.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--theme", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="Check prepared local dependencies/models without inference")
    args = parser.parse_args()
    if args.check:
        ready(args.models)
        print("Prepared local media dependencies and model hashes verified")
    else:
        render(args.directory, args.models, args.theme)
