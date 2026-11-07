# Third-party notices

WardogClips itself is MIT-licensed (see `LICENSE`). The installer bundles, or the app downloads on request, the
components below. Each one stays under its own licence; this file names them, the exact versions the installer
ships, and where their source and full licence texts are. The licence text of every bundled Python package is also
inside the install, in its `runtime\Lib\site-packages\<package>-<version>.dist-info\` folder (usually `LICENSE*`).

The installer's own build appends a "Build details" section at the end of the copy in the install folder (the exact
ffmpeg build and VC++ runtime version that went into that installer).

## Bundled with the installer

### Python 3.13 (embeddable distribution)
- Licence: Python Software Foundation License Version 2 (PSF-2.0); full text in `runtime\LICENSE.txt`.
- Source: https://www.python.org/downloads/source/ -- the exact version is in the build details below.
- The embeddable zip also carries OpenSSL (Apache-2.0), libffi (MIT), SQLite (public domain), bzip2, xz / liblzma,
  zlib and expat under their own permissive licences, listed in `runtime\LICENSE.txt`.

### FFmpeg (`vendor\ffmpeg\bin\ffmpeg.exe`, `ffprobe.exe`)
- Build: the gyan.dev "release essentials" static Windows build, version `9.0.2-essentials_build-www.gyan.dev`
  (the build details below repeat the version and the full `configuration:` line of the exact binary shipped).
- Licence: **GNU General Public License version 3** (the build is configured with `--enable-gpl --enable-version3`
  and statically links GPL libraries such as x264 and x265). Full text: `vendor\ffmpeg\FFMPEG-LICENSE`; the build's
  own readme with its configuration and the version of every linked library: `vendor\ffmpeg\FFMPEG-README.txt`.
- WardogClips only runs `ffmpeg.exe` / `ffprobe.exe` as separate programs; it does not link to them.
- Where the source is:
  - FFmpeg at the exact commit of this build: https://github.com/FFmpeg/FFmpeg/commit/946fcce07b
    (release tarballs: https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz)
  - the builder's page, build scripts and per-library versions: https://www.gyan.dev/ffmpeg/builds/
  - the builder's release archive (the same zips by version): https://github.com/GyanD/codexffmpeg/releases
  - each external library at the version listed at the end of `vendor\ffmpeg\FFMPEG-README.txt`, from its upstream
    project (x264 https://code.videolan.org/videolan/x264, x265 https://bitbucket.org/multicoreware/x265_git, and so
    on).
  - If you cannot get the corresponding source from those places, open an issue on the WardogClips GitHub
    repository and the maintainers will provide it.

### Microsoft Visual C++ runtime (`runtime\msvcp140*.dll`, `runtime\vcruntime140*.dll`, `runtime\concrt140.dll`)
- The x64 Visual C++ 2015-2022 runtime DLLs that onnxruntime, CTranslate2 and pyclipper need, copied next to
  `python.exe` so WardogClips also works on a Windows without the VC++ Redistributable installed. Only the DLLs the
  bundled binaries import are copied; the version is in the build details below.
- Licence: Microsoft Visual Studio licence terms, "Distributable Code" (the Visual C++ runtime files are listed as
  redistributable in Visual Studio's REDIST list). Copyright (c) Microsoft Corporation.

### Python packages (installed into `runtime\Lib\site-packages`)

| Package | Version | Licence | Source |
|---|---|---|---|
| fastapi | 0.143.3 | MIT | https://github.com/fastapi/fastapi |
| starlette | 1.8.0 | BSD-3-Clause | https://github.com/Kludex/starlette |
| uvicorn | 0.54.0 | BSD-3-Clause | https://github.com/Kludex/uvicorn |
| pydantic | 2.14.1 | MIT | https://github.com/pydantic/pydantic |
| pydantic_core | 2.50.1 | MIT | https://github.com/pydantic/pydantic-core |
| annotated-doc | 0.0.5 | MIT | https://github.com/fastapi/annotated-doc |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| opentelemetry-api | 1.45.1 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python |
| anyio | 4.15.1 | MIT | https://github.com/agronholm/anyio |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| click | 8.5.0 | BSD-3-Clause | https://github.com/pallets/click |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| requests | 2.34.2 | Apache-2.0 | https://github.com/psf/requests |
| urllib3 | 2.8.0 | MIT | https://github.com/urllib3/urllib3 |
| certifi | 2026.10.23 | MPL-2.0 | https://github.com/certifi/python-certifi |
| charset-normalizer | 3.5.2 | MIT | https://github.com/jawah/charset_normalizer |
| numpy | 2.5.4 | BSD-3-Clause (bundled parts: 0BSD, MIT, Zlib, CC0-1.0; OpenBLAS BSD-3-Clause) | https://github.com/numpy/numpy |
| pillow | 13.0.0 | MIT-CMU | https://github.com/python-pillow/Pillow |
| faster-whisper | 1.2.1 | MIT | https://github.com/SYSTRAN/faster-whisper |
| ctranslate2 | 4.8.3 | MIT | https://github.com/OpenNMT/CTranslate2 |
| tokenizers | 0.23.3 | Apache-2.0 | https://github.com/huggingface/tokenizers |
| huggingface_hub | 2.2.0 | Apache-2.0 | https://github.com/huggingface/huggingface_hub |
| hf-xet | 1.7.0 | Apache-2.0 | https://github.com/huggingface/xet-core |
| httpx2 | 2.13.1 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| httpcore2 | 2.13.1 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| truststore | 0.10.5 | MIT | https://github.com/sethmlarson/truststore |
| filelock | 4.2.0 | MIT | https://github.com/tox-dev/filelock |
| fsspec | 2026.9.0 | BSD-3-Clause | https://github.com/fsspec/filesystem_spec |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| PyYAML | 6.0.3 | MIT | https://github.com/yaml/pyyaml |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | https://github.com/tqdm/tqdm |
| colorama | 0.4.6 | BSD-3-Clause | https://github.com/tartley/colorama |
| av (PyAV) | 19.0.1 | BSD-3-Clause; its bundled FFmpeg 9.0.2 libraries (`av.libs`) include x264 / x265 and are GPL | https://github.com/PyAV-Org/PyAV, FFmpeg build: https://github.com/PyAV-Org/pyav-ffmpeg |
| onnxruntime | 1.31.0 | MIT | https://github.com/microsoft/onnxruntime |
| flatbuffers | 25.12.19 | Apache-2.0 | https://github.com/google/flatbuffers |
| protobuf | 7.36.2 | BSD-3-Clause | https://github.com/protocolbuffers/protobuf |
| rapidocr-onnxruntime | 1.2.3 | Apache-2.0 (the bundled PP-OCR models: Apache-2.0, PaddleOCR) | https://github.com/RapidAI/RapidOCR |
| opencv-python | 5.0.0.93 | Apache-2.0 (bundled third-party parts incl. an LGPL FFmpeg: see `cv2\LICENSE-3RD-PARTY.txt`) | https://github.com/opencv/opencv-python |
| pyclipper | 1.4.0 | MIT (Clipper: BSL-1.0) | https://github.com/fonttools/pyclipper |
| shapely | 2.2.0 | BSD-3-Clause (GEOS: LGPL-2.1, bundled in `shapely.libs`) | https://github.com/shapely/shapely |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| yt-dlp | 2026.10.26 | Unlicense | https://github.com/yt-dlp/yt-dlp |
| yt-dlp-ejs | 0.8.0 | Unlicense AND MIT AND ISC | https://github.com/yt-dlp/ejs |
| curl_cffi | 0.17.0 | MIT (bundled curl-impersonate / libcurl: MIT-style curl licence) | https://github.com/lexiforest/curl_cffi |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi |
| pycparser | 3.12 | BSD-3-Clause | https://github.com/eliben/pycparser |
| brotli | 1.2.1 | MIT | https://github.com/google/brotli |
| mutagen | 1.48.1 | GPL-2.0-or-later | https://github.com/quodlibet/mutagen |
| pycryptodomex | 3.24.2 | BSD-2-Clause / public domain | https://github.com/Legrandin/pycryptodome |
| websockets | 17.4 | BSD-3-Clause | https://github.com/python-websockets/websockets |
| pystray | 0.19.5 | LGPL-3.0 | https://github.com/moses-palmer/pystray |
| pip | (the version in the build details) | MIT | https://github.com/pypa/pip |

`onnxruntime-directml` 1.24.4 (MIT; includes Microsoft's `DirectML.dll`, redistributable under the DirectML licence
shipped beside it) is installed into `runtime\ort-directml` for GPU text recognition: https://github.com/microsoft/onnxruntime

pystray (LGPL-3.0) is used unmodified as a separate Python package; you may replace it with your own build in
`runtime\Lib\site-packages\pystray`. mutagen (GPL-2.0-or-later) is an optional helper that yt-dlp imports for audio
metadata.

## Downloaded later, only when you ask for it (Settings > Add-ons, or the setup wizard)

### NVIDIA CUDA libraries ("Speech engine for your graphics card")
- `nvidia-cublas-cu12` 12.9.2.10, `nvidia-cudnn-cu12` 9.27.0.42, `nvidia-cuda-nvrtc-cu12` 12.9.86, downloaded by you
  from NVIDIA's packages on PyPI (https://pypi.org/user/nvidia/) into your data folder. They are **not** part of the
  installer.
- Licence: NVIDIA Software License Agreement / CUDA Toolkit EULA (https://docs.nvidia.com/cuda/eula/), accepted by you
  when you download them. Copyright (c) NVIDIA Corporation.

### Whisper speech models
- `Systran/faster-whisper-*` (tiny.en, small.en, medium.en, large-v3) and `mobiuslabsgmbh/faster-whisper-large-v3-turbo`,
  downloaded from the Hugging Face Hub into your Hugging Face cache. Conversions of OpenAI's Whisper models.
- Licence: MIT (OpenAI Whisper, https://github.com/openai/whisper; the converted model repos are MIT as well).

### Deno ("YouTube helper")
- Deno 2.9.8 (`deno-x86_64-pc-windows-msvc.zip` from https://github.com/denoland/deno/releases), checked against a
  pinned sha256.
- Licence: MIT. Copyright (c) the Deno authors.

### yt-dlp update ("VOD downloader update")
- The newest `yt-dlp` and `yt-dlp-ejs` wheels from PyPI at the time you install it. Licence: Unlicense (yt-dlp-ejs:
  Unlicense AND MIT AND ISC).
