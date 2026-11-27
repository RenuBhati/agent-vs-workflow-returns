# Demo video

These scripts make the walkthrough video in [`docs/demo.mp4`](../docs/demo.mp4) and the looping preview in [`docs/demo-preview.webp`](../docs/demo-preview.webp).

The video is made in three parts, then put together:

1. **Live scenes.** `record.mjs` drives the demo page in headless Chrome and saves a timestamped frame every time the screen changes, plus the start and end time of each scene and each model wait.
2. **Slides.** `slides.html` holds the explainer slides, one `<section class="slide">` each. `shoot.mjs` saves each one as a 1280×800 JPEG.
3. **Narration.** `build.py` reads every line of narration aloud with [Kokoro](https://github.com/thewh1teagle/kokoro-onnx), a small text-to-speech model that runs locally. It lays the slides and scenes end to end, keeps each one on screen at least as long as its narration, and encodes the MP4 with ffmpeg.

The model is slow on a laptop, so `build.py` shortens every model wait to 2.5 seconds. The recording shows a running timer with the real time during each wait, so the speed-up is visible.

## What you need

- The project set up as in the main [README](../README.md): Postgres running, migrations applied, Ollama with `qwen3.5:4b`.
- Google Chrome. The scripts look for it at the standard macOS path. Elsewhere, set `CHROME` to the Chrome binary.
- Node.js 18 or later.
- [uv](https://docs.astral.sh/uv/).

ffmpeg comes with the `imageio-ffmpeg` package, so you don't install it separately.

## One-time setup

Run these from the `demo/` folder:

```bash
npm install
uv venv .venv
uv pip install --python .venv -r requirements.txt
mkdir -p models
curl -L -o models/kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -o models/voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

The two model files are about 340 MB together. They, `node_modules/`, `.venv/` and everything generated are git-ignored.

## Make the video

**1. Start the app** from the project root, in its own terminal:

```bash
make serve
```

This starts the server on port 8000 with `ALLOW_RESET=true`, which the recorder needs: it resets the shop before each conversation.

**2. Record the live scenes** (from `demo/`):

```bash
node record.mjs
```

Most of the time goes on the agent's model calls: the last recording took about 3.5 minutes on an 8 GB laptop. It writes `rec/frames/`, `rec/frames.json` and `rec/beats.json`. You can stop the server once it's done.

**3. Screenshot the slides:**

```bash
node shoot.mjs slides.html slides
```

Each slide prints `ok`, or `OVERFLOW` if its content runs off the slide. Fix any overflow in `slides.html` before going on.

**4. Build the video:**

```bash
.venv/bin/python build.py demo.mp4
```

It prints each segment with its length and the length of its narration, then the total. Building takes a few minutes, most of it spent on speech.

**5. Build the preview** (optional), a silent sped-up loop of the live scenes:

```bash
.venv/bin/python preview.py demo-preview.webp
```

**6. Copy into the repo:**

```bash
cp demo.mp4 ../docs/demo.mp4
cp demo-preview.webp ../docs/demo-preview.webp
```

If the video length changes noticeably, update the length in the main README's link text.

## Changing the content

- **Slide text** lives in `slides.html`. **Narration** lives in `PLAN` in `build.py`, as one entry per segment: `("slide", "<section id>", "text")` or `("scene", "<scene name>", "text")`. When you add a slide, add its `<section>` and a `PLAN` entry with the same id, in the order it should play.
- **Results are written out by hand** in both places: the held-out table, the experiment table, and the numbers in the narration. When `RESULTS.md` or `EXPERIMENT.md` changes, update both files.
- **Numbers from the live scenes** (model calls, prompt tokens, the agent's first reply time) are read from the recording, so the narration matches whatever happened in your run.
- **Scenes** are defined in `record.mjs`. Each scene is a `beat("name", async () => { ... })` block, which records when it starts and ends. A new scene needs a matching `("scene", "name", ...)` entry in `PLAN`; `preview.py` lists the scenes it loops.
- **Voice and pacing** are set at the top of `build.py`: `VOICE` (default `af_heart`), `SPEED`, `WAIT_S` (how long each shortened wait lasts), and the pauses before and after each line.

Only rerun the step you changed: new slides need steps 3 and 4, new narration only step 4, and new live scenes everything from step 1.
