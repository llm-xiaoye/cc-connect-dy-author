# Third-party dependencies

This repository does not bundle dependency implementations, Chrome data, downloaded media,
or model weights. Installations fetch dependencies listed in `requirements.lock.txt`.
Their licenses and applicable service/content terms remain separate from this repository's MIT license.

Key upstream projects:

- F2: https://github.com/Johnserf-Seed/f2 — the dependency is pinned to a commit and archive digest in the version list.
- yt-dlp: https://github.com/yt-dlp/yt-dlp
- browser-cookie3: https://github.com/borisbabic/browser_cookie3
- MLX Whisper: https://github.com/ml-explore/mlx-examples/tree/main/whisper
- imageio-ffmpeg: https://github.com/imageio/imageio-ffmpeg — includes separately licensed FFmpeg binaries when installed.
- Pillow: https://github.com/python-pillow/Pillow
- cc-connect: https://github.com/chenhg5/cc-connect — optional host integration; not distributed here.

Swift/Vision and Chrome are provided by their respective vendors. Model weights are downloaded
separately; consult their model card and license. Refer to installed package metadata and each
upstream repository for the exact license of your installed version, especially before redistribution.
