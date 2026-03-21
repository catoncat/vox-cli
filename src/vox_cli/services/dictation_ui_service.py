from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import webbrowser

from pydantic import BaseModel, Field

from ..config import VoxConfig, _load_toml, get_config_path
from .dictation_context_service import capture_dictation_context
from .dictation_service import dictation_session_log_path, tail_session_log


class DictationUiHotwordEntryPayload(BaseModel):
    value: str = ''
    aliases: list[str] = Field(default_factory=list)


class DictationUiContextPayload(BaseModel):
    enabled: bool = False
    max_chars: int = 1200


class DictationUiHotwordsPayload(BaseModel):
    enabled: bool = False
    rewrite_aliases: bool = True
    case_sensitive: bool = False
    entries: list[DictationUiHotwordEntryPayload] = Field(default_factory=list)


class DictationUiHintsPayload(BaseModel):
    enabled: bool = False
    items: list[str] = Field(default_factory=list)


class DictationUiStatePayload(BaseModel):
    context: DictationUiContextPayload = DictationUiContextPayload()
    hotwords: DictationUiHotwordsPayload = DictationUiHotwordsPayload()
    hints: DictationUiHintsPayload = DictationUiHintsPayload()


_MANAGED_HEADERS = {
    '[dictation.context]',
    '[dictation.hotwords]',
    '[[dictation.hotwords.entries]]',
    '[dictation.hints]',
}


def launch_dictation_ui(
    config: VoxConfig,
    *,
    host: str = '127.0.0.1',
    port: int | None = None,
    open_browser: bool = True,
) -> str:
    effective_port = port or _pick_free_port(host)
    server = ThreadingHTTPServer((host, effective_port), _build_handler(config))
    url = f'http://{host}:{effective_port}'
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url


def build_dictation_ui_state(config: VoxConfig) -> dict[str, Any]:
    live = _read_config_for_ui(config)
    state = DictationUiStatePayload(
        context=DictationUiContextPayload(
            enabled=live.dictation.context.enabled,
            max_chars=live.dictation.context.max_chars,
        ),
        hotwords=DictationUiHotwordsPayload(
            enabled=live.dictation.hotwords.enabled,
            rewrite_aliases=live.dictation.hotwords.rewrite_aliases,
            case_sensitive=live.dictation.hotwords.case_sensitive,
            entries=[
                DictationUiHotwordEntryPayload(
                    value=entry.value,
                    aliases=list(entry.aliases),
                )
                for entry in live.dictation.hotwords.entries
            ],
        ),
        hints=DictationUiHintsPayload(
            enabled=live.dictation.hints.enabled,
            items=list(live.dictation.hints.items),
        ),
    )
    return {
        'config_path': str(get_config_path(config)),
        'log_path': str(dictation_session_log_path(config)),
        'state': state.model_dump(),
        'logs': tail_session_log(config, lines=120),
    }


def save_dictation_ui_state(config: VoxConfig, payload: dict[str, Any]) -> dict[str, Any]:
    state = DictationUiStatePayload.model_validate(payload)
    config_path = get_config_path(config)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing_text = config_path.read_text(encoding='utf-8') if config_path.exists() else ''
    preserved_text = strip_managed_dictation_ui_sections(existing_text)
    managed_text = render_dictation_ui_sections(state)

    next_text = preserved_text.rstrip()
    if next_text:
        next_text = f'{next_text}\n\n{managed_text}'
    else:
        next_text = managed_text
    config_path.write_text(f'{next_text.rstrip()}\n', encoding='utf-8')
    return build_dictation_ui_state(config)


