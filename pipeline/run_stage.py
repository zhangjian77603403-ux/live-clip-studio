#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直播切片 · 执行器（被工作台以子进程方式拉起）

用法：python run_stage.py --proj <项目目录> --stage assets|shots|final

stage=assets：Z-Image 出「商品主视图」1 张 + 「主播坐桌前 + 商品摆桌上」定妆照 N 个 seed
              结果写进 03_商品素材/product.png 与 04_分镜参考/cand/host_<seed>.png
              定稿由人在工作台里点选，不由本脚本决定
stage=shots ：H3 首尾同图（每镜 first = last = 同一张定妆照）逐镜生成
              素材写进 02_生成源/raw/S01.mp4 …，提示词写 02_生成源/prompts/
stage=final ：整段直拼（不剪帧，切点落在说完之后）→ 按交付尺寸缩放裁切 → 烧中文字幕
              → 响度归一到 -16 LUFS，成片写进 11_成片/

进度写 02_生成源/progress.json，日志走 stdout。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

COMFY = os.environ.get("LCS_COMFY_URL", "http://127.0.0.1:8188")


# ---------------------------------------------------------------- HTTP 助手
# 注意：不要用 requests —— video_agent 目录下有个 requests/ 数据目录，
# 一旦把它插进 sys.path 就会遮蔽真包，报 module 'requests' has no attribute 'post'
def _http(url: str, body: bytes | None = None, headers: dict | None = None,
          timeout: float = 180) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url: str, payload: dict | None = None, timeout: float = 180) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    raw = _http(url, body, {"Content-Type": "application/json"}, timeout)
    return json.loads(raw or b"{}")


def http_get(url: str, params: dict | None = None, timeout: float = 180) -> bytes:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    return _http(url, None, None, timeout)

# 日志统一走 UTF-8，避免中文在 GBK 控制台被写成乱码
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
VIDEO_AGENT = Path(r"C:\Users\张健\Documents\ChatGPT\短视频制作\video_agent")

MODELS = {
    "unet": "z_image_turbo_bf16.safetensors",
    "clip": "qwen_3_4b.safetensors",
    "vae": "ae.safetensors",
}
SEEDS = [2026111301, 2026111302, 2026111303, 2026111304]

H3_BASE = {
    "models": {
        "video": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "video_vae": "minimax_h3_video_vae_int8_convrot.safetensors",
        "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
        "text_encoder": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "turbo_lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
    },
    "render": {"width": 480, "height": 864, "fps": 24, "steps": 8,
               "seed": 2026111280, "timeout_seconds": 7200},
    "voice": {"backend": "minimax-h3-native", "default_role": "female"},
    "native_audio": {
        "overall_soundscape": (
            "Clear close-microphone female speech dominates at a steady consistent level, "
            "a very faint steady indoor room tone, no crowd, no other voices."),
        "non_diegetic_music": "N/A",
    },
}

# ---------------------------------------------------------------- 提示词恒定层
LOCK_HEAD_BOOKEND = (
    "For the target video, at 0.00 seconds into the target video, <Picture 1> is fully "
    "referenced as the first frame, and at the final frame of the target video the picture "
    "must return to exactly the same framing, subject pose, lighting and object position as "
    "<Picture 2>, which is the same reference image. Keep the camera position and the "
    "product fixed on the desk throughout; only her face, mouth, shoulders and hands may "
    "move. The product keeps exactly the same size, shape, colour and detailing in every "
    "frame. Never change the background, the desk edge or the framing."
)
LOCK_VOICE = (
    "One adult female speaker (S1), warm medium pitch, clear standard Mandarin, even and "
    "matter-of-fact delivery in a single consistent register at a brisk five syllables per "
    "second, tight pacing with very short clause pauses and plain statement intonation,"
)
LOCK_TAIL = (
    "Natural-speed body gestures, no exaggerated pauses, no added words, no second speaker. "
    "No rendered text overlays, no captions, no watermark."
)

DEFAULT_ACTIONS = [
    "She looks straight into the lens and speaks with a neutral confident expression, both "
    "hands resting flat on the desk beside the product without touching it.",
    "She keeps looking into the lens and speaks, tilting her chin down briefly toward the "
    "product and back up to the camera, both hands staying flat on the desk.",
    "She speaks facing the lens and makes one small flat gesture with her right hand at "
    "chest height, then returns the hand to the desk.",
    "She speaks while leaning very slightly toward the lens, then settles back upright, "
    "hands staying flat on the desk.",
    "She speaks calmly and makes one relaxed open-palm gesture above the desk surface, not "
    "touching the product.",
    "She speaks with a small steady nod, keeping both hands still and flat on the desk.",
    "She raises the index finger of her anatomical right hand at viewer left to mark the "
    "measurement, speaking evenly.",
    "She speaks directly to the lens and finishes with one single small nod, mouth closing "
    "at the end.",
]

PROMPT_TEMPLATE = {
    "product_en": "",
    "host_en": "",
    "wardrobe_en": "",
    "room_en": "",
    "product_seed": 2026111300,
    "seeds": SEEDS,
    "shots": [],
}


