import sys


def selftest(out):
    """Wavegrab.exe --selftest <folder>: download one of each kind and log the result."""
    import os
    import traceback
    os.makedirs(out, exist_ok=True)
    log = open(os.path.join(out, "selftest.log"), "w", encoding="utf-8")
    try:
        from wavegrab import engine as E
        log.write(f"engine {E.ENGINE_VERSION} ({E.ENGINE_SOURCE}) ffmpeg={E.FFMPEG} deno={E.DENO}\n")
        cases = [("mp3", E.resolve_youtube("https://www.youtube.com/watch?v=dQw4w9WgXcQ")[0][0]),
                 ("mp4", E.resolve_youtube("https://www.youtube.com/watch?v=jNQXAC9IVRw")[0][0]),
                 ("spotify", E.resolve_spotify("https://open.spotify.com/album/4LH4d3cOWNNsVw41Gqt2kv")[0][3])]
        for mode, item in cases:
            try:
                p = E.download(item, mode, {"quality": "192", "resolution": "720p"}, out,
                               lambda s, f: None, lambda: False)
                log.write(f"OK   {mode}: {p}\n")
            except Exception:
                log.write(f"FAIL {mode}: {traceback.format_exc()}\n")
            log.flush()
    except Exception:
        log.write(traceback.format_exc())
    log.close()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
    else:
        from wavegrab.app import main
        main()
