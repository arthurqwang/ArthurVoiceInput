# -*- coding: utf-8 -*-
"""讯飞开放平台 语音听写 (iat) WebSocket 引擎。

与 voice_input.py 的 Transcriber 接口对齐：提供 transcribe(wav_path) -> str。
凭证通过环境变量传入（需先在 https://www.xfyun.cn 注册并创建应用，免费额度即可）：
    XFYUN_APPID    应用 APPID
    XFYUN_APIKEY   APIKey
    XFYUN_APISECRET APISecret

说明：
- 讯飞 iat 要求音频为 16kHz / 16bit / 单声道 PCM（本项目录音正是此规格）。
- 使用普通模式（不启用 dwa=wpgs）：wpgs 的分段回退修正对短句反而引入拼接
  乱码（实测 '大大景大庆油田…'），普通模式直接返回完整结果最稳。
- 自动读取 HTTPS_PROXY/HTTP_PROXY 走代理（适配内网环境）。
- 本协议使用「同步」websocket.WebSocket()，消息通过 recv() 直接收取（不要挂
  on_message 之类仅在 WebSocketApp 下才生效的回调）。
- 关键点 1：iat 每个数据帧必须携带 common.app_id 与 business 参数，否则服务器返回
  code=10313 "app_id cannot be empty" 并立即关闭连接。
- 关键点 2：经实测，企业代理对 WSS 空闲极敏感——若边发边等服务器回包（帧间出现
  空闲），隧道常被秒关。故采用「手动 CONNECT 建隧道 + 先连发完所有音频帧、再统一
  收取结果」的策略，并把整段交互包在 3 次退避重试里，最大化稳定性。
"""
import os
import sys
import base64
import hmac
import hashlib
import json
import time
import configparser

import websocket  # websocket-client

# 凭证配置文件（程序根目录：exe 所在目录或脚本同目录）；优先环境变量，其次读本文件 [xfyun] 段
_CONFIG_PATH = os.path.join(
    os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__)),
    "xfyun_config.ini",
)


def load_xfyun_config():
    """返回 (app_id, api_key, api_secret)。
    优先级：环境变量 XFYUN_APPID/XFYUN_APIKEY/XFYUN_APISECRET > xfyun_config.ini。"""
    vals = {
        "app_id": os.environ.get("XFYUN_APPID"),
        "api_key": os.environ.get("XFYUN_APIKEY"),
        "api_secret": os.environ.get("XFYUN_APISECRET"),
    }
    try:
        cp = configparser.ConfigParser()
        cp.read(_CONFIG_PATH, encoding="utf-8")
        sec = cp["xfyun"]
        for key in ("app_id", "api_key", "api_secret"):
            if not vals[key] and sec.get(key):
                vals[key] = sec[key].strip()
    except Exception:
        pass
    return vals["app_id"], vals["api_key"], vals["api_secret"]