def log(msg: str) -> None:
    print(msg, flush=True)


def write_progress(proj: Path, **kw) -> None:
    p = proj / "02_生成源" / "progress.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    d = {"updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        d.update(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        pass
    d.update(kw)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _hidden() -> dict:
    kw: dict = {}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kw["startupinfo"] = si
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    except Exception:
        pass
    return kw


# ---------------------------------------------------------------- Z-Image（出图）
def graph(prompt: str, w: int, h: int, seed: int, prefix: str) -> dict:
    cid = uuid.uuid4().hex[:8]
    return {
        "28": {"class_type": "UNETLoader",
               "inputs": {"unet_name": MODELS["unet"], "weight_dtype": "default"}},
        "30": {"class_type": "CLIPLoader",
               "inputs": {"clip_name": MODELS["clip"], "type": "lumina2", "device": "default"}},
        "29": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae"]}},
        "27": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["30", 0], "text": prompt}},
        "33": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["27", 0]}},
        "13": {"class_type": "EmptySD3LatentImage",
               "inputs": {"width": w, "height": h, "batch_size": 1}},
        "11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["28", 0], "shift": 3.0}},
        "3": {"class_type": "KSampler", "inputs": {
            "model": ["11", 0], "positive": ["27", 0], "negative": ["33", 0],
            "latent_image": ["13", 0], "seed": seed, "steps": 8, "cfg": 1.0,
            "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["29", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["8", 0], "filename_prefix": f"{prefix}-{cid}"}},
    }


def post(payload: dict) -> dict:
    req = urllib.request.Request(
        COMFY + "/prompt",
        data=json.dumps({"prompt": payload}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def wait_img(pid: str, timeout: int = 1200) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{COMFY}/history/{pid}", timeout=30) as r:
                hist = json.loads(r.read())
        except Exception as e:
            log(f"  轮询异常（继续等）：{e}")
            time.sleep(3)
            continue
        if pid in hist:
            entry = hist[pid]
            st = entry.get("status", {})
            if st.get("completed") or entry.get("outputs"):
                return entry
            if st.get("status_str") == "error":
                raise RuntimeError(json.dumps(st, ensure_ascii=False)[:800])
        time.sleep(2)
    raise TimeoutError(pid)


def fetch_img(entry: dict, dest: Path) -> int:
    files = []
    for node in entry.get("outputs", {}).values():
        files += node.get("images", [])
    if not files:
        raise RuntimeError("无输出图像")
    f = files[0]
    q = urllib.parse.urlencode({"filename": f["filename"],
                                "subfolder": f.get("subfolder", ""),
                                "type": f.get("type", "output")})
    data = urllib.request.urlopen(f"{COMFY}/view?{q}", timeout=180).read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)


# ---------------------------------------------------------------- 方案与提示词
def load_prompts(proj: Path) -> dict:
    p = proj / "01_方案" / "提示词.json"
    if not p.is_file():
        raise FileNotFoundError(
            "缺少 01_方案/提示词.json —— 在工作台点「编辑提示词」，填商品外观、主播外貌、"
            "服装、房间四段英文描述和逐镜台词")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    missing = [k for k in ("product_en", "host_en", "room_en") if not (cfg.get(k) or "").strip()]
    if missing:
        raise ValueError("提示词.json 还缺字段：" + "、".join(missing) + "（点「编辑提示词」补上）")
    return cfg


def read_task(proj: Path) -> dict:
    p = proj / "task.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def gen_size(w: int, h: int) -> tuple[int, int]:
    """H3 与 Z-Image 都按 16 的倍数出图，交付尺寸再缩放裁切。"""
    gw = max(256, int(math.ceil(w / 16.0)) * 16)
    gh = max(256, int(math.ceil(h / 16.0)) * 16)
    return gw, gh


FPS = 24
H3_MIN_FRAMES = 124          # H3 最短 124 帧；合法帧数一律是 5 + 17k
CPS = 5                      # 铁律①：台词字数 = 分镜秒数 × 5


def snap_frames(seconds: float, slack: float = 0.10) -> int:
    """把「名义秒数」换成 H3 能生成的帧数。

    H3 的帧数只能是 5 + 17k（一档 17 帧 ≈ 0.708 秒），做不到正好 5.000 秒。
    这里取「能装下名义时长 + 收尾余量」的最小合法帧数，让整段素材原样用完：
    切点落在句子说完之后，一帧都不剪，接起来就是无缝的（铁律②）。
    """
    need = (seconds + slack) * FPS
    k = max(0, math.ceil((need - 5) / 17))
    return max(H3_MIN_FRAMES, 5 + 17 * k)


def build_shots(cfg: dict, task: dict) -> list[dict]:
    n = max(1, int(task.get("shots") or 8))
    total = float(task.get("seconds") or 60)
    nominal = round(total / n, 3)          # 分镜名义秒数：台词字数就按它 ×5
    frames = snap_frames(nominal)          # H3 合法帧数（不小于名义时长）
    clip = round(frames / FPS, 3)          # 素材实际时长 = 帧数 ÷ 24
    edit = clip                            # 整段原样进合成，不截断
    gen = clip
    want_chars = round(nominal * CPS)      # 每镜台词目标字数

    raw = cfg.get("shots") or []
    if not raw and cfg.get("script"):
        parts = [p.strip() for p in re.split(r"[。！？\n]", cfg["script"]) if p.strip()]
        raw = [{"voice": p} for p in parts]
    if len(raw) < n:
        raise ValueError(
            f"提示词.json 里只有 {len(raw)} 条台词，任务要 {n} 条 —— 点「编辑提示词」补齐，一条一行")

    product_en = cfg["product_en"].strip()
    host_en = cfg["host_en"].strip()
    wardrobe = (cfg.get("wardrobe_en") or "").strip()
    room_en = cfg["room_en"].strip()

    subject = (
        "Photorealistic live-action product presentation. Preserve the exact woman from the "
        "reference: " + host_en + (" " + wardrobe + "." if wardrobe else ".")
        + " Preserve the exact room: " + room_en
        + " Preserve the exact product: " + product_en + ". "
        "The product stays fully visible on the desk in the same position and never leaves "
        "the desk."
    )

    shots = []
    for i in range(n):
        s = raw[i]
        voice = (s.get("voice") or "").strip()
        if not voice:
            raise ValueError(f"第 {i + 1} 条台词是空的 —— 点「编辑提示词」补上")
        action = (s.get("action_en") or "").strip() or DEFAULT_ACTIONS[i % len(DEFAULT_ACTIONS)]
        # 铁律①：字数必须等于 分镜秒数 × 5；铁律②：素材不剪，切点留在句子说完之后
        spoken = re.sub(r"[，。！？；、,.!?;:：\s]", "", voice)
        if abs(len(spoken) - want_chars) > 2:
            log(f"[S{i + 1:02d}] [warn] 这条 {len(spoken)} 字，按 {nominal:.2f}s × 5 应为 "
                f"{want_chars} 字（差 {len(spoken) - want_chars:+d}），语速会偏离 5 字/秒")
        # say_to = 字数 ÷ 5 - 0.15（补 H3 的 0.1~0.3s 起口延迟），上限 clip-0.25 保证不压到切点
        say_to = round(min(clip - 0.25, max(0.8, len(spoken) / float(CPS) - 0.15)), 2)
        body = (
            subject + " " + action + " " + LOCK_VOICE
            + " speaks visibly with accurate natural lip articulation, begins by 0.10 "
            + f"seconds and completes the exact sentence by {say_to:.2f} seconds: "
            + f"<d>[Chinese] {voice}</d>. " + LOCK_TAIL
        )
        prompt = "\n\n".join([
            LOCK_HEAD_BOOKEND,
            "integrated_multimodal_description: [Shot 1] " + body,
            "overall_soundscape: " + H3_BASE["native_audio"]["overall_soundscape"],
            "non_diegetic_music: N/A",
        ])
        shots.append({
            "id": f"S{i + 1:02d}",
            "duration": gen,
            "edit_duration": edit,
            "mode": "dialogue",
            "scene": "fixed desk",
            "shot_size": (s.get("shot_size") or "medium chest-up").strip(),
            "camera": (s.get("camera") or "static, eye level").strip(),
            "action": action,
            "voice": voice,
            "voice_role": "female",
            "prompt": action,
            "h3_prompt": prompt,
        })
    return shots


# ---------------------------------------------------------------- H3（生成）
def h3_cfg(gw: int, gh: int, seed: int | None = None) -> dict:
    c = json.loads(json.dumps(H3_BASE))
    c["render"]["width"] = gw
    c["render"]["height"] = gh
    if seed:
        c["render"]["seed"] = int(seed)
    return c


def upload_image(path: Path) -> str:
    b = "----wb" + uuid.uuid4().hex
    body = b"".join([
        f'--{b}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode(),
        f'--{b}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n'.encode(),
        (f'--{b}\r\nContent-Disposition: form-data; name="image"; '
         f'filename="{path.name}"\r\nContent-Type: image/png\r\n\r\n').encode(),
        path.read_bytes(),
        f'\r\n--{b}--\r\n'.encode(),
    ])
    raw = _http(f"{COMFY}/upload/image", body,
                {"Content-Type": f"multipart/form-data; boundary={b}"}, 300)
    d = json.loads(raw or b"{}")
    return (d["subfolder"] + "/" + d["name"]).lstrip("/") if d.get("subfolder") else d["name"]


def submit(graph_payload: dict) -> str:
    raw = _http(f"{COMFY}/prompt",
                json.dumps({"prompt": graph_payload}).encode(),
                {"Content-Type": "application/json"}, 180)
    return json.loads(raw)["prompt_id"]


def wait_video(pid: str, proj: Path, pct0: int, pct1: int, timeout: int = 7200) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = json.loads(http_get(f"{COMFY}/history/{pid}", timeout=60) or b"{}")
        if pid in h:
            st = h[pid].get("status", {})
            if st.get("status_str") == "error":
                raise RuntimeError(json.dumps(st, ensure_ascii=False)[:2000])
            if st.get("completed") or h[pid].get("outputs"):
                return h[pid]
        el = time.time() - t0
        pct = min(pct1, pct0 + int((pct1 - pct0) * el / 300))
        write_progress(proj, state="running", pct=pct,
                       msg=f"渲染中 {int(el)}s（一镜约 3-6 分钟）")
        time.sleep(8)
    raise TimeoutError(pid)


def fetch_video(entry: dict, dest: Path) -> int:
    items = []
    for node in entry.get("outputs", {}).values():
        for key in ("images", "videos", "gifs"):
            items += node.get(key, [])
    if not items:
        raise RuntimeError("输出为空：" + json.dumps(entry.get("outputs", {}), ensure_ascii=False)[:600])
    f = items[0]
    raw = http_get(f"{COMFY}/view", {"filename": f["filename"],
                                    "subfolder": f.get("subfolder", ""),
                                    "type": f.get("type", "output")}, 600)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return len(raw)


ENGINE_INST = Path(r"C:\Users\张健\AppData\Local\Comfy-Desktop\ComfyUI-Installs\星原智控")
ENGINE_PY = ENGINE_INST / "ComfyUI" / ".venv" / "Scripts" / "python.exe"
ENGINE_WORK = ENGINE_INST / "ComfyUI"
ENGINE_LOG = ENGINE_INST / "logs" / "engine_manual.log"
ENGINE_YAML = Path(r"C:\Users\张健\AppData\Roaming\Comfy Desktop\instance-model-paths\inst-1793548554448.yaml")
ENGINE_SHARED = Path(r"C:\Users\张健\AppData\Local\Comfy-Desktop\ComfyUI-Shared")


def engine_alive(timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(COMFY + "/system_stats", timeout=timeout) as r:
            r.read(64)
        return True
    except Exception:
        return False


def _clean_env() -> dict:
    import os
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    env["no_proxy"] = env["NO_PROXY"] = "127.0.0.1,localhost"
    env["PYTHONUNBUFFERED"] = "1"
    # 无窗口子进程的 stdout 走系统 ANSI 代码页（简体中文 = cp936/GBK）。
    # ComfyUI 启动时会打 emoji，GBK 编不了就直接抛 UnicodeEncodeError 把引擎弄死，
    # 表现就是"启动了但一直不就绪"。强制 UTF-8，日志文件也在服务端以 utf-8 打开。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def start_engine() -> bool:
    """把本地 ComfyUI 引擎拉起来（脱离父进程），成功返回 True。"""
    import subprocess
    if not ENGINE_PY.is_file() or not ENGINE_YAML.is_file():
        return False
    ENGINE_LOG.parent.mkdir(parents=True, exist_ok=True)
    f = open(ENGINE_LOG, "ab", buffering=0)
    args = [str(ENGINE_PY), "main.py",
            "--port", "8188", "--listen", "127.0.0.1",
            "--extra-model-paths-config", str(ENGINE_YAML),
            "--output-directory", str(ENGINE_SHARED / "output"),
            "--input-directory", str(ENGINE_SHARED / "input"),
            "--disable-auto-launch"]
    log("引擎没在跑，正在自动启动（首次约 20~60 秒）…")
    proc = subprocess.Popen(args, cwd=str(ENGINE_WORK), stdout=f, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=_clean_env(), close_fds=True,
                            creationflags=0x00000008 | 0x00000200 | 0x08000000)
    for i in range(60):
        time.sleep(2)
        if engine_alive(3):
            log(f"引擎就绪（{2 * (i + 1)} 秒）")
            return True
        # 引擎进程已经退出还等下去没有意义，直接把日志尾巴打给用户看
        if proc.poll() is not None:
            log(f"引擎进程已退出（返回码 {proc.returncode}），最近日志：")
            for line in engine_log_tail(18):
                log("  " + line)
            return False
    log("引擎 120 秒内没有就绪，最近日志：")
    for line in engine_log_tail(18):
        log("  " + line)
    return False


def engine_log_tail(n: int = 20) -> list[str]:
    try:
        raw = ENGINE_LOG.read_bytes()[-8000:]
        text = raw.decode("utf-8", errors="replace")
        return [ln.rstrip() for ln in text.splitlines() if ln.strip()][-n:]
    except Exception:
        return ["(引擎日志读不到)"]


def ensure_engine() -> None:
    if engine_alive():
        return
    if start_engine():
        return
    raise RuntimeError(
        f"ComfyUI 引擎没起来，自动启动也没成功（{COMFY}）。"
        f"日志见 {ENGINE_LOG}，或手动双击桌面「启动引擎」。")


def free_vram() -> None:
    """生成完把模型从显存卸掉，免得一直占着显卡。"""
    try:
        req = urllib.request.Request(
            COMFY + "/free", data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        log("已释放显存")
    except Exception:
        pass


# ---------------------------------------------------------------- 阶段 1：出图
def prompt_product(cfg: dict) -> str:
    return (
        "A single studio product photograph of " + cfg["product_en"] + ". "
        "The product stands fully visible, centred, slightly turned forty-five degrees so "
        "its volume reads clearly. Soft large softbox light from upper left plus a subtle "
        "fill at right, clean matte reflections, plain warm light-grey seamless background, "
        "85mm product photograph, sharp focus, high detail. "
        "One object only, no hands, no person, no props, no text, no watermark."
    )


def prompt_host(cfg: dict) -> str:
    return (
        "Photorealistic vertical video still of a livestream sales presenter. "
        + cfg["host_en"] + " " + cfg.get("wardrobe_en", "") + ". "
        "In " + cfg["room_en"] + ". "
        "Sitting upright and squarely facing the camera, both forearms on the desk, hands "
        "resting flat on the desk surface about twenty centimetres behind and to either "
        "side of the product, fingers relaxed and clearly away from it, not touching it "
        "and not framing it. "
        "Standing on the desk directly in front of her is " + cfg["product_en"] + ". "
        "It is placed at about forty-five degrees to the lens, turned so that its side "
        "profile, its upright structure and its base all read clearly, with its front "
        "detail pointing toward the lower right of the frame. "
        "This product is the single hero object of the shot: it stands alone, nothing is "
        "resting on it, mounted in it, clipped to it or leaning against it, and no other "
        "device or accessory sits beside it. It is sharply in focus, evenly lit, about a "
        "third of the frame wide, placed in the lower third of the frame and fully "
        "visible from its base to its top, unobstructed and not overlapped by her arms, "
        "hands or hair. "
        "She looks straight into the lens with a calm confident closed-mouth smile, no "
        "visible teeth. Medium shot from the waist up, the front edge of the desk crossing "
        "the bottom of the frame, camera at her eye level tilted gently down onto the "
        "desktop so the whole product, its base and a broad strip of bare desk stay fully "
        "in frame and read at a glance, soft window daylight from frame right, shallow "
        "depth of field, realistic skin texture, 50mm lens. "
        "The desk carries that product and nothing else besides her forearms: no cups, "
        "no papers, no unrelated props. No text, no logo, no watermark."
    )


def _archive_old_assets(proj: Path) -> str:
    """重出图前把旧图移到 _历史_时间戳/ 下，不删除；定稿图改名备份，避免继续被当成定稿。"""
    stamp = time.strftime("%m%d_%H%M%S")
    prod_dir = proj / "03_商品素材"
    old = prod_dir / "product.png"
    if old.is_file():
        bak = prod_dir / "_历史" / stamp
        bak.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(bak / "product.png"))

    cand_dir = proj / "04_分镜参考" / "cand"
    if cand_dir.is_dir():
        bak = cand_dir / f"_历史_{stamp}"
        moved = False
        for f in sorted(cand_dir.glob("*.png")):
            bak.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(bak / f.name))
            moved = True
        if not moved and bak.is_dir():
            shutil.rmtree(bak, ignore_errors=True)

    ref = proj / "04_分镜参考" / "host_scene_ref.png"
    if ref.is_file():
        ref.rename(proj / "04_分镜参考" / f"host_scene_ref_旧_{stamp}.png")
    return stamp


def stage_assets(proj: Path, regen: bool = False) -> int:
    cfg = load_prompts(proj)
    task = read_task(proj)
    w, h = int(task.get("width", 480)), int(task.get("height", 854))
    gw, gh = gen_size(w, h)

    if regen:
        stamp = _archive_old_assets(proj)
        # seed 前移，否则重出会得到几乎一样的图
        cfg["product_seed"] = int(cfg.get("product_seed") or 2026111300) + 1
        cfg["seeds"] = [int(s) + 1 for s in (cfg.get("seeds") or SEEDS)]
        p = proj / "01_方案" / "提示词.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"重新出图：旧图已备份到 _历史_{stamp}/，seed 前移一位")

    ensure_engine()
    dest = proj / "03_商品素材" / "product.png"
    write_progress(proj, stage="assets", state="running", pct=2, msg="生成商品主视图")
    if dest.is_file() and dest.stat().st_size > 10000:
        log(f"商品主视图已存在，跳过：{dest.name}")
    else:
        log("提交商品主视图…")
        pid = post(graph(prompt_product(cfg), 1024, 1024,
                         int(cfg.get("product_seed", 2026111300)), "product"))["prompt_id"]
        entry = wait_img(pid)
        size = fetch_img(entry, dest)
        log(f"商品主视图完成 {size/1024:.0f} KB")

    seeds = cfg.get("seeds") or SEEDS
    cand_dir = proj / "04_分镜参考" / "cand"
    cand_dir.mkdir(parents=True, exist_ok=True)
    total = len(seeds)
    for i, sd in enumerate(seeds, 1):
        out = cand_dir / f"host_{sd}.png"
        pct = 10 + int(80 * (i - 1) / max(1, total))
        write_progress(proj, stage="assets", state="running", pct=pct,
                       msg=f"定妆照 {i}/{total}（seed {sd}）")
        if out.is_file() and out.stat().st_size > 10000:
            log(f"候选已存在，跳过 {out.name}")
            continue
        log(f"提交定妆照 seed {sd} @ {gw}×{gh}")
        pid = post(graph(prompt_host(cfg), gw, gh, int(sd), "host"))["prompt_id"]
        entry = wait_img(pid)
        size = fetch_img(entry, out)
        log(f"  -> {out.name} {size/1024:.0f} KB")

    write_progress(proj, stage="assets", state="done", pct=100,
                   msg=f"出图完成：商品图 1 张 + 定妆候选 {total} 张，点一张「选为定稿」")
    log("assets 阶段完成")
    return 0


# ---------------------------------------------------------------- 阶段 2：生成
def stage_shots(proj: Path) -> int:
    cfg = load_prompts(proj)
    task = read_task(proj)
    ref = proj / "04_分镜参考" / "host_scene_ref.png"
    if not ref.is_file():
        raise FileNotFoundError("定妆照还没定稿：先跑①，再在候选里点一张「选为定稿」")

    shots = build_shots(cfg, task)
    w, h = int(task.get("width", 480)), int(task.get("height", 854))
    gw, gh = gen_size(w, h)

    ensure_engine()
    sys.path.append(str(VIDEO_AGENT))
    import agent  # noqa: E402

    raw_dir = proj / "02_生成源" / "raw"
    prompt_dir = proj / "02_生成源" / "prompts"
    raw_dir.mkdir(parents=True, exist_ok=True)
    prompt_dir.mkdir(parents=True, exist_ok=True)

    write_progress(proj, stage="shots", state="running", pct=1, msg="上传定妆照")
    name = upload_image(ref)
    log(f"定妆照已上传 -> {name}")

    c = h3_cfg(gw, gh)
    pre = agent.graph(c, shots[0], "livestream/preflight", image_name=name,
                      last_image_name=name)
    try:
        info = json.loads(http_get(f"{COMFY}/object_info", timeout=180) or b"{}")
        issues = agent.check_graph(pre, info)
    except Exception as e:
        log(f"节点预检跳过（{e}）")
        issues = []
    if issues:
        raise RuntimeError("H3 节点预检失败：" + "; ".join(issues))
    log("节点预检通过")

    n = len(shots)
    for i, shot in enumerate(shots, 1):
        sid = shot["id"]
        out = raw_dir / f"{sid}.mp4"
        if out.is_file() and out.stat().st_size > 10000:
            log(f"[{sid}] 已存在，跳过")
            write_progress(proj, pct=5 + int(90 * i / n), msg=f"{sid} 已有素材，跳过")
            continue
        g = agent.graph(c, shot, f"livestream-{task.get('code', '')}/{sid}",
                        image_name=name, last_image_name=name)
        (prompt_dir / f"{sid}.txt").write_text(g["131"]["inputs"]["prompt"], encoding="utf-8")
        frames = g["131"]["inputs"]["length"]
        log(f"[{sid}] {i}/{n}  {frames} 帧 = {frames/24:.2f}s  {gw}×{gh}  首帧=尾帧=定妆照")
        pct0 = 5 + int(90 * (i - 1) / n)
        pct1 = 5 + int(90 * i / n) - 2
        write_progress(proj, stage="shots", state="running", pct=pct0,
                       msg=f"{sid} 已提交，等渲染")
        t0 = time.time()
        pid = submit(g)
        entry = wait_video(pid, proj, pct0, pct1)
        size = fetch_video(entry, out)
        log(f"[{sid}] 完成 {time.time()-t0:.0f}s  {size/1024/1024:.2f} MB")

    write_progress(proj, stage="shots", state="done", pct=100,
                   msg=f"{n} 镜全部生成完成，可以点③合成")
    log("shots 阶段完成")
    return 0


# ---------------------------------------------------------------- 阶段 3：合成
def ffprobe(path: Path) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=width,height,r_frame_rate,codec_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=60, **_hidden()).stdout
        return json.loads(out or "{}")
    except Exception:
        return {}


def gen_voice(proj: Path, sid: str, fallback: str = "") -> str:
    """读这一镜生成当时写下的台词（02_生成源/prompts/S0X.txt）。"""
    p = proj / "02_生成源" / "prompts" / f"{sid}.txt"
    if p.is_file():
        try:
            m = re.search(r"<d>\[Chinese\]\s*(.+?)</d>",
                          p.read_text(encoding="utf-8", errors="ignore"), re.S)
        except Exception:
            m = None
        if m:
            return " ".join(m.group(1).split())
    return fallback


def speech_span(mp4: Path, dur: float) -> tuple[float, float]:
    """用 silencedetect 估一条台词在镜头里的起止，用于对齐字幕。"""
    txt = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(mp4), "-af",
         "silencedetect=noise=-32dB:d=0.20", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
        timeout=180, **_hidden()).stderr
    sil, cur = [], None
    for line in (txt or "").splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            cur = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and cur is not None:
            sil.append((cur, float(m.group(1))))
            cur = None
    if cur is not None:
        sil.append((cur, dur))
    start, end = 0.0, dur
    for a, b in sil:
        if a <= start + 0.03:
            start = b
        else:
            break
    for a, b in reversed(sil):
        if b >= end - 0.03:
            end = a
        else:
            break
    if end - start < 0.4:
        start, end = 0.10, max(0.6, dur)
    return start, min(end, dur)


