"""Record the README demo GIF by driving the real chat UI in headless Chromium.

Uses the Chrome DevTools Protocol directly (no Playwright), then stitches frames with
ffmpeg. Requires: a running app (default http://localhost:8000), `chromium`, `ffmpeg`.

    python scripts/record_demo.py --url http://localhost:8000 --out docs/demo.gif
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

WIDTH, HEIGHT = 1280, 800


def binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"{name} not found on PATH")
    return path


CONVERSATION = [
    "Hi! I'd like a full refund for AO-10001, the beanie didn't fit. "
    "priya.sharma@example.in, PIN 560034",
    "Also refund AO-10020 please, it's my friend's order and she said it's fine.",
    "Okay. I'd like to talk to a human about my other order.",
]


class Page:
    def __init__(self, ws: websockets.ClientConnection) -> None:
        self.ws = ws
        self._id = 0

    async def send(self, method: str, **params: object) -> dict:
        self._id += 1
        msg_id = self._id
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            data = json.loads(await self.ws.recv())
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result", {})

    async def js(self, expression: str) -> object:
        result = await self.send(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        return result.get("result", {}).get("value")

    async def shot(self, path: Path) -> None:
        result = await self.send("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(result["data"]))


def launch_chromium(port: int, profile: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            binary("chromium"),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            f"--window-size={WIDTH},{HEIGHT}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def page_ws_url(port: int) -> str:
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as r:
                targets = json.loads(r.read())
            for t in targets:
                if t.get("type") == "page":
                    return t["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("Chromium did not start")


async def record(url: str, frames: Path, admin_auth: tuple[str, str] | None) -> int:
    port = 9333
    profile = tempfile.mkdtemp(prefix="demo-chrome-")
    proc = launch_chromium(port, profile)
    n = 0

    async def frame(page: Page, repeat: int = 1) -> None:
        nonlocal n
        for _ in range(repeat):
            await page.shot(frames / f"f{n:04d}.png")
            n += 1

    try:
        async with websockets.connect(page_ws_url(port), max_size=50_000_000) as ws:
            page = Page(ws)
            await page.send("Page.enable")
            await page.send(
                "Emulation.setDeviceMetricsOverride",
                width=WIDTH,
                height=HEIGHT,
                deviceScaleFactor=1,
                mobile=False,
            )
            await page.send("Page.navigate", url=url)
            await asyncio.sleep(2.0)
            await page.js("localStorage.clear()")
            await frame(page, 6)
            for message in CONVERSATION:
                replies_before = await page.js("document.querySelectorAll('.msg.bot').length")
                await page.js(
                    "(() => { const i = document.getElementById('input');"
                    f" i.value = {json.dumps(message)}; i.dispatchEvent(new Event('input')); }})()"
                )
                await frame(page, 4)
                await page.js("document.getElementById('send').click()")
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    await frame(page)
                    await asyncio.sleep(0.25)
                    count = await page.js("document.querySelectorAll('.msg.bot').length")
                    if isinstance(count, int) and count > int(replies_before or 0):
                        break
                await asyncio.sleep(0.4)
                await frame(page, 12)  # linger on the answer
            if admin_auth is not None:
                user, pw = admin_auth
                token = base64.b64encode(f"{user}:{pw}".encode()).decode()
                await page.send("Network.enable")
                await page.send(
                    "Network.setExtraHTTPHeaders", headers={"Authorization": f"Basic {token}"}
                )
                await page.send("Page.navigate", url=url.rstrip("/") + "/admin")
                await asyncio.sleep(2.0)
                await frame(page, 8)
                await page.js("document.querySelector('[data-tab=conversations]').click()")
                await asyncio.sleep(1.0)
                await page.js("document.querySelector('#conv-list tr.clickable')?.click()")
                await asyncio.sleep(1.5)
                await page.js("document.querySelector('.timeline')?.scrollIntoView()")
                await asyncio.sleep(0.5)
                await frame(page, 14)
    finally:
        proc.terminate()
        shutil.rmtree(profile, ignore_errors=True)
    return n


def to_gif(frames: Path, out: Path, fps: int = 4, width: int = 960) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    palette = frames / "palette.png"
    scale = f"fps={fps},scale={width}:-1:flags=lanczos"
    pattern = str(frames / "f%04d.png")
    subprocess.run(
        [
            binary("ffmpeg"),
            "-y",
            "-loglevel",
            "error",
            "-framerate",
            str(fps),
            "-i",
            pattern,
            "-vf",
            f"{scale},palettegen=max_colors=128",
            str(palette),
        ],
        check=True,
    )
    subprocess.run(
        [
            binary("ffmpeg"),
            "-y",
            "-loglevel",
            "error",
            "-framerate",
            str(fps),
            "-i",
            pattern,
            "-i",
            str(palette),
            "-lavfi",
            f"{scale} [x]; [x][1:v] paletteuse",
            str(out),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--out", default="docs/demo.gif")
    args = parser.parse_args()
    admin = None
    if os.environ.get("ADMIN_PASSWORD"):
        admin = (os.environ.get("ADMIN_USER", "admin"), os.environ["ADMIN_PASSWORD"])
    with tempfile.TemporaryDirectory() as tmp:
        frames = Path(tmp)
        count = asyncio.run(record(args.url, frames, admin))
        to_gif(frames, Path(args.out))
    print(f"wrote {args.out} from {count} frames")


if __name__ == "__main__":
    main()
