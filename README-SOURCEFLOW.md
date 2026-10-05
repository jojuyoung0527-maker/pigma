# SourceFlow Public Web Beta

This branch is an isolated deployment carrier for the SourceFlow browser beta.

- Browser -> SourceFlow server -> platform -> verified media -> browser download
- No login-cookie uploads in the public beta
- No DRM/private-access bypass
- Publicly viewable media only; users must have permission to save/use content
- Temporary server storage; completed downloads should be saved promptly

Render runtime uses Python, yt-dlp and static-ffmpeg.
