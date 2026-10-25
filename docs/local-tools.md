# Project-local media toolchain

The application runs without paid APIs. FFmpeg, Piper, the voice model, the embedding model, and Node are local tools; none of them send story text to a cloud service. This document records the setup used for the verified real-media run on Windows.

> **Git hygiene:** `.tools/`, `models/*`, and `.env` are ignored. Do not commit binaries, voice weights, or credentials.

## Layout

```text
.tools/
  ffmpeg/bin/ffmpeg.exe
  ffmpeg/bin/ffprobe.exe
  piper/piper/piper.exe
  node/node.exe
models/
  piper/en_US-lessac-medium.onnx
  piper/en_US-lessac-medium.onnx.json
```

## 1. FFmpeg

Download the Windows essentials build, extract it, and flatten the versioned directory:

```powershell
New-Item -ItemType Directory -Force .tools | Out-Null
curl.exe -L --fail -o .tools\ffmpeg.zip https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
Expand-Archive .tools\ffmpeg.zip .tools\ffmpeg-unpack -Force
$inner = Get-ChildItem .tools\ffmpeg-unpack -Directory | Select-Object -First 1
Move-Item $inner.FullName .tools\ffmpeg
Remove-Item .tools\ffmpeg-unpack -Recurse -Force
Remove-Item .tools\ffmpeg.zip -Force
.\.tools\ffmpeg\bin\ffmpeg.exe -version
```

The verified build was FFmpeg 9.0.2 essentials.

## 2. Piper and a voice

The Windows release is a zip containing `piper.exe`, ONNX Runtime, and eSpeak data. Extract it without flattening:

```powershell
curl.exe -L --fail -o .tools\piper.zip https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip
Expand-Archive .tools\piper.zip .tools\piper -Force
Remove-Item .tools\piper.zip -Force
```

Download a voice model and its metadata file. The verified voice is `en_US-lessac-medium`:

```powershell
New-Item -ItemType Directory -Force models\piper | Out-Null
curl.exe -L --fail -o models\piper\en_US-lessac-medium.onnx "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx?download=true"
curl.exe -L --fail -o models\piper\en_US-lessac-medium.onnx.json "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json?download=true"
```

Check synthesis directly:

```powershell
cmd.exe /c "echo This is a local narration test. | .tools\piper\piper\piper.exe --model models\piper\en_US-lessac-medium.onnx --output_file $env:TEMP\piper-test.wav"
.\.tools\ffmpeg\bin\ffprobe.exe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $env:TEMP\piper-test.wav
```

## 3. Local embeddings

`sentence-transformers` is already declared in `pyproject.toml`. Install the project dependencies, then allow the first run to download `all-MiniLM-L6-v2` into the Hugging Face cache:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -c "from sentence_transformers import SentenceTransformer; m=SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); print(m.encode(['local embedding test']).shape)"
```

The verified environment used `sentence-transformers` 5.7.0 and produced 384-dimensional vectors. The model download is approximately 90 MB and requires network access only on the first run.

## 4. Node (optional)

Node is not used by the application at runtime. It is useful for syntax-checking the dependency-free dashboard:

```powershell
curl.exe -L --fail -o .tools\node.zip https://nodejs.org/dist/latest-v22.x/node-v22.23.4-win-x64.zip
Expand-Archive .tools\node.zip .tools\node-unpack -Force
$inner = Get-ChildItem .tools\node-unpack -Directory | Select-Object -First 1
Move-Item $inner.FullName .tools\node
Remove-Item .tools\node-unpack -Recurse -Force
Remove-Item .tools\node.zip -Force
.\.tools\node\node.exe --check frontend/app.js
```

Adjust the version in the URL if that exact release is no longer listed under `https://nodejs.org/dist/latest-v22.x/`.

## 5. Configure `.env`

```dotenv
NEWS_TTS_PIPER_EXECUTABLE=.tools/piper/piper/piper.exe
NEWS_TTS_PIPER_VOICE_MODEL=models/piper/en_US-lessac-medium.onnx
NEWS_TTS_FFMPEG_EXECUTABLE=.tools/ffmpeg/bin/ffmpeg.exe
NEWS_VIDEO_FFMPEG_EXECUTABLE=.tools/ffmpeg/bin/ffmpeg.exe
```

`.env.example` keeps portable defaults (`ffmpeg` and `piper`) for machines where those commands are on `PATH`.

## 6. Verify readiness

Start the API, then check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/system/status | ConvertTo-Json -Depth 4
```

The `capabilities` object should report `true` for `video_ffmpeg`, `tts_ffmpeg`, `piper_tts`, `local_embeddings`, and `local_publishing`. `youtube_publishing` remains `false` until OAuth credentials are added.

Run the automated checks with:

```powershell
$env:TEMP='C:\Users\Shiva\AppData\Local\Temp\opencode'
$env:TMP=$env:TEMP
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
```

The verified real-media smoke run produced a 1920x1080 MP4 with H.264 video, AAC audio, and a `mov_text` subtitle track, then approved it by SHA-256 and exported it through the local publisher. See [`implementation-status.md`](implementation-status.md).

## Licensing note

FFmpeg builds, Piper, and voice models carry their own licenses. Review the FFmpeg build configuration and the selected voice model card before redistributing binaries or publishing generated media commercially. The repository does not redistribute any of these files.