def strip_managed_dictation_ui_sections(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    skipping = False

    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith('[') and stripped.endswith(']')
        if is_header:
            if stripped in _MANAGED_HEADERS:
                skipping = True
                while kept and not kept[-1].strip():
                    kept.pop()
                continue
            if skipping:
                skipping = False
        if skipping:
            continue
        kept.append(line)

    while kept and not kept[-1].strip():
        kept.pop()
    return '\n'.join(kept)


def render_dictation_ui_sections(state: DictationUiStatePayload | dict[str, Any]) -> str:
    state = DictationUiStatePayload.model_validate(state)
    lines: list[str] = []

    lines.extend(
        [
            '[dictation.context]',
            f'enabled = {_toml_bool(state.context.enabled)}',
            f'max_chars = {max(0, int(state.context.max_chars))}',
            '',
            '[dictation.hotwords]',
            f'enabled = {_toml_bool(state.hotwords.enabled)}',
            f'rewrite_aliases = {_toml_bool(state.hotwords.rewrite_aliases)}',
            f'case_sensitive = {_toml_bool(state.hotwords.case_sensitive)}',
        ]
    )

    entries = [
        DictationUiHotwordEntryPayload(
            value=entry.value.strip(),
            aliases=[alias.strip() for alias in entry.aliases if alias.strip()],
        )
        for entry in state.hotwords.entries
        if entry.value.strip()
    ]
    for entry in entries:
        lines.extend(
            [
                '',
                '[[dictation.hotwords.entries]]',
                f'value = {_toml_string(entry.value)}',
                f'aliases = {_toml_array(entry.aliases)}',
            ]
        )

    items = [item.strip() for item in state.hints.items if item.strip()]
    lines.extend(
        [
            '',
            '[dictation.hints]',
            f'enabled = {_toml_bool(state.hints.enabled)}',
        ]
    )
    if items:
        lines.append('items = [')
        for item in items:
            lines.append(f'  {_toml_string(item)},')
        lines.append(']')
    else:
        lines.append('items = []')

    return '\n'.join(lines).rstrip()


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _toml_bool(value: bool) -> str:
    return 'true' if value else 'false'


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _build_handler(config: VoxConfig) -> type[BaseHTTPRequestHandler]:
    class DictationUiHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == '/':
                self._send_html(_DICTATION_UI_HTML)
                return
            if parsed.path == '/api/state':
                self._send_json(build_dictation_ui_state(config))
                return
            if parsed.path == '/api/logs':
                query = parse_qs(parsed.query)
                lines = int(query.get('lines', ['120'])[0])
                self._send_json({'logs': tail_session_log(config, lines=max(20, min(lines, 500)))})
                return
            if parsed.path == '/api/context':
                query = parse_qs(parsed.query)
                delay_ms = _parse_context_delay_ms(query.get('delay_ms', ['0'])[0])
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)
                live = _read_config_for_ui(config)
                context = capture_dictation_context(live, force=True)
                self._send_json(
                    {
                        'context': context.to_dict() if context else None,
                        'delay_ms': delay_ms,
                    }
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != '/api/config':
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get('Content-Length', '0') or 0)
            raw = self.rfile.read(length).decode('utf-8') if length else '{}'
            try:
                payload = json.loads(raw or '{}')
                state = save_dictation_ui_state(config, payload.get('state', payload))
            except Exception as error:
                self._send_json({'error': str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(state)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_html(self, html: str) -> None:
            encoded = html.encode('utf-8')
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return DictationUiHandler


def _read_config_for_ui(config: VoxConfig) -> VoxConfig:
    config_path = get_config_path(config)
    data = _load_toml(config_path)
    if not data:
        return config
    live = config.model_copy(deep=True)
    merged = VoxConfig(**data)
    live.dictation = merged.dictation
    return live


def _parse_context_delay_ms(raw: str) -> int:
    try:
        delay_ms = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(delay_ms, 10_000))


_DICTATION_UI_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Vox Dictation Settings</title>
  <style>
    :root {
      --bg: #e9edf3;
      --window: rgba(250, 251, 253, 0.94);
      --sidebar: rgba(243, 245, 248, 0.96);
      --panel: rgba(255, 255, 255, 0.96);
      --panel-muted: rgba(247, 249, 252, 0.96);
      --line: #d7dee8;
      --line-strong: #c5d0dc;
      --text: #1f2937;
      --muted: #66758a;
      --accent: #237bff;
      --accent-soft: rgba(35, 123, 255, 0.10);
      --success: #0f9f6e;
      --success-soft: rgba(15, 159, 110, 0.10);
      --danger: #d1495b;
      --danger-soft: rgba(209, 73, 91, 0.12);
      --shadow: 0 24px 64px rgba(28, 42, 59, 0.16);
      --radius: 22px;
      --radius-sm: 14px;
    }

    * {
      box-sizing: border-box;
    }

    html, body {
      margin: 0;
      min-height: 100%;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(121, 174, 255, 0.20), transparent 28%),
        radial-gradient(circle at bottom right, rgba(170, 190, 210, 0.24), transparent 30%),
        linear-gradient(180deg, #eef2f7 0%, #e4e9f1 100%);
      font-family: "SF Pro Display", "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif;
    }

    body {
      padding: 20px;
    }

    button, input, textarea {
      font: inherit;
    }

    button {
      border: 0;
      cursor: pointer;
      transition: background 140ms ease, border-color 140ms ease, transform 140ms ease, box-shadow 140ms ease;
    }

    button:hover {
      transform: translateY(-1px);
    }

    code {
      font-family: "SF Mono", "Menlo", monospace;
    }

    .window {
      width: min(1280px, 100%);
      height: min(860px, calc(100vh - 40px));
      margin: 0 auto;
      display: grid;
      grid-template-rows: auto 1fr;
      background: var(--window);
      border: 1px solid rgba(197, 208, 220, 0.9);
      border-radius: 28px;
      box-shadow: var(--shadow);
      overflow: hidden;
      backdrop-filter: blur(18px);
    }

    .titlebar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.94) 0%, rgba(247, 249, 252, 0.92) 100%);
    }

    .titlebar-left {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }

    .traffic {
      display: flex;
      gap: 8px;
      align-items: center;
      flex: 0 0 auto;
    }

    .traffic span {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      display: inline-block;
      box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.65);
    }

    .traffic .red { background: #ff5f57; }
    .traffic .yellow { background: #ffbd2e; }
    .traffic .green { background: #28c840; }

    .title {
      min-width: 0;
    }

    .title h1 {
      margin: 0;
      font-size: 15px;
      font-weight: 650;
      letter-spacing: 0.01em;
    }

    .title p {
      margin: 2px 0 0;
      color: var(--muted);
      font-size: 12px;
    }

    .titlebar-right {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(102, 117, 138, 0.10);
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }

    .status-pill::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: currentColor;
      opacity: 0.9;
    }

    .status-pill.ok {
      background: var(--success-soft);
      color: var(--success);
    }

    .btn {
      height: 36px;
      padding: 0 14px;
      border-radius: 10px;
      border: 1px solid transparent;
      font-size: 13px;
      font-weight: 600;
    }

    .btn.secondary {
      color: var(--text);
      background: rgba(255, 255, 255, 0.82);
      border-color: var(--line);
    }

    .btn.primary {
      color: #ffffff;
      background: linear-gradient(180deg, #3491ff 0%, #237bff 100%);
      box-shadow: 0 8px 18px rgba(35, 123, 255, 0.24);
    }

    .workspace {
      min-height: 0;
      display: grid;
      grid-template-columns: 248px minmax(0, 1fr);
    }

    .sidebar {
      min-height: 0;
      overflow: auto;
      padding: 18px 14px 18px 18px;
      background: var(--sidebar);
      border-right: 1px solid var(--line);
    }

    .sidebar-group + .sidebar-group {
      margin-top: 18px;
    }

    .sidebar-label {
      margin: 0 10px 8px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .nav-list {
      display: grid;
      gap: 6px;
    }

    .nav-item {
      width: 100%;
      display: grid;
      gap: 2px;
      text-align: left;
      padding: 12px 12px;
      border-radius: 12px;
      color: var(--text);
      background: transparent;
    }

    .nav-item:hover {
      background: rgba(255, 255, 255, 0.68);
    }

    .nav-item.active {
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--line);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.7);
    }

    .nav-item strong {
      font-size: 13px;
      font-weight: 650;
    }

    .nav-item span {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }

    .meta-card {
      padding: 12px;
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.76);
      border: 1px solid var(--line);
      display: grid;
      gap: 10px;
    }

    .meta-row {
      display: grid;
      gap: 4px;
    }

    .meta-row label {
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .meta-row code,
    .meta-row div {
      font-size: 12px;
      line-height: 1.5;
      color: var(--text);
      overflow-wrap: anywhere;
    }

    .content-wrap {
      min-width: 0;
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 380px;
    }

    .main-pane,
    .inspector {
      min-height: 0;
      overflow: auto;
      padding: 20px;
    }

    .inspector {
      background: rgba(245, 247, 250, 0.92);
      border-left: 1px solid var(--line);
    }

    .pane {
      display: none;
      gap: 16px;
      align-content: start;
    }

    .pane.active {
      display: grid;
    }

    .section-card,
    .preview-card {
      padding: 18px;
      border-radius: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: 0 8px 18px rgba(37, 52, 69, 0.04);
    }

    .section-head,
    .preview-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 14px;
    }

    .section-head h2,
    .preview-head h3 {
      margin: 0;
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    .section-head p,
    .preview-head p {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .section-tag {
      padding: 6px 10px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }

    .settings-block {
      display: grid;
      gap: 14px;
    }

    .field {
      display: grid;
      gap: 8px;
    }

    .field label,
    .mini-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }

    .field input[type="text"],
    .field input[type="number"],
    .field textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel-muted);
      color: var(--text);
      padding: 11px 12px;
      outline: none;
      transition: border-color 140ms ease, box-shadow 140ms ease;
    }

    .field textarea {
      min-height: 180px;
      resize: vertical;
      line-height: 1.6;
    }

    .field input:focus,
    .field textarea:focus {
      border-color: rgba(35, 123, 255, 0.56);
      box-shadow: 0 0 0 4px rgba(35, 123, 255, 0.10);
    }

    .row-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }

    .toggle-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .switch {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: var(--panel-muted);
      font-size: 13px;
      font-weight: 600;
    }

    .switch input {
      margin: 0;
      width: 16px;
      height: 16px;
      accent-color: var(--accent);
    }

    .note {
      padding: 12px 14px;
      border-radius: 12px;
      background: rgba(102, 117, 138, 0.08);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }

    .hotword-list {
      display: grid;
      gap: 12px;
    }

    .hotword-row {
      display: grid;
      grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.25fr) auto;
      gap: 10px;
      align-items: end;
      padding: 12px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: var(--panel-muted);
    }

    .btn-danger {
      height: 40px;
      padding: 0 12px;
      border-radius: 10px;
      background: var(--danger-soft);
      color: var(--danger);
      font-size: 13px;
      font-weight: 700;
    }

    .preview-card + .preview-card {
      margin-top: 16px;
    }

    .preview-meta {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }

    .context-box,
    .log-box {
      width: 100%;
      border-radius: 14px;
      border: 1px solid #202a35;
      background: #111822;
      color: #dde6f0;
      padding: 14px;
      font-family: "SF Mono", "Menlo", monospace;
      font-size: 12px;
      line-height: 1.65;
      white-space: pre-wrap;
      overflow: auto;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .context-box {
      height: 240px;
    }

    .log-box {
      height: 360px;
    }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    .summary {
      padding: 14px;
      border-radius: 14px;
      background: var(--panel-muted);
      border: 1px solid var(--line);
    }

    .summary label {
      display: block;
      margin-bottom: 6px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .summary div {
      font-size: 13px;
      line-height: 1.5;
      overflow-wrap: anywhere;
    }

    @media (max-width: 1180px) {
      .window {
        width: 100%;
        height: auto;
        min-height: calc(100vh - 40px);
      }

      .content-wrap {
        grid-template-columns: 1fr;
      }

      .inspector {
        border-left: 0;
        border-top: 1px solid var(--line);
      }
    }

    @media (max-width: 920px) {
      body {
        padding: 12px;
      }

      .window {
        min-height: calc(100vh - 24px);
        border-radius: 20px;
      }

      .workspace {
        grid-template-columns: 1fr;
      }

      .sidebar {
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }

      .row-grid,
      .hotword-row,
      .summary-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="window">
    <header class="titlebar">
      <div class="titlebar-left">
        <div class="traffic" aria-hidden="true">
          <span class="red"></span>
          <span class="yellow"></span>
          <span class="green"></span>
        </div>
        <div class="title">
          <h1>Vox Dictation 设置</h1>
          <p>本地网页面板，直接管理 <code>~/.vox/config.toml</code></p>
        </div>
      </div>
      <div class="titlebar-right">
        <span class="status-pill" id="statusPill">等待加载</span>
        <button class="btn secondary" id="reloadBtn" type="button">重新载入</button>
        <button class="btn secondary" id="contextBtn" type="button">立即抓取</button>
        <button class="btn secondary" id="contextDelayedBtn" type="button">2 秒后抓取</button>
        <button class="btn secondary" id="logsBtn" type="button">刷新日志</button>
        <button class="btn primary" id="saveBtn" type="button">保存配置</button>
      </div>
    </header>

    <div class="workspace">
      <aside class="sidebar">
        <section class="sidebar-group">
          <div class="sidebar-label">设置项</div>
          <div class="nav-list">
            <button class="nav-item active" id="navContext" data-pane="context" type="button">
              <strong>上下文</strong>
              <span id="contextSummary">未加载</span>
            </button>
            <button class="nav-item" id="navHotwords" data-pane="hotwords" type="button">
              <strong>热词</strong>
              <span id="hotwordSummary">0 条</span>
            </button>
            <button class="nav-item" id="navHints" data-pane="hints" type="button">
              <strong>说话人提示</strong>
              <span id="hintSummary">0 条</span>
            </button>
            <button class="nav-item" id="navDebug" data-pane="debug" type="button">
              <strong>调试预览</strong>
              <span>上下文与日志</span>
            </button>
          </div>
        </section>

        <section class="sidebar-group">
          <div class="sidebar-label">路径</div>
          <div class="meta-card">
            <div class="meta-row">
              <label>配置文件</label>
              <code id="configPath">-</code>
            </div>
            <div class="meta-row">
              <label>日志文件</label>
              <code id="logPath">-</code>
            </div>
          </div>
        </section>

        <section class="sidebar-group">
          <div class="sidebar-label">使用提示</div>
          <div class="meta-card">
            <div class="meta-row">
              <label>启动命令</label>
              <div><code>uv run vox dictation start --lang zh --verbose</code></div>
            </div>
            <div class="meta-row">
              <label>说明</label>
              <div>这里改完后，直接回终端跑一轮，就能从彩色日志和文件日志看到热词、提示词、上下文是否真的生效。</div>
            </div>
          </div>
        </section>
      </aside>

      <div class="content-wrap">
        <main class="main-pane">
          <section class="pane active" id="paneContext">
            <div class="section-card">
              <div class="section-head">
                <div>
                  <h2>上下文策略</h2>
                  <p>录音开始前预先采集焦点内容，用于后续润色与纠错。</p>
                </div>
                <div class="section-tag">Focus</div>
              </div>
              <div class="settings-block">
                <div class="toggle-row">
                  <label class="switch"><input type="checkbox" id="contextEnabled" />启用焦点上下文</label>
                </div>
                <div class="row-grid">
                  <div class="field">
                    <label for="contextMaxChars">注入最大字符数</label>
                    <input id="contextMaxChars" type="number" min="0" step="50" />
                  </div>
                  <div class="note">
                    Ghostty 会优先清洗终端噪音；浏览器会优先抓选中文本、输入框内容或页面尾部片段。这里控制注入给后处理链路的文本窗口大小。
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section class="pane" id="paneHotwords">
            <div class="section-card">
              <div class="section-head">
                <div>
                  <h2>热词词库</h2>
                  <p>维护标准写法与常见误识别，优先修正稳定错词。</p>
                </div>
                <div class="section-tag">Hotwords</div>
              </div>
              <div class="settings-block">
                <div class="toggle-row">
                  <label class="switch"><input type="checkbox" id="hotwordsEnabled" />启用热词</label>
                  <label class="switch"><input type="checkbox" id="rewriteAliases" />先做精确别名改写</label>
                  <label class="switch"><input type="checkbox" id="caseSensitive" />区分大小写</label>
                </div>
                <div class="hotword-list" id="hotwordList"></div>
                <div>
                  <button class="btn secondary" id="addHotwordBtn" type="button">新增热词</button>
                </div>
                <div class="note">
                  左边填标准写法，右边填常见误识别；多个别名用英文逗号分隔。日志里会打印当前热词表和每次命中明细。
                </div>
              </div>
            </div>
          </section>

          <section class="pane" id="paneHints">
            <div class="section-card">
              <div class="section-head">
                <div>
                  <h2>说话人提示</h2>
                  <p>描述说话习惯，而不是写成一整段产品规则。</p>
                </div>
                <div class="section-tag">Hints</div>
              </div>
              <div class="settings-block">
                <div class="toggle-row">
                  <label class="switch"><input type="checkbox" id="hintsEnabled" />启用说话人提示</label>
                </div>
                <div class="field">
                  <label for="hintsInput">每行一条提示</label>
                  <textarea id="hintsInput" placeholder="例如：说话人前后鼻音不分，优先纠正 an/ang、en/eng、in/ing 等常见混淆。"></textarea>
                </div>
                <div class="note">
                  这块更适合放“前后鼻音不分”“专有名词经常读混”等说话特征，便于你持续维护，不会把系统提示词越堆越长。
                </div>
              </div>
            </div>
          </section>

          <section class="pane" id="paneDebug">
            <div class="section-card">
              <div class="section-head">
                <div>
                  <h2>调试预览</h2>
                  <p>看当前面板状态、焦点上下文和最近日志。</p>
                </div>
                <div class="section-tag">Debug</div>
              </div>
              <div class="settings-block">
                <div class="summary-grid">
                  <div class="summary">
                    <label>上下文</label>
                    <div id="debugContextSummary">未加载</div>
                  </div>
                  <div class="summary">
                    <label>热词与提示</label>
                    <div id="debugRulesSummary">未加载</div>
                  </div>
                </div>
                <div class="note">
                  日志区固定高度、内部滚动，不会再把整页撑开。你可以把这里当成一个内置调试抽屉。
                </div>
              </div>
            </div>
          </section>
        </main>

        <aside class="inspector" data-dictation-ignore="true">
          <section class="preview-card">
            <div class="preview-head">
              <div>
                <h3>焦点上下文预览</h3>
                <p>立即抓取适合当前窗口，延时抓取适合先切回 Ghostty 再采集。</p>
              </div>
              <div class="preview-meta" id="contextMeta">未抓取</div>
            </div>
            <div class="context-box" id="contextPreview">当前还没有上下文预览。</div>
          </section>

          <section class="preview-card">
            <div class="preview-head">
              <div>
                <h3>最近日志</h3>
                <p>固定高度，内部滚动。</p>
              </div>
              <div class="preview-meta" id="logsMeta">最近 180 行</div>
            </div>
            <div class="log-box" id="logPreview">正在加载日志...</div>
          </section>
        </aside>
      </div>
    </div>
  </div>

  <script>
    const state = {
      data: null,
      activePane: 'context',
    };

    const $ = (id) => document.getElementById(id);

    function setStatus(text, ok = false) {
      const pill = $('statusPill');
      pill.textContent = text;
      pill.classList.toggle('ok', ok);
    }

    function switchPane(name) {
      state.activePane = name;
      document.querySelectorAll('.nav-item').forEach((item) => {
        item.classList.toggle('active', item.dataset.pane === name);
      });
      document.querySelectorAll('.pane').forEach((pane) => {
        pane.classList.toggle('active', pane.id === `pane${capitalize(name)}`);
      });
    }

    function capitalize(value) {
      return value.charAt(0).toUpperCase() + value.slice(1);
    }

    function hotwordRow(entry = { value: '', aliases: [] }) {
      const wrapper = document.createElement('div');
      wrapper.className = 'hotword-row';
      wrapper.innerHTML = `
        <div class="field">
          <label>标准写法</label>
          <input type="text" class="hotword-value" placeholder="例如：潮汕" value="${escapeHtml(entry.value || '')}" />
        </div>
        <div class="field">
          <label>别名 / 常见误识别</label>
          <input type="text" class="hotword-aliases" placeholder="例如：潮上, 潮山" value="${escapeHtml((entry.aliases || []).join(', '))}" />
        </div>
        <div>
          <button class="btn-danger" type="button">删除</button>
        </div>
      `;
      wrapper.querySelector('button').addEventListener('click', () => wrapper.remove());
      return wrapper;
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
    }

    function countChars(value) {
      return String(value || '').trim().length;
    }

    function renderState(payload) {
      state.data = payload;
      $('configPath').textContent = payload.config_path || '-';
      $('logPath').textContent = payload.log_path || '-';

      const current = payload.state;
      const hotwordEntries = current.hotwords.entries || [];
      const hintItems = (current.hints.items || []).filter(Boolean);

      $('contextEnabled').checked = !!current.context.enabled;
      $('contextMaxChars').value = current.context.max_chars ?? 1200;

      $('hotwordsEnabled').checked = !!current.hotwords.enabled;
      $('rewriteAliases').checked = !!current.hotwords.rewrite_aliases;
      $('caseSensitive').checked = !!current.hotwords.case_sensitive;

      $('hintsEnabled').checked = !!current.hints.enabled;
      $('hintsInput').value = hintItems.join('\n');

      $('contextSummary').textContent = current.context.enabled
        ? `已启用 · ${current.context.max_chars || 0} 字`
        : '已关闭';
      $('hotwordSummary').textContent = `${hotwordEntries.length} 条`;
      $('hintSummary').textContent = `${hintItems.length} 条`;
      $('debugContextSummary').textContent = current.context.enabled
        ? `开启，最多注入 ${current.context.max_chars || 0} 字`
        : '未启用';
      $('debugRulesSummary').textContent = `热词 ${hotwordEntries.length} 条，提示 ${hintItems.length} 条`;

      const list = $('hotwordList');
      list.innerHTML = '';
      if (hotwordEntries.length) {
        hotwordEntries.forEach((entry) => list.appendChild(hotwordRow(entry)));
      } else {
        list.appendChild(hotwordRow());
      }

      $('logPreview').textContent = payload.logs || '还没有日志。';
    }

    function collectState() {
      const rows = Array.from(document.querySelectorAll('.hotword-row'));
      return {
        context: {
          enabled: $('contextEnabled').checked,
          max_chars: Number($('contextMaxChars').value || 0),
        },
        hotwords: {
          enabled: $('hotwordsEnabled').checked,
          rewrite_aliases: $('rewriteAliases').checked,
          case_sensitive: $('caseSensitive').checked,
          entries: rows.map((row) => ({
            value: row.querySelector('.hotword-value').value.trim(),
            aliases: row.querySelector('.hotword-aliases').value.split(',').map((item) => item.trim()).filter(Boolean),
          })).filter((entry) => entry.value),
        },
        hints: {
          enabled: $('hintsEnabled').checked,
          items: $('hintsInput').value.split('\n').map((item) => item.trim()).filter(Boolean),
        },
      };
    }

    function formatContextPreview(context) {
      if (!context) {
        $('contextMeta').textContent = '无可用数据';
        return '当前没有可用的焦点上下文。';
      }

      const excerpt = context.selected_text || context.context_text || '';
      const meta = [
        context.source ? `来源: ${context.source}` : '',
        context.app_name ? `应用: ${context.app_name}` : '',
        context.window_title ? `窗口: ${context.window_title}` : '',
        context.page_url ? `URL: ${context.page_url}` : '',
      ].filter(Boolean);

      $('contextMeta').textContent = `${context.source || 'unknown'} · ${countChars(excerpt)} 字`;
      return [
        ...meta,
        '',
        excerpt || '上下文为空。',
      ].join('\n');
    }

    async function loadState() {
      setStatus('正在加载…');
      const response = await fetch('/api/state');
      const payload = await response.json();
      renderState(payload);
      setStatus('已加载配置', true);
    }

    async function saveState() {
      setStatus('正在保存…');
      const response = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state: collectState() }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || '保存失败');
      }
      renderState(payload);
      setStatus('配置已保存', true);
    }

    async function refreshLogs() {
      const response = await fetch('/api/logs?lines=180');
      const payload = await response.json();
      $('logPreview').textContent = payload.logs || '还没有日志。';
      $('logsMeta').textContent = `最近 180 行 · ${new Date().toLocaleTimeString()}`;
    }

    async function refreshContext(delayMs = 0) {
      if (delayMs > 0) {
        setStatus(`请在 ${Math.round(delayMs / 1000)} 秒内切回目标窗口…`);
        $('contextMeta').textContent = `等待 ${Math.round(delayMs / 1000)} 秒后抓取`;
      } else {
        setStatus('正在抓取上下文…');
      }
      const response = await fetch(`/api/context?delay_ms=${delayMs}`);
      const payload = await response.json();
      $('contextPreview').textContent = formatContextPreview(payload.context);
      if (delayMs > 0) {
        setStatus('延时上下文已更新', true);
      } else {
        setStatus('上下文已更新', true);
      }
    }

    $('saveBtn').addEventListener('click', async () => {
      try {
        await saveState();
      } catch (error) {
        setStatus(error.message || '保存失败');
      }
    });

    $('reloadBtn').addEventListener('click', async () => {
      try {
        await loadState();
      } catch (error) {
        setStatus(error.message || '重新加载失败');
      }
    });

    $('contextBtn').addEventListener('click', async () => {
      try {
        await refreshContext();
      } catch (error) {
        setStatus(error.message || '抓取上下文失败');
      }
    });

    $('contextDelayedBtn').addEventListener('click', async () => {
      try {
        await refreshContext(2000);
      } catch (error) {
        setStatus(error.message || '延时抓取失败');
      }
    });

    $('logsBtn').addEventListener('click', async () => {
      try {
        await refreshLogs();
        setStatus('日志已刷新', true);
      } catch (error) {
        setStatus(error.message || '刷新日志失败');
      }
    });

    $('addHotwordBtn').addEventListener('click', () => {
      $('hotwordList').appendChild(hotwordRow());
    });

    document.querySelectorAll('.nav-item').forEach((item) => {
      item.addEventListener('click', () => switchPane(item.dataset.pane));
    });

    switchPane('context');
    loadState().catch((error) => setStatus(error.message || '加载失败'));
    setInterval(() => {
      refreshLogs().catch(() => {});
    }, 5000);
  </script>
</body>
</html>
"""