def split_script(text: str, max_len: int = 26, min_len: int = 12) -> list[str]:
    """按标点切句，再把过短的碎片合并，避免出现「第一」「第三」这种单条字幕。"""
    parts = [p.strip() for p in re.findall(r"[^，。！？；、]+[，。！？；、]?", text) if p.strip()]
    chunks: list[str] = []
    for p in parts:
        while len(p.rstrip("，。！？；、")) > max_len:
            chunks.append(p[:max_len])
            p = p[max_len:]
        if p.strip():
            chunks.append(p)

    out: list[str] = []
    buf = ""
    for c in chunks:
        if not buf:
            buf = c
        elif len(buf.rstrip("，。！？；、")) < min_len and \
                len(buf) + len(c) <= max_len + 2:
            buf += c
        else:
            out.append(buf)
            buf = c
    if buf:
        out.append(buf)

    # 末条太短就并回上一条
    if len(out) >= 2 and len(out[-1].rstrip("，。！？；、")) < min_len:
        merged = out[-2] + out[-1]
        if len(merged) <= max_len + 4:
            out[-2] = merged
            out.pop()

    return [s.rstrip("，。！？；、") for s in out if s.rstrip("，。！？；、")]


def measure_loudness(path: Path) -> float | None:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af",
         "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
        timeout=600, **_hidden()).stderr
    m = re.search(r'"input_i"\s*:\s*"?(-?[\d.]+)"?', out or "")
    return float(m.group(1)) if m else None