class XfyunTranscriber:
    def __init__(self, app_id=None, api_key=None, api_secret=None):
        _aid, _akey, _asec = load_xfyun_config()
        self.app_id = app_id or _aid
        self.api_key = api_key or _akey
        self.api_secret = api_secret or _asec
        self.host = "iat-api.xfyun.cn"
        self.path = "/v2/iat"
        self.ws_url = "wss://iat-api.xfyun.cn/v2/iat"

    # ---------- 预热（仅做凭证校验，不联网） ----------
    def load(self):
        if not (self.app_id and self.api_key and self.api_secret):
            return

    # ---------- 鉴权头 ----------
    def _auth_headers(self):
        import datetime
        now = datetime.datetime.utcnow()
        date = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
        signature_origin = "host: " + self.host + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + self.path + " HTTP/1.1"
        signature_sha = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature_base64 = base64.b64encode(signature_sha).decode("utf-8")
        authorization = (
            'api_key="%s", algorithm="hmac-sha256", '
            'headers="host date request-line", signature="%s"'
            % (self.api_key, signature_base64)
        )
        return {"Authorization": authorization, "Date": date, "Host": self.host}

    @staticmethod
    def _proxy():
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            v = os.environ.get(k)
            if v:
                if "://" in v:
                    v = v.split("://", 1)[1]
                if "@" in v:
                    v = v.split("@", 1)[1]
                host, _, port = v.partition(":")
                if host and port:
                    return host, int(port)
        return None, None

    @staticmethod
    def _tunnel_socket(ph, pp, host="iat-api.xfyun.cn", timeout=15):
        """手动经 HTTP 代理建 CONNECT 隧道并手包 TLS，返回已握手的 TLS socket。

        websocket-client 自带的 http_proxy_host 在本环境对 WSS 不稳（握手后即被关），
        故改用底层 socket 自建隧道，再把 socket 交给 websocket 使用。
        """
        import socket
        import ssl
        s = socket.create_connection((ph, pp), timeout=timeout)
        req = "CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n" % (host, host)
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            c = s.recv(4096)
            if not c:
                break
            resp += c
        if b" 200 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError("代理 CONNECT 失败: %s" % resp[:200])
        ctx = ssl.create_default_context()
        return ctx.wrap_socket(s, server_hostname=host)

    @staticmethod
    def _apply_result(result, ws_cache):
        """把一帧的 ws 结果按 wpgs 的 rg 范围回退替换到 ws_cache。

        wpgs 的 rg=[bg, ed] 是相对于「累计输出流」的索引：后续帧可能修正
        前面已输出的段（ed 可能超出当前缓存长度）。正确处理：先按 ed 补齐
        空位，再按位置替换，否则会产出重复/渐进乱码（如 '大庆大庆油田'）。
        """
        for ws_seg in result["ws"]:
            rg = ws_seg.get("rg")  # [bg, ed] 基于 ws 索引
            word = "".join(cw["w"] for cw in ws_seg["cw"])
            if rg:
                bg, ed = rg[0], rg[1]
                if ed > len(ws_cache):
                    ws_cache.extend([""] * (ed - len(ws_cache)))
                ws_cache[bg:ed] = [word]
            else:
                ws_cache.append(word)
        return "".join(ws_cache)

    # ---------- 入口 ----------
    def transcribe(self, wav_path, max_attempts=3):
        if not (self.app_id and self.api_key and self.api_secret):
            raise RuntimeError(
                "缺少讯飞凭证：请设置环境变量 XFYUN_APPID / XFYUN_APIKEY / XFYUN_APISECRET\n"
                "（在 https://www.xfyun.cn 注册 → 创建应用 → 拿到三项密钥）\n"
                "获得三项密钥后，右键点击输入法图标，在配置窗口填入。"
            )

        import soundfile as sf

        data, sr = sf.read(wav_path, dtype="int16", always_2d=False)
        if data.ndim == 2:
            data = data[:, 0]
        if sr != 16000:
            raise RuntimeError("讯飞要求 16kHz 采样率，当前 %.0fHz" % sr)
        pcm = data.tobytes()

        headers = self._auth_headers()
        ph, pp = self._proxy()

        # 讯飞 iat 协议：每个数据帧必须携带 common.app_id 与 business，缺一不可。
        common = {"app_id": self.app_id}
        business = {
            "language": "zh_cn",
            "domain": "iat",
            "accent": "mandarin",
            "vad_eos": 3000,
            # 注意：不要启用 dwa=wpgs。wpgs 会分段回退修正，若结果拼接逻辑
            # 不严谨会产生「渐进式重复乱码」（如 '大大景大庆油田…'）；
            # 普通模式（无 dwa）对几秒级短句直接返回完整结果，实测最稳。
        }

        # 预生成所有音频帧（每帧 8000 字节 ≈ 0.25s）。
        CHUNK = 8000
        total = len(pcm)
        frames = []
        idx = 0
        while idx < total:
            chunk = pcm[idx:idx + CHUNK]
            if idx == 0:
                status = 0
            elif idx + CHUNK >= total:
                status = 2
            else:
                status = 1
            frames.append(json.dumps({
                "common": common,
                "business": business,
                "data": {
                    "status": status,
                    "format": "audio/L16;rate=16000",
                    "encoding": "raw",
                    "audio": base64.b64encode(chunk).decode("utf-8"),
                }
            }))
            idx += CHUNK

        last_err = None
        for attempt in range(1, max_attempts + 1):
            ws = None
            try:
                ws = websocket.WebSocket()
                ws.settimeout(15)
                if ph:
                    ss = self._tunnel_socket(ph, pp)
                    ws.connect(self.ws_url,
                               header=["%s: %s" % (k, v) for k, v in headers.items()],
                               socket=ss)
                else:
                    ws.connect(self.ws_url,
                               header=["%s: %s" % (k, v) for k, v in headers.items()])

                # 先连发所有帧（帧间仅极短停顿，避免被代理空闲掐断）。
                for f in frames:
                    ws.send(f)
                    time.sleep(0.01)

                # 发完后统一收取结果，直到 status==2 或超时。
                ws_cache = []
                final_text = ""
                finished = False
                deadline = time.time() + 15
                while time.time() < deadline:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except Exception as e:
                        last_err = "接收失败: %s" % e
                        break
                    if not raw:
                        break
                    data = json.loads(raw)
                    if data.get("code") != 0:
                        raise RuntimeError("讯飞错误 %s: %s" % (
                            data.get("code"), data.get("message", "unknown")))
                    final_text = self._apply_result(data["data"]["result"], ws_cache)
                    if data["data"]["status"] == 2:
                        finished = True
                        break
                if finished:
                    return final_text.strip()
                last_err = "连接在未收到最终结果前被关闭"
            except Exception as e:
                last_err = e
                time.sleep(1.5 * attempt)  # 退避
            finally:
                try:
                    ws.close()
                except Exception:
                    pass

        msg = last_err if isinstance(last_err, str) else repr(last_err)
        raise RuntimeError("讯飞识别失败（已重试 %d 次）: %s" % (max_attempts, msg))
