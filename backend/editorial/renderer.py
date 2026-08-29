"""Trusted HTML compiler and exact-time Chromium renderer for Editorial Mode."""

from __future__ import annotations

import base64
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from html import escape
from pathlib import Path
from typing import Any

from backend.graphics.browser import discover_chromium
from backend.rendering.binaries import require_ffmpeg
from backend.rendering.process import run_media_process

from .models import EditPlan, EditorialComposition, EditorialElementType


def _script_json(value: Any) -> str:
    """Serialize validated data without allowing a value to close the script tag."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _element(composition: EditorialComposition, element_id: str):
    return next((item for item in composition.elements if item.id == element_id), None)


def _archive_markup(composition: EditorialComposition) -> str:
    year = _element(composition, "year")
    elon = _element(composition, "elon")
    rulers = _element(composition, "rulers")
    ruler_count = rulers.count if rulers else 10
    nodes = "".join(
        f'<div class="ruler-node" data-ruler="{index}"><span>{index + 1:02d}</span></div>'
        for index in range(ruler_count)
    )
    return f"""
      <section class="composition archive-canvas" data-composition="{escape(composition.id)}">
        <div class="grain"></div>
        <div class="research-layer">
          <div class="technical-line line-a"></div><div class="technical-line line-b"></div>
          <div id="year" class="year editorial-element">{escape(year.text if year else "")}</div>
          <div id="photo" class="archive-photo editorial-element">
            <div class="photo-art"><div class="portrait-head"></div><div class="portrait-body"></div></div>
            <div class="asset-tag">ARCHIVE PHOTOGRAPH · EVIDENCE</div>
          </div>
          <div id="document" class="document editorial-element">
            <div class="paper-index">ARCHIVE 1949 / FILE 07</div>
            <h2>THE MARS PROJECT</h2>
            <p>A TECHNICAL TALE</p>
            <div class="rule"></div>
            <p class="document-copy">A study of an expedition to Mars and the systems imagined to govern a distant settlement.</p>
            <div id="passage" class="passage editorial-element"></div>
            <div class="paper-stamp">PROJECT MARS</div>
          </div>
          <div id="rulers" class="ruler-grid editorial-element">{nodes}</div>
          <div class="draft-label">FIG. 01 · AUTHORITY STRUCTURE</div>
        </div>
        <div id="blackout" class="blackout"></div>
        <div id="elon" class="elon editorial-element">{escape(elon.text if elon else "")}</div>
      </section>
    """


def compile_edit_plan_html(plan: EditPlan) -> str:
    """Compile a validated plan into trusted, self-contained preview/render HTML."""
    markup = "".join(_archive_markup(item) for item in plan.compositions)
    payload = _script_json(plan.model_dump(mode="json"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--charcoal:#111315;--charcoal-2:#1b1d1f;--ivory:#e9dfc6;--rust:#b9532f;--blue:#6f91a6;--ink:#25211d}}
*{{box-sizing:border-box}} html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#000}}
body{{font-family:"DejaVu Sans Condensed","Liberation Sans Narrow",sans-serif}}
#stage{{position:relative;width:{plan.width}px;height:{plan.height}px;overflow:hidden;background:#000}}
.composition{{position:absolute;inset:0;display:none;overflow:hidden;background:var(--charcoal);color:var(--ivory)}}
.research-layer{{position:absolute;inset:0;transform-origin:50% 50%}}
.grain{{position:absolute;inset:0;z-index:80;pointer-events:none;opacity:.11;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.82' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.32'/%3E%3C/svg%3E")}}
.technical-line{{position:absolute;background:var(--blue);opacity:.28}} .line-a{{left:74px;top:120px;width:1px;height:1640px}} .line-b{{left:74px;right:74px;top:1704px;height:1px}}
.year{{position:absolute;left:72px;top:105px;font-size:250px;font-weight:900;line-height:.84;letter-spacing:-10px;color:var(--ivory);text-shadow:0 8px 28px #0008}}
.archive-photo{{position:absolute;left:76px;top:420px;width:610px;height:610px;padding:18px;background:#c9bea4;box-shadow:0 30px 80px #0008;transform:rotate(-2.4deg)}}
.photo-art{{position:relative;width:100%;height:520px;overflow:hidden;background:radial-gradient(circle at 50% 27%,#a59c88 0 12%,transparent 13%),linear-gradient(145deg,#625f57,#b3aa97 46%,#494943)}}
.portrait-head{{position:absolute;left:220px;top:100px;width:130px;height:165px;border-radius:48% 48% 42% 42%;background:#353735;box-shadow:24px 5px 0 #77756c}}
.portrait-body{{position:absolute;left:120px;top:245px;width:360px;height:340px;border-radius:48% 48% 0 0;background:#292c2c}}
.asset-tag{{padding-top:18px;color:#3b3731;font-size:22px;letter-spacing:3px}}
.document{{position:absolute;right:54px;top:630px;width:590px;height:760px;padding:64px 56px;background:var(--ivory);color:var(--ink);box-shadow:0 35px 95px #000b;transform:rotate(2deg)}}
.paper-index{{font:18px monospace;letter-spacing:2px;color:#6e675b}} .document h2{{margin:95px 0 6px;font:700 62px/1 "DejaVu Serif",serif;letter-spacing:1px}} .document>p{{margin:0;font:22px "DejaVu Serif",serif;letter-spacing:3px}}
.document .rule{{height:2px;background:#2b2925;margin:24px 0 42px}} .document .document-copy{{font:27px/1.65 "DejaVu Serif",serif;letter-spacing:0}}
.passage{{position:absolute;left:54px;right:54px;top:508px;height:22px;border-bottom:8px solid var(--rust);background:#b9532f26;transform-origin:left center}}
.paper-stamp{{position:absolute;right:38px;bottom:40px;padding:10px 16px;border:4px solid #9f4429;color:#9f4429;font-weight:800;letter-spacing:3px;transform:rotate(-8deg);opacity:.82}}
.ruler-grid{{position:absolute;left:82px;right:72px;bottom:170px;display:grid;grid-template-columns:repeat(5,1fr);gap:24px}}
.ruler-node{{height:86px;border:1px solid #6f91a680;position:relative;color:#9db4c0;background:#182027}}
.ruler-node:before{{content:"";position:absolute;left:12px;right:12px;top:50%;height:1px;background:#6f91a680}} .ruler-node span{{position:absolute;right:10px;top:8px;font:17px monospace}}
.ruler-node.focus{{border-color:var(--rust);background:#4e271d;color:#ffd8bd;box-shadow:inset 0 0 0 3px #b9532f}}
.draft-label{{position:absolute;left:80px;bottom:64px;font:18px monospace;letter-spacing:3px;color:#6f91a6}}
.blackout{{position:absolute;inset:0;z-index:100;background:#050505;opacity:0}}
.elon{{position:absolute;z-index:110;inset:0;display:flex;align-items:center;justify-content:center;font-size:210px;font-weight:900;letter-spacing:8px;color:var(--ivory);opacity:0}}
</style></head><body><main id="stage">{markup}</main>
<script>
"use strict";
const PLAN={payload};
const clamp=v=>Math.max(0,Math.min(1,v));
const ease=v=>{{v=clamp(v);return v*v*(3-2*v)}};
function reset(root){{
  root.querySelectorAll('.editorial-element').forEach(el=>{{el.style.opacity='0';el.style.filter='none';el.style.transform='none'}});
  root.querySelectorAll('.ruler-node').forEach(el=>{{el.style.opacity='0';el.classList.remove('focus')}});
  root.querySelector('.research-layer').style.cssText='';
  root.querySelector('.blackout').style.opacity='0';
  const passage=root.querySelector('#passage'); if(passage) passage.style.transform='scaleX(0)';
}}
function applyEvent(root,event,t){{
  if(t<event.time)return;
  const p=event.duration===0?1:ease((t-event.time)/event.duration);
  const target=event.target==='canvas'?root:root.querySelector('#'+CSS.escape(event.target));
  if(!target)return;
  switch(event.action){{
    case 'fade': target.style.opacity=String(p); break;
    case 'fadeUp': target.style.opacity=String(p);target.style.transform=`translateY(${{(1-p)*70}}px)`;break;
    case 'slideInLeft': target.style.opacity=String(p);target.style.transform=`translateX(${{(p-1)*520}}px) rotate(-2.4deg)`;break;
    case 'slideInRight': target.style.opacity=String(p);target.style.transform=`translateX(${{(1-p)*520}}px)`;break;
    case 'scaleIn': target.style.opacity=String(p);target.style.transform=`scale(${{.78+.22*p}})`;break;
    case 'slowPush': target.style.opacity='1';target.style.transform=`scale(${{1+.04*p}})`;break;
    case 'paperSlide': target.style.opacity=String(p);target.style.transform=`translate(${{(1-p)*520}}px,${{(1-p)*90}}px) rotate(${{7-5*p}}deg)`;break;
    case 'underline': case 'highlight': target.style.opacity=String(p);target.style.transform=`scaleX(${{p}})`;break;
    case 'drawLine': target.style.opacity=String(p);target.style.transform=`scaleX(${{p}})`;break;
    case 'staggerIn': {{const nodes=[...target.querySelectorAll('.ruler-node')];nodes.forEach((node,i)=>{{const q=ease(clamp(p*1.65-i/nodes.length*.65));node.style.opacity=String(q);node.style.transform=`translateY(${{(1-q)*34}}px)`}});target.style.opacity='1';break;}}
    case 'dimOthers': {{const focus=Number(event.value)||0;target.querySelectorAll('.ruler-node').forEach((node,i)=>{{node.style.opacity=String(i===focus?1:1-.78*p)}});target.style.opacity='1';break;}}
    case 'focusOne': {{const focus=Number(event.value)||0;const node=target.querySelectorAll('.ruler-node')[focus];if(node&&p>.2)node.classList.add('focus');target.style.opacity='1';break;}}
    case 'collapseToBlack': {{const layer=root.querySelector('.research-layer');layer.style.opacity=String(1-p);layer.style.transform=`scale(${{1-.08*p}})`;root.querySelector('.blackout').style.opacity=String(p);break;}}
    case 'hardCut': target.style.opacity='1';break;
  }}
}}
window.renderAt=function(globalTime){{
  PLAN.compositions.forEach((composition,index)=>{{
    const root=document.querySelectorAll('.composition')[index];
    const active=globalTime>=composition.start&&globalTime<=composition.start+composition.duration;
    root.style.display=active?'block':'none'; if(!active)return;
    const t=globalTime-composition.start;reset(root);composition.events.forEach(event=>applyEvent(root,event,t));
  }});
  return true;
}};
window.renderAt(0);window.__editorialReady=true;
</script></body></html>"""