def ass_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def wrap_sub(text: str, per_line: int = 13) -> str:
    """字幕折行：一行最多 per_line 个字，优先在标点处断开，否则对半硬断。"""
    if len(text) <= per_line:
        return text
    mid = (len(text) + 1) // 2
    cut = mid
    for d in range(0, 5):
        for i in (mid + d, mid - d):
            if 2 <= i <= len(text) - 2 and text[i - 1] in "，。！？；、":
                cut = i
                break
        if cut != mid:
            break
    return text[:cut] + "\\N" + text[cut:]


ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CN,Microsoft YaHei,{fs},&H00FFFFFF,&H00FFFFFF,&H00302820,&HB4000000,-1,0,0,0,100,100,1,0,1,3,1,2,40,40,{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def stage_final(proj: Path) -> int:
    cfg = load_prompts(proj)
    task = read_task(proj)
    shots = build_shots(cfg, task)
    W, H = int(task.get("width", 480)), int(task.get("height", 854))
    raw_dir = proj / "02_生成源" / "raw"
    work = proj / "02_生成源" / "work"
    out_dir = proj / "11_成片"
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    missing = [s["id"] for s in shots if not (raw_dir / f"{s['id']}.mp4").is_file()]
    if missing:
        raise FileNotFoundError("还缺这些镜头：" + "、".join(missing) + "，先跑②")

    write_progress(proj, stage="final", state="running", pct=3, msg="对齐字幕时间轴")
    lines, clips, t0 = [], [], 0.0
    n = len(shots)
    for i, s in enumerate(shots, 1):
        src = raw_dir / f"{s['id']}.mp4"
        # 铁律②：不截断。以素材真实时长为准（H3 按 17 帧一档出，会是 5.167 这类值），
        # 整段原样进合成，句尾留白一起保留，切点永远落在说话结束之后。
        dur = float(s["edit_duration"])
        real = float(ffprobe(src).get("format", {}).get("duration") or 0)
        if real > 0:
            dur = round(real, 3)
        clips.append((src, dur))
        st, en = speech_span(src, dur)
        # 素材可能由旧台词生成：一律以生成当时的 prompt 为准，免得字幕和口播对不上
        voice = s["voice"]
        old = gen_voice(proj, s["id"], "")
        if old and old.strip().rstrip("。") != voice.strip().rstrip("。"):
            log(f"[{s['id']}] [warn] 这一镜是旧台词生成的，提示词后来改过 —— "
                f"字幕按旧台词写，想对上就删掉重跑②")
            voice = old
        cues = split_script(voice)
        total_chars = sum(len(c) for c in cues) or 1
        span = max(1.0, en - st)
        acc = st
        for c in cues:
            d = span * len(c) / total_chars
            lines.append((t0 + acc, min(t0 + acc + d, t0 + dur), c))
            acc += d
        cn = len(re.sub(r"[，。！？；、,.!?;:：\s]", "", voice))
        log(f"[{s['id']}] 台词 {st:.2f}-{en:.2f}s = {cn / max(0.2, en - st):.1f} 字/秒 "
            f"→ {len(cues)} 条字幕")
        t0 += dur
        write_progress(proj, pct=3 + int(12 * i / n), msg=f"{s['id']} 时间轴对齐完成")

    # 拼接 + 缩放裁切（不 trim：每镜整段用完，帧序连续，接缝无跳帧无断句）
    write_progress(proj, pct=18, msg="拼接镜头（整段直拼，不剪帧）")
    inputs, filters = [], []
    for i, (src, dur) in enumerate(clips):
        inputs += ["-i", str(src)]
        filters.append(
            f"[{i}:v]setpts=PTS-STARTPTS,fps=24,"
            f"scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1[c{i}]")
        # 音频不剪有声内容：只在尾部补静音凑齐长度，保证 A/V 严格等长才拼得无缝
        filters.append(f"[{i}:a]asetpts=PTS-STARTPTS,apad,atrim=0:{dur:.3f}[a{i}]")
    chain = "".join(f"[c{i}][a{i}]" for i in range(len(clips)))
    filters.append(f"{chain}concat=n={len(clips)}:v=1:a=1[v][a]")
    silent = work / "concat.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + inputs + [
        "-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "slow", "-crf", "16",
        "-x264-params", "aq-mode=3", "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(silent)], check=True, **_hidden())
    log(f"拼接 -> {silent}")

    write_progress(proj, pct=45, msg="响度归一")
    cur = measure_loudness(silent)
    gain = (-16.0 - cur) if cur is not None else 0.0
    log(f"响度 {cur if cur is None else round(cur,1)} LUFS -> 增益 {gain:+.1f} dB")
    af = ["-af", f"volume={gain:.2f}dB"]

    base = f"{task.get('product', '成片')}_{task.get('resolution', '480P')}_{int(float(task.get('seconds', 60)))}秒"
    plain = out_dir / f"{base}_原生声音_v1.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent)] + af +
                   ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                    "-movflags", "+faststart", str(plain)], check=True, **_hidden())

    write_progress(proj, pct=70, msg="烧中文字幕")
    ass = work / "subs.ass"
    head = ASS_HEAD.format(w=W, h=H, fs=max(18, round(H * 0.029)), mv=max(24, round(H * 0.04)))
    body = head + "".join(
        f"Dialogue: 0,{ass_time(a)},{ass_time(b)},CN,,0,0,0,,{wrap_sub(c)}\n" for a, b, c in lines)
    ass.write_text(body, encoding="utf-8")
    subbed = out_dir / f"{base}_中文字幕_v1.mp4"
    esc = str(ass).replace("\\", "/").replace(":", "\\:")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent),
                    "-vf", f"ass='{esc}'"] + af +
                   ["-c:v", "libx264", "-preset", "slow", "-crf", "16",
                    "-pix_fmt", "yuv420p", "-profile:v", "high",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                    "-movflags", "+faststart", str(subbed)], check=True, **_hidden())

    (work / "cues.json").write_text(json.dumps(
        [{"start": round(a, 2), "end": round(b, 2), "text": c} for a, b, c in lines],
        ensure_ascii=False, indent=1), encoding="utf-8")

    meta = ffprobe(subbed)
    streams = meta.get("streams") or []
    v = next((x for x in streams if x.get("codec_type") == "video"), {})
    report = {
        "file": subbed.name,
        "size_mb": round(subbed.stat().st_size / 1048576, 2),
        "duration": round(float(meta.get("format", {}).get("duration", 0) or 0), 3),
        "width": v.get("width"), "height": v.get("height"), "fps": v.get("r_frame_rate"),
        "shots": len(shots), "each_edit": shots[0]["edit_duration"],
        "lufs_in": cur, "gain_db": round(gain, 2), "loudness_target": -16.0,
        "subtitles": len(lines),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    ad = proj / "05_审核记录"
    ad.mkdir(parents=True, exist_ok=True)
    (ad / "成片参数.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    write_progress(proj, stage="final", state="done", pct=100,
                   msg=f"成片完成 {report['duration']}s / {report['size_mb']} MB")
    log(f"成片 -> {subbed}")
    return 0


# ---------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proj", required=True)
    ap.add_argument("--stage", required=True, choices=["assets", "shots", "final"])
    ap.add_argument("--regen", action="store_true",
                    help="只对 assets 有效：备份旧图、seed 前移，重新出商品图与定妆照")
    a = ap.parse_args()
    proj = Path(a.proj).resolve()

    fn = {"assets": stage_assets, "shots": stage_shots, "final": stage_final}[a.stage]
    try:
        rc = fn(proj, a.regen) if a.stage == "assets" else fn(proj)
        free_vram()
        return rc
    except Exception as e:
        write_progress(proj, stage=a.stage, state="error", msg=str(e)[:400])
        log(f"[error] {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
