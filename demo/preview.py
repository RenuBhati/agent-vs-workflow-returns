"""Silent looping preview of the live part of the demo, for the README (animated WebP)."""
import json, subprocess, sys
import imageio_ffmpeg
WAIT_S, SPEED, HOLD = 1.2, 1.6, 1.4
frames = json.load(open("rec/frames.json"))["frames"]
meta = json.load(open("rec/beats.json")); waits = meta["waits"]; beat = {b["name"]: b for b in meta["beats"]}
clip = []
for name in ["workflow_ask", "workflow_confirm", "agent_ask", "agent_confirm", "return_setup", "yes_not_confirm"]:
    a0, a1 = beat[name]["start"], beat[name]["end"]
    def sq(x):
        out = x - a0
        for w in waits:
            lo, hi = max(w["start"], a0), w["end"]
            if hi <= a0 or lo >= x: continue
            span = min(hi, x) - lo
            out -= span - span * WAIT_S / (w["end"] - w["start"])
        return out / SPEED
    before = [f for f in frames if f["t"] <= a0]
    seq = ([(before[-1]["file"], 0.0)] if before else []) + [(f["file"], sq(f["t"])) for f in frames if a0 < f["t"] <= a1]
    length = sq(a1) + HOLD
    for i, (f, t) in enumerate(seq):
        nxt = seq[i + 1][1] if i + 1 < len(seq) else length
        if nxt - t > 0.0005: clip.append((f, nxt - t))
with open("preview.txt", "w") as fh:
    for f, d in clip: fh.write(f"file '{f}'\nduration {d:.4f}\n")
    fh.write(f"file '{clip[-1][0]}'\n")
total = sum(d for _, d in clip)
ff = imageio_ffmpeg.get_ffmpeg_exe()
subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "preview.txt",
                "-vf", "fps=10,scale=960:-1:flags=lanczos", "-c:v", "libwebp_anim", "-lossless", "0",
                "-quality", "70", "-compression_level", "6", "-loop", "0", "-t", f"{total:.2f}", sys.argv[1]], check=True)
print(f"{sys.argv[1]}: {total:.1f} s")