class _CDP:
    def __init__(self, url: str) -> None:
        try:
            import websocket  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Editorial rendering requires the existing dev dependency websocket-client"
            ) from exc
        self._websocket = websocket
        self.ws = websocket.create_connection(url, timeout=60)
        self._ids = itertools.count(1)

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = next(self._ids)
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            return message.get("result", {})

    def close(self) -> None:
        self.ws.close()


class EditorialRenderer:
    """Render exact timestamps from trusted composition HTML into an MP4."""

    def __init__(self, chromium: Path | None = None) -> None:
        self.chromium = chromium or discover_chromium()

    def render(self, plan: EditPlan, output: Path, *, preview_html: Path | None = None) -> Path:
        if self.chromium is None:
            raise RuntimeError("Chromium is required for Editorial Mode rendering")
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        html = compile_edit_plan_html(plan)
        if preview_html is not None:
            preview_html.parent.mkdir(parents=True, exist_ok=True)
            preview_html.write_text(html, encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="lvs-editorial-", dir=output.parent) as temp_name:
            work = Path(temp_name)
            document = work / "composition.html"
            document.write_text(html, encoding="utf-8")
            frames = work / "frames"
            frames.mkdir()
            self._capture_frames(plan, document, frames, work / "chrome-profile")
            temporary = work / "render.mp4"
            ffmpeg = require_ffmpeg()
            run_media_process([
                str(ffmpeg), "-y", "-framerate", str(plan.fps),
                "-i", str(frames / "frame-%06d.png"),
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
            ], timeout=max(120.0, plan.duration * 20))
            publish = output.with_name(f".{output.name}.editorial.tmp")
            shutil.copyfile(temporary, publish)
            os.replace(publish, output)
        return output

    def _capture_frames(self, plan: EditPlan, document: Path, frames: Path, profile: Path) -> None:
        profile.mkdir(parents=True)
        args = [
            str(self.chromium), "--headless=new", "--disable-gpu", "--disable-extensions",
            "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
            "--force-device-scale-factor=1", f"--window-size={plan.width},{plan.height}",
            "--remote-debugging-port=0", "--remote-allow-origins=*",
            "--allow-file-access-from-files", f"--user-data-dir={profile}", "about:blank",
        ]
        if os.environ.get("LVS_CHROMIUM_NO_SANDBOX", "").strip().lower() in {"1", "true", "yes"}:
            args.insert(2, "--no-sandbox")
        process = subprocess.Popen(
            args, env=dict(os.environ, HOME=str(profile)), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client: _CDP | None = None
        try:
            port_file = profile / "DevToolsActivePort"
            for _ in range(100):
                if port_file.is_file():
                    break
                if process.poll() is not None:
                    raise RuntimeError("Chromium exited before exposing Editorial renderer control")
                time.sleep(0.1)
            if not port_file.is_file():
                raise RuntimeError("Chromium did not expose Editorial renderer control")
            port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
            page_url = None
            for _ in range(50):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as response:
                        targets = json.load(response)
                    page_url = next(item["webSocketDebuggerUrl"] for item in targets if item["type"] == "page")
                    break
                except Exception:
                    time.sleep(0.1)
            if not page_url:
                raise RuntimeError("Chromium did not create an Editorial render page")
            client = _CDP(page_url)
            client.command("Page.enable")
            client.command("Runtime.enable")
            client.command("Emulation.setDeviceMetricsOverride", {
                "width": plan.width, "height": plan.height,
                "deviceScaleFactor": 1, "mobile": False,
            })
            client.command("Page.navigate", {"url": document.resolve().as_uri()})
            for _ in range(100):
                ready = client.command("Runtime.evaluate", {
                    "expression": "window.__editorialReady === true", "returnByValue": True,
                })
                if ready.get("result", {}).get("value") is True:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("Editorial composition did not become ready")
            frame_count = round(plan.duration * plan.fps)
            for frame in range(frame_count):
                timestamp = frame / plan.fps
                client.command("Runtime.evaluate", {
                    "expression": f"window.renderAt({timestamp:.12f})", "returnByValue": True,
                })
                screenshot = client.command("Page.captureScreenshot", {
                    "format": "png", "fromSurface": True, "captureBeyondViewport": False,
                })
                (frames / f"frame-{frame:06d}.png").write_bytes(base64.b64decode(screenshot["data"]))
        finally:
            if client is not None:
                client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
