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

from ..config import (
    VoxConfig,
    _load_toml,
    get_config_path,
    get_dictation_prompt_presets,
    resolve_dictation_prompt_selection,
)
from .dictation_context_service import capture_dictation_context
from .dictation_service import dictation_agent_log_path, dictation_session_log_path, tail_session_log


class DictationUiHotwordEntryPayload(BaseModel):
    value: str = ''
    aliases: list[str] = Field(default_factory=list)


class DictationUiTransformsPayload(BaseModel):
    fullwidth_to_halfwidth: bool = False
    space_around_punct: bool = False
    space_between_cjk: bool = False
    strip_trailing_punctuation: bool = False


class DictationUiLlmPayload(BaseModel):
    enabled: bool = False
    provider: str = 'openai-compatible'
    base_url: str = ''
    model: str = ''
    api_key_env: str = 'OPENAI_API_KEY'
    timeout_sec: float = 20.0
    stream: bool = True
    temperature: float = 0.0
    max_tokens: int | None = None
    prompt_preset: str = 'default'
    custom_prompt_enabled: bool = False
    system_prompt: str = ''
    user_prompt_template: str = ''
    api_key_present: bool = False


class DictationUiContextPayload(BaseModel):
    enabled: bool = False
    max_chars: int = 1200
    capture_budget_ms: int = 1200


class DictationUiHotwordsPayload(BaseModel):
    enabled: bool = False
    rewrite_aliases: bool = True
    case_sensitive: bool = False
    entries: list[DictationUiHotwordEntryPayload] = Field(default_factory=list)


class DictationUiHintsPayload(BaseModel):
    enabled: bool = False
    items: list[str] = Field(default_factory=list)


class DictationUiStatePayload(BaseModel):
    transforms: DictationUiTransformsPayload = DictationUiTransformsPayload()
    llm: DictationUiLlmPayload = DictationUiLlmPayload()
    context: DictationUiContextPayload = DictationUiContextPayload()
    hotwords: DictationUiHotwordsPayload = DictationUiHotwordsPayload()
    hints: DictationUiHintsPayload = DictationUiHintsPayload()


_MANAGED_HEADERS = {
    '[dictation.transforms]',
    '[dictation.llm]',
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
    prompt_preset, custom_prompt_enabled, resolved_system_prompt, resolved_user_prompt_template = (
        resolve_dictation_prompt_selection(live.dictation.llm)
    )
    state = DictationUiStatePayload(
        transforms=DictationUiTransformsPayload(
            fullwidth_to_halfwidth=live.dictation.transforms.fullwidth_to_halfwidth,
            space_around_punct=live.dictation.transforms.space_around_punct,
            space_between_cjk=live.dictation.transforms.space_between_cjk,
            strip_trailing_punctuation=live.dictation.transforms.strip_trailing_punctuation,
        ),
        llm=DictationUiLlmPayload(
            enabled=live.dictation.llm.enabled,
            provider=live.dictation.llm.provider,
            base_url=live.dictation.llm.base_url or '',
            model=live.dictation.llm.model or '',
            api_key_env=live.dictation.llm.api_key_env or '',
            timeout_sec=live.dictation.llm.timeout_sec,
            stream=live.dictation.llm.stream,
            temperature=live.dictation.llm.temperature,
            max_tokens=live.dictation.llm.max_tokens,
            prompt_preset=prompt_preset,
            custom_prompt_enabled=custom_prompt_enabled,
            system_prompt=resolved_system_prompt,
            user_prompt_template=resolved_user_prompt_template,
            api_key_present=bool(live.dictation.llm.api_key),
        ),
        context=DictationUiContextPayload(
            enabled=live.dictation.context.enabled,
            max_chars=live.dictation.context.max_chars,
            capture_budget_ms=live.dictation.context.capture_budget_ms,
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
        'agent_log_path': str(dictation_agent_log_path(config)),
        'prompt_presets': [
            {
                'key': preset.key,
                'label': preset.label,
                'system_prompt': preset.system_prompt,
                'user_prompt_template': preset.user_prompt_template,
            }
            for preset in get_dictation_prompt_presets().values()
        ],
        'state': state.model_dump(),
        'logs': tail_session_log(config, lines=120),
    }


def save_dictation_ui_state(config: VoxConfig, payload: dict[str, Any]) -> dict[str, Any]:
    state = DictationUiStatePayload.model_validate(payload)
    config_path = get_config_path(config)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    live = _read_config_for_ui(config)

    existing_text = config_path.read_text(encoding='utf-8') if config_path.exists() else ''
    preserved_text = strip_managed_dictation_ui_sections(existing_text)
    managed_text = render_dictation_ui_sections(
        state,
        preserved_llm_api_key=live.dictation.llm.api_key if state.llm.api_key_present else None,
    )

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


def render_dictation_ui_sections(
    state: DictationUiStatePayload | dict[str, Any],
    *,
    preserved_llm_api_key: str | None = None,
) -> str:
    infer_custom_prompt_enabled = False
    if isinstance(state, dict):
        raw_llm = state.get('llm')
        if isinstance(raw_llm, dict) and 'custom_prompt_enabled' not in raw_llm:
            infer_custom_prompt_enabled = bool(
                str(raw_llm.get('system_prompt', '')).strip()
                or str(raw_llm.get('user_prompt_template', '')).strip()
            )
    state = DictationUiStatePayload.model_validate(state)
    custom_prompt_enabled = bool(state.llm.custom_prompt_enabled or infer_custom_prompt_enabled)
    lines: list[str] = [
        '[dictation.transforms]',
        f'fullwidth_to_halfwidth = {_toml_bool(state.transforms.fullwidth_to_halfwidth)}',
        f'space_around_punct = {_toml_bool(state.transforms.space_around_punct)}',
        f'space_between_cjk = {_toml_bool(state.transforms.space_between_cjk)}',
        f'strip_trailing_punctuation = {_toml_bool(state.transforms.strip_trailing_punctuation)}',
        '',
        '[dictation.llm]',
        f'enabled = {_toml_bool(state.llm.enabled)}',
        f'provider = {_toml_string(state.llm.provider.strip() or "openai-compatible")}',
        f'prompt_preset = {_toml_string(state.llm.prompt_preset.strip() or "default")}',
    ]

    if (base_url := state.llm.base_url.strip()):
        lines.append(f'base_url = {_toml_string(base_url)}')
    if (model := state.llm.model.strip()):
        lines.append(f'model = {_toml_string(model)}')
    if preserved_llm_api_key:
        lines.append(f'api_key = {_toml_string(preserved_llm_api_key)}')
    if (api_key_env := state.llm.api_key_env.strip()):
        lines.append(f'api_key_env = {_toml_string(api_key_env)}')

    lines.extend(
        [
            f'timeout_sec = {_toml_number(max(0.1, float(state.llm.timeout_sec)))}',
            f'stream = {_toml_bool(state.llm.stream)}',
            f'temperature = {_toml_number(float(state.llm.temperature))}',
        ]
    )
    if state.llm.max_tokens is not None and int(state.llm.max_tokens) > 0:
        lines.append(f'max_tokens = {int(state.llm.max_tokens)}')
    if custom_prompt_enabled:
        lines.append(f'system_prompt = {_toml_text(_normalize_text_block(state.llm.system_prompt))}')
        lines.append(f'user_prompt_template = {_toml_text(_normalize_text_block(state.llm.user_prompt_template))}')
    lines.extend(
        [
            '',
            '[dictation.context]',
            f'enabled = {_toml_bool(state.context.enabled)}',
            f'max_chars = {max(0, int(state.context.max_chars))}',
            f'capture_budget_ms = {max(0, int(state.context.capture_budget_ms))}',
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


def _toml_text(value: str) -> str:
    if '\n' in value and "'''" not in value:
        return "'''\n" + value.rstrip('\n') + "\n'''"
    return _toml_string(value)


def _toml_array(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _toml_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    text = f'{float(value):.6f}'.rstrip('0').rstrip('.')
    if '.' not in text and 'e' not in text and 'E' not in text:
        text = f'{text}.0'
    return text


def _normalize_text_block(value: str) -> str:
    return value.replace('\r\n', '\n').strip('\n')


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
  <title>Vox Dictation</title>
  <style>
    :root {
      --app-width: 1028px;
      --app-height: 758px;
      --sidebar-width: 248px;
      --font-color: rgba(255, 255, 255, 0.9);
      --font-color-muted: rgba(255, 255, 255, 0.45);
      --window-bg: rgba(34, 34, 38, 0.78);
      --sidebar-bg: rgba(30, 30, 30, 0.7);
      --content-bg: rgba(31, 31, 35, 0.74);
      --panel-bg: rgba(255, 255, 255, 0.04);
      --panel-bg-soft: rgba(255, 255, 255, 0.024);
      --panel-bg-emphasis: rgba(255, 255, 255, 0.06);
      --border: rgba(255, 255, 255, 0.1);
      --border-soft: rgba(255, 255, 255, 0.085);
      --border-separator: rgba(255, 255, 255, 0.06);
      --input-bg: rgba(0, 0, 0, 0.18);
      --input-focus: rgba(0, 0, 0, 0.22);
      --selected: #255fbd;
      --accent: #007aff;
      --switch-on: #34c759;
      --success: #34c759;
      --danger: #f25f58;
      --shadow-window: 0 28px 72px rgba(0, 0, 0, 0.34);
      --radius-lg: 16px;
      --radius-md: 12px;
      --radius-sm: 8px;
    }

    * {
      box-sizing: border-box;
    }

    html,
    body {
      margin: 0;
      min-height: 100%;
      color: var(--font-color);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif;
      font-size: 13px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }

    body {
      min-height: 100vh;
      padding: 24px;
      position: relative;
      overflow: hidden;
      display: flex;
      justify-content: center;
      align-items: center;
      background:
        radial-gradient(circle at 18% 12%, rgba(132, 151, 176, 0.22), transparent 22%),
        linear-gradient(180deg, #71829a 0%, #2f3945 100%);
    }

    body::before,
    body::after {
      content: "";
      position: fixed;
      pointer-events: none;
      opacity: 0.38;
      z-index: 0;
    }

    body::before {
      inset: auto auto -24vh -20vw;
      width: 48vw;
      height: 42vh;
      background: linear-gradient(180deg, rgba(42, 49, 86, 0.44), rgba(32, 34, 73, 0.34));
      clip-path: polygon(0 8%, 74% 0, 100% 80%, 34% 100%);
      filter: blur(8px);
    }

    body::after {
      inset: -18vh -22vw auto auto;
      width: 34vw;
      height: 38vh;
      background: linear-gradient(180deg, rgba(103, 92, 160, 0.34), rgba(98, 126, 208, 0.2));
      clip-path: polygon(18% 0, 100% 0, 100% 72%, 0 100%);
      filter: blur(10px);
    }

    button,
    input,
    textarea {
      font: inherit;
    }

    button {
      border: 0;
      cursor: pointer;
    }

    code {
      font-family: "SF Mono", "Menlo", monospace;
    }

    .app-window {
      width: min(100%, var(--app-width));
      height: min(calc(100vh - 48px), var(--app-height));
      display: flex;
      position: relative;
      z-index: 1;
      border-radius: var(--radius-lg);
      overflow: hidden;
      border: 0.5px solid var(--border);
      box-shadow: var(--shadow-window);
      backdrop-filter: blur(24px) saturate(1.04);
      background: var(--window-bg);
    }

    .app-window::before {
      content: "";
      position: absolute;
      inset: 0;
      box-shadow: inset 0 0 0 0.5px rgba(255, 255, 255, 0.08);
      border-radius: inherit;
      pointer-events: none;
    }

    .sidebar {
      width: var(--sidebar-width);
      display: flex;
      flex-direction: column;
      padding: 24px 16px;
      background: var(--sidebar-bg);
      border-right: 0.5px solid var(--border);
      backdrop-filter: blur(20px);
    }

    .window-actions {
      display: flex;
      gap: 8px;
      padding: 0 0 20px 4px;
    }

    .window-dot {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      display: inline-block;
    }

    .window-dot.red { background: #ff5f57; }
    .window-dot.yellow { background: #ffbd2e; }
    .window-dot.green { background: #28c840; }

    .user-card {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 6px 8px 14px;
      margin-bottom: 10px;
      border-radius: 8px;
    }

    .user-avatar {
      width: 34px;
      height: 34px;
      border-radius: 11px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(180deg, #2d86ff 0%, #1149c7 100%);
      color: white;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.16);
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.08em;
    }

    .user-copy {
      min-width: 0;
      display: grid;
      gap: 3px;
    }

    .user-copy strong {
      font-size: 14px;
      font-weight: 600;
      letter-spacing: -0.01em;
    }

    .user-copy span {
      color: var(--font-color-muted);
      font-size: 11px;
    }

    .nav-list {
      display: grid;
      gap: 6px;
      min-height: 0;
      overflow: auto;
      padding-right: 2px;
      scrollbar-width: none;
    }

    .nav-list::-webkit-scrollbar {
      display: none;
    }

    .nav-item {
      width: 100%;
      display: flex;
      align-items: center;
      gap: 12px;
      min-height: 40px;
      padding: 8px 12px;
      border-radius: 8px;
      border: 0.5px solid transparent;
      background: transparent;
      color: var(--font-color);
      text-align: left;
      transition: background 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
    }

    .nav-item:hover:not(.active) {
      background: rgba(255, 255, 255, 0.04);
      border-color: rgba(255, 255, 255, 0.04);
    }

    .nav-item.active {
      background: linear-gradient(180deg, rgba(44, 108, 200, 0.96) 0%, rgba(34, 92, 186, 0.94) 100%);
      border-color: rgba(255, 255, 255, 0.08);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.12);
    }

    .nav-icon {
      width: 20px;
      height: 20px;
      border-radius: 6px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
      background: rgba(255, 255, 255, 0.1);
      border: 0.5px solid rgba(255, 255, 255, 0.06);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.05em;
    }

    .nav-copy {
      min-width: 0;
      display: grid;
      gap: 1px;
    }

    .nav-copy strong {
      font-size: 13px;
      font-weight: 600;
      line-height: 1.15;
    }

    .nav-copy span {
      display: none;
    }

    .content-view {
      min-width: 0;
      flex: 1;
      display: flex;
      flex-direction: column;
      position: relative;
      overflow: hidden;
      background: var(--content-bg);
    }

    .content-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 40px 12px 36px;
      background: rgba(31, 31, 35, 0.68);
      border-bottom: 0.5px solid var(--border);
      backdrop-filter: blur(20px);
      position: absolute;
      inset: 0 0 auto 0;
      z-index: 3;
    }

    .header-leading {
      display: flex;
      align-items: center;
      gap: 11px;
      min-width: 0;
    }

    .history-strip {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      color: var(--font-color-muted);
    }

    .history-btn {
      width: 24px;
      height: 24px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      border: 1px solid transparent;
      background: transparent;
      color: inherit;
      cursor: default;
      font-size: 18px;
      line-height: 1;
    }

    .history-btn[disabled] {
      opacity: 0.7;
    }

    .content-header h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 600;
      letter-spacing: -0.02em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .header-actions {
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
      min-height: 20px;
      padding: 0;
      border-radius: 0;
      background: transparent;
      color: var(--font-color-muted);
      font-size: 12px;
      font-weight: 500;
    }

    .status-pill::before {
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.28);
    }

    .status-pill.ok {
      color: var(--success);
    }

    .status-pill.ok::before {
      background: currentColor;
    }

    .btn {
      height: 32px;
      padding: 0 14px;
      border-radius: 8px;
      border: 0.5px solid transparent;
      font-size: 12px;
      font-weight: 600;
      transition: background 140ms ease, border-color 140ms ease, transform 120ms ease;
    }

    .btn:hover {
      transform: translateY(-1px);
    }

    .btn.secondary {
      color: var(--font-color);
      background: rgba(255, 255, 255, 0.05);
      border-color: rgba(255, 255, 255, 0.08);
    }

    .btn.primary {
      color: white;
      background: linear-gradient(180deg, #2490ff 0%, #007aff 100%);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.16);
    }

    .btn-danger {
      width: 18px;
      height: 18px;
      padding: 0;
      border-radius: 999px;
      background: transparent;
      color: rgba(255, 255, 255, 0.22);
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
      border: 0;
      opacity: 0;
      transform: scale(0.96);
      transition: opacity 120ms ease, transform 120ms ease, background 120ms ease, color 120ms ease;
    }

    .hotword-row:hover .btn-danger,
    .hotword-row:focus-within .btn-danger {
      opacity: 1;
      transform: scale(1);
    }

    .btn-danger:hover {
      background: rgba(255, 255, 255, 0.06);
      color: rgba(255, 255, 255, 0.62);
    }

    .content-scroll {
      flex: 1;
      overflow: auto;
      padding: 80px 40px 34px;
    }

    .pane {
      display: none;
    }

    .pane.active {
      display: block;
    }

    .group {
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-bottom: 20px;
    }

    .group h2 {
      margin: 0;
      padding-left: 2px;
      font-size: 13px;
      font-weight: 600;
      letter-spacing: -0.01em;
      color: rgba(255, 255, 255, 0.72);
    }

    .settings-items {
      display: flex;
      flex-direction: column;
      background: var(--panel-bg);
      border-radius: var(--radius-sm);
      border: 0.5px solid var(--border);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
      overflow: hidden;
      backdrop-filter: blur(18px);
    }

    .setting-row {
      display: flex;
      justify-content: space-between;
      gap: 24px;
      padding: 14px 16px;
      align-items: flex-start;
    }

    .setting-row + .setting-row {
      border-top: 0.5px solid var(--border-separator);
    }

    .setting-copy {
      width: 128px;
      flex: 0 0 128px;
      display: grid;
      gap: 3px;
      padding-top: 2px;
    }

    .setting-copy strong {
      font-size: 13px;
      font-weight: 600;
    }

    .setting-copy p {
      margin: 0;
      color: var(--font-color-muted);
      font-size: 11px;
      line-height: 1.35;
    }

    .setting-control {
      flex: 1;
      display: flex;
      justify-content: flex-start;
      align-items: flex-start;
      min-width: 0;
    }

    .field,
    .field-stack {
      display: grid;
      gap: 8px;
      width: min(100%, 520px);
    }

    .field label {
      color: var(--font-color-muted);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.01em;
    }

    .field input[type="text"],
    .field input[type="number"],
    .field textarea,
    .field select {
      width: 100%;
      background: var(--input-bg);
      border: 0.5px solid var(--border-soft);
      border-radius: 6px;
      color: inherit;
      outline: none;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
      font-size: 13px;
    }

    .field input[type="text"],
    .field input[type="number"],
    .field select {
      height: 32px;
      padding: 0 10px;
    }

    .field input:focus,
    .field textarea:focus,
    .field select:focus {
      background: var(--input-focus);
      box-shadow: 0 0 0 2px rgba(0, 122, 255, 0.32);
      border-color: rgba(0, 122, 255, 0.72);
    }

    .field textarea {
      min-height: 112px;
      padding: 10px 11px;
      resize: vertical;
      line-height: 1.52;
    }

    .field textarea:disabled,
    .field select:disabled,
    .field input:disabled {
      opacity: 0.58;
      cursor: not-allowed;
    }

    .prompt-row {
      align-items: stretch;
    }

    .prompt-control {
      width: min(100%, 568px);
      display: grid;
    }

    .prompt-editor {
      width: 100%;
      display: grid;
      border-radius: 10px;
      overflow: hidden;
      border: 0.5px solid var(--border);
      background: rgba(255, 255, 255, 0.02);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
      backdrop-filter: blur(18px);
    }

    .prompt-editor-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 40px;
      padding: 9px 14px;
      border-bottom: 0.5px solid var(--border-separator);
      background: rgba(255, 255, 255, 0.025);
    }

    .prompt-editor-title {
      display: grid;
      gap: 2px;
      min-width: 0;
    }

    .prompt-editor-title strong {
      font-size: 12px;
      font-weight: 600;
      letter-spacing: -0.01em;
    }

    .prompt-editor-title span {
      color: var(--font-color-muted);
      font-size: 10px;
      line-height: 1.2;
    }

    .prompt-editor-mode {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--font-color-muted);
      font-size: 11px;
      font-weight: 500;
      white-space: nowrap;
    }

    .prompt-editor-mode::before {
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: currentColor;
      opacity: 0.85;
    }

    .prompt-editor textarea {
      min-height: 170px;
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 15px 14px;
      box-shadow: none;
      resize: vertical;
      line-height: 1.58;
    }

    .prompt-editor textarea:focus {
      background: transparent;
      box-shadow: none;
      border-color: transparent;
    }

    .prompt-editor textarea:disabled {
      opacity: 1;
      cursor: default;
    }

    .prompt-editor.is-readonly textarea {
      color: rgba(235, 230, 238, 0.74);
    }

    .prompt-editor.is-custom {
      border-color: rgba(0, 122, 255, 0.34);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.04),
        0 0 0 1px rgba(0, 122, 255, 0.14);
    }

    .prompt-editor.is-custom .prompt-editor-mode {
      color: #7fb1ff;
    }

    .row-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      width: min(100%, 520px);
    }

    .toggle-row {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-start;
      gap: 10px 12px;
      width: min(100%, 520px);
    }

    .switch {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      font-weight: 500;
    }

    .switch input {
      appearance: none;
      -webkit-appearance: none;
      margin: 0;
      width: 26px;
      height: 16px;
      border: 0.5px solid var(--border-soft);
      border-radius: 999px;
      background: var(--panel-bg-emphasis);
      position: relative;
      cursor: pointer;
      transition: background 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06);
    }

    .switch input::after {
      content: "";
      position: absolute;
      top: 2px;
      left: 2px;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.45);
      transition: transform 140ms ease;
    }

    .switch input:checked {
      background: var(--switch-on);
      border-color: rgba(52, 199, 89, 0.68);
    }

    .switch input:checked::after {
      transform: translateX(10px);
    }

    .switch input:focus-visible {
      box-shadow: 0 0 0 2px rgba(0, 122, 255, 0.28);
    }

    .switch input:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }

    .lexicon-pane {
      width: min(100%, 760px);
    }

    .lexicon-stack {
      display: grid;
      gap: 14px;
    }

    .surface-card {
      background: var(--panel-bg);
      border: 0.5px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
      backdrop-filter: blur(18px);
    }

    .card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px 12px;
      border-bottom: 0.5px solid var(--border-separator);
    }

    .card-copy {
      display: grid;
      gap: 3px;
      min-width: 0;
    }

    .card-copy strong {
      color: rgba(255, 255, 255, 0.9);
      font-size: 13px;
      font-weight: 600;
    }

    .card-copy p {
      margin: 0;
      color: var(--font-color-muted);
      font-size: 11px;
      line-height: 1.35;
    }

    .card-body {
      display: grid;
      gap: 14px;
      padding: 14px 16px 16px;
    }

    .card-body .setting-row {
      padding: 0;
    }

    .card-body .setting-row + .setting-row {
      margin-top: 2px;
      padding-top: 14px;
      border-top: 0.5px solid var(--border-separator);
    }

    .hotword-list-frame {
      border: 0.5px solid rgba(255, 255, 255, 0.08);
      border-radius: 9px;
      overflow: hidden;
      background: rgba(0, 0, 0, 0.14);
    }

    .hotword-list-header {
      display: grid;
      grid-template-columns: minmax(0, 0.86fr) minmax(0, 1.14fr) 20px;
      gap: 12px;
      align-items: center;
      padding: 9px 12px;
      border-bottom: 0.5px solid rgba(255, 255, 255, 0.06);
      background: rgba(255, 255, 255, 0.018);
      color: var(--font-color-muted);
      font-size: 11px;
      font-weight: 600;
    }

    .hotword-list {
      display: grid;
      width: 100%;
    }

    .hotword-row {
      display: grid;
      grid-template-columns: minmax(0, 0.86fr) minmax(0, 1.14fr) 20px;
      gap: 12px;
      align-items: center;
      padding: 10px 12px;
      border-bottom: 0.5px solid rgba(255, 255, 255, 0.05);
    }

    .hotword-row:last-child {
      border-bottom: 0;
    }

    .hotword-input {
      width: 100%;
      height: 28px;
      padding: 0 8px;
      border-radius: 6px;
      border: 0.5px solid rgba(255, 255, 255, 0.1);
      background: rgba(0, 0, 0, 0.2);
      color: #eee;
      font-size: 13px;
      outline: none;
    }

    .hotword-input:focus {
      border-color: rgba(0, 122, 255, 0.72);
      box-shadow: 0 0 0 2px rgba(0, 122, 255, 0.36);
    }

    .hotword-remove {
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.08);
      color: var(--font-color-muted);
      font-size: 11px;
      font-weight: 700;
    }

    .inline-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }

    .context-box,
    .log-box {
      width: 100%;
      border-radius: 8px;
      border: 0.5px solid var(--border-soft);
      background: rgba(14, 17, 22, 0.82);
      color: #dde6f0;
      padding: 14px;
      font-family: "SF Mono", "Menlo", monospace;
      font-size: 12px;
      line-height: 1.65;
      white-space: pre-wrap;
      overflow: auto;
      overflow-wrap: anywhere;
      word-break: break-word;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
    }

    .context-box { height: 240px; }
    .log-box { height: 360px; }

    .path-stack {
      display: grid;
      gap: 8px;
      width: min(100%, 560px);
    }

    .path-item {
      display: grid;
      gap: 4px;
    }

    .path-item span {
      color: var(--font-color-muted);
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.01em;
    }

    .path-item code {
      display: block;
      padding: 10px 12px;
      border-radius: 8px;
      border: 0.5px solid var(--border-soft);
      background: var(--panel-bg-soft);
      color: var(--font-color);
      line-height: 1.45;
      word-break: break-word;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
    }

    .subtle {
      color: var(--font-color-muted);
      font-size: 12px;
      line-height: 1.4;
      padding-left: 12px;
    }

    @media (max-width: 920px) {
      body {
        padding: 12px;
      }

      .app-window {
        height: auto;
        min-height: calc(100vh - 24px);
        flex-direction: column;
      }

      .sidebar {
        width: 100%;
        border-right: 0;
        border-bottom: 0.5px solid var(--border);
      }

      .content-header {
        position: static;
      }

      .content-scroll {
        padding: 16px 16px 24px;
      }

      .setting-row,
      .hotword-row,
      .row-grid {
        grid-template-columns: 1fr;
        flex-direction: column;
      }

      .card-head {
        flex-direction: column;
        align-items: flex-start;
      }

      .hotword-list-header {
        display: none;
      }

      .btn-danger {
        opacity: 1;
        transform: scale(1);
      }

      .setting-copy {
        width: auto;
        flex-basis: auto;
      }

      .setting-control,
      .toggle-row {
        justify-content: flex-start;
      }
    }
  </style>
</head>
<body>
  <div class="app-window">
    <aside class="sidebar">
      <div class="window-actions" aria-hidden="true">
        <span class="window-dot red"></span>
        <span class="window-dot yellow"></span>
        <span class="window-dot green"></span>
      </div>

      <div class="user-card">
        <div class="user-avatar">VO</div>
        <div class="user-copy">
          <strong>Vox Dictation</strong>
          <span>Settings</span>
        </div>
      </div>

      <nav class="nav-list">
        <button class="nav-item active" data-pane="rewrite" type="button">
          <span class="nav-icon">AI</span>
          <span class="nav-copy">
            <strong>输入修订</strong>
            <span id="rewriteSummary">未加载</span>
          </span>
        </button>
        <button class="nav-item" data-pane="model" type="button">
          <span class="nav-icon">ML</span>
          <span class="nav-copy">
            <strong>模型接入</strong>
            <span id="modelSummary">未加载</span>
          </span>
        </button>
        <button class="nav-item" data-pane="context" type="button">
          <span class="nav-icon">FX</span>
          <span class="nav-copy">
            <strong>上下文增强</strong>
            <span id="contextSummary">未加载</span>
          </span>
        </button>
        <button class="nav-item" data-pane="lexicon" type="button">
          <span class="nav-icon">LX</span>
          <span class="nav-copy">
            <strong>词库与习惯</strong>
            <span id="lexiconSummary">未加载</span>
          </span>
        </button>
        <button class="nav-item" data-pane="run" type="button">
          <span class="nav-icon">QA</span>
          <span class="nav-copy">
            <strong>运行检查</strong>
            <span id="runSummary">日志</span>
          </span>
        </button>
      </nav>
    </aside>

    <section class="content-view">
      <header class="content-header">
        <div class="header-leading">
          <div class="history-strip" aria-hidden="true">
            <button class="history-btn" type="button" disabled tabindex="-1">‹</button>
            <button class="history-btn" type="button" disabled tabindex="-1">›</button>
          </div>
          <h1 id="pageTitle">输入修订</h1>
        </div>
        <div class="header-actions">
          <span class="status-pill" id="statusPill">等待加载</span>
          <button class="btn secondary" id="reloadBtn" type="button">重新载入</button>
          <button class="btn primary" id="saveBtn" type="button">保存</button>
        </div>
      </header>

      <div class="content-scroll">
        <section class="pane active" id="paneRewrite">
          <section class="group">
            <h2>修订</h2>
            <div class="settings-items">
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>LLM 修订</strong>
                  <p>最终输出</p>
                </div>
                <div class="setting-control">
                  <div class="toggle-row">
                    <label class="switch"><input type="checkbox" id="llmEnabled" />启用</label>
                  </div>
                </div>
              </div>
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>提示词</strong>
                  <p>预设与覆盖</p>
                </div>
                <div class="setting-control">
                  <div class="row-grid">
                    <div class="field">
                      <label for="promptPreset">预设</label>
                      <select id="promptPreset"></select>
                    </div>
                    <div class="field">
                      <label>覆盖</label>
                      <div class="toggle-row">
                        <label class="switch"><input type="checkbox" id="customPromptEnabled" />覆盖预设</label>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
              <div class="setting-row prompt-row">
                <div class="setting-copy">
                  <strong>系统</strong>
                  <p>角色和边界</p>
                </div>
                <div class="setting-control">
                  <div class="prompt-control">
                    <div class="prompt-editor" id="systemPromptPanel">
                      <div class="prompt-editor-head">
                        <div class="prompt-editor-title">
                          <strong>系统提示词</strong>
                          <span>角色、边界、输出要求</span>
                        </div>
                        <span class="prompt-editor-mode" id="systemPromptMode">跟随预设</span>
                      </div>
                      <textarea id="systemPrompt"></textarea>
                    </div>
                  </div>
                </div>
              </div>
              <div class="setting-row prompt-row">
                <div class="setting-copy">
                  <strong>模板</strong>
                  <p><code>{language}</code> · <code>{text}</code></p>
                </div>
                <div class="setting-control">
                  <div class="prompt-control">
                    <div class="prompt-editor" id="userPromptPanel">
                      <div class="prompt-editor-head">
                        <div class="prompt-editor-title">
                          <strong>用户模板</strong>
                          <span>可用变量: <code>{language}</code> <code>{text}</code></span>
                        </div>
                        <span class="prompt-editor-mode" id="userPromptMode">跟随预设</span>
                      </div>
                      <textarea id="userPromptTemplate"></textarea>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section class="group">
            <h2>规则</h2>
            <div class="settings-items">
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>基础整理</strong>
                  <p>轻规则</p>
                </div>
                <div class="setting-control">
                  <div class="toggle-row">
                    <label class="switch"><input type="checkbox" id="fullwidthToHalfwidth" />全半角</label>
                    <label class="switch"><input type="checkbox" id="spaceAroundPunct" />标点空格</label>
                    <label class="switch"><input type="checkbox" id="spaceBetweenCjk" />中英空格</label>
                    <label class="switch"><input type="checkbox" id="stripTrailingPunctuation" />尾部标点</label>
                  </div>
                </div>
              </div>
            </div>
          </section>
        </section>

        <section class="pane" id="paneModel">
          <section class="group">
            <h2>模型</h2>
            <div class="settings-items">
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>Provider / Model</strong>
                  <p>服务与模型</p>
                </div>
                <div class="setting-control">
                  <div class="row-grid">
                    <div class="field">
                      <label for="llmProvider">服务商</label>
                      <input id="llmProvider" type="text" list="providerOptions" />
                    </div>
                    <div class="field">
                      <label for="llmModel">模型</label>
                      <input id="llmModel" type="text" />
                    </div>
                  </div>
                </div>
              </div>
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>Base URL</strong>
                  <p>兼容接口</p>
                </div>
                <div class="setting-control">
                  <div class="field">
                    <label for="llmBaseUrl">接口地址</label>
                    <input id="llmBaseUrl" type="text" />
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section class="group">
            <h2>参数</h2>
            <div class="settings-items">
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>API Key Env</strong>
                  <p id="inlineKeyNotice">本地环境变量</p>
                </div>
                <div class="setting-control">
                  <div class="field">
                    <label for="llmApiKeyEnv">环境变量</label>
                    <input id="llmApiKeyEnv" type="text" />
                  </div>
                </div>
              </div>
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>推理参数</strong>
                  <p>超时、温度、输出</p>
                </div>
                <div class="setting-control">
                  <div class="row-grid">
                    <div class="field">
                      <label for="llmTimeoutSec">超时</label>
                      <input id="llmTimeoutSec" type="number" min="0.1" step="0.1" />
                    </div>
                    <div class="field">
                      <label for="llmTemperature">温度</label>
                      <input id="llmTemperature" type="number" min="0" step="0.1" />
                    </div>
                    <div class="field">
                      <label for="llmMaxTokens">长度</label>
                      <input id="llmMaxTokens" type="number" min="1" step="1" />
                    </div>
                    <div class="field">
                      <label>流式</label>
                      <div class="toggle-row">
                        <label class="switch"><input type="checkbox" id="llmStream" />启用</label>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </section>
        </section>

        <section class="pane" id="paneContext">
          <section class="group">
            <h2>焦点上下文</h2>
            <div class="settings-items">
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>启用</strong>
                  <p>录音前抓取</p>
                </div>
                <div class="setting-control">
                  <div class="toggle-row">
                    <label class="switch"><input type="checkbox" id="contextEnabled" />启用</label>
                  </div>
                </div>
              </div>
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>预算</strong>
                  <p>字符与时间</p>
                </div>
                <div class="setting-control">
                  <div class="row-grid">
                    <div class="field">
                      <label for="contextMaxChars">最大字数</label>
                      <input id="contextMaxChars" type="number" min="0" step="50" />
                    </div>
                    <div class="field">
                      <label for="contextCaptureBudgetMs">预算毫秒</label>
                      <input id="contextCaptureBudgetMs" type="number" min="0" step="50" />
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </section>
        </section>

        <section class="pane lexicon-pane" id="paneLexicon">
          <div class="lexicon-stack">
            <section class="surface-card">
              <div class="card-head">
                <div class="card-copy">
                  <strong>热词策略</strong>
                  <p>决定词表如何参与修正</p>
                </div>
              </div>
              <div class="card-body">
                <div class="setting-row">
                  <div class="setting-copy">
                    <strong>应用方式</strong>
                    <p>按你的写法优先输出</p>
                  </div>
                  <div class="setting-control">
                    <div class="toggle-row">
                      <label class="switch"><input type="checkbox" id="hotwordsEnabled" />启用</label>
                      <label class="switch"><input type="checkbox" id="rewriteAliases" />别名改写</label>
                      <label class="switch"><input type="checkbox" id="caseSensitive" />区分大小写</label>
                    </div>
                  </div>
                </div>
              </div>
            </section>

            <section class="surface-card">
              <div class="card-head">
                <div class="card-copy">
                  <strong>词表</strong>
                  <p>左边是标准写法，右边是常见别名</p>
                </div>
                <button class="btn secondary" id="addHotwordBtn" type="button">新增热词</button>
              </div>
              <div class="card-body">
                <div class="hotword-list-frame">
                  <div class="hotword-list-header">
                    <span>标准写法</span>
                    <span>常见别名</span>
                    <span aria-hidden="true"></span>
                  </div>
                  <div class="hotword-list" id="hotwordList"></div>
                </div>
              </div>
            </section>

            <section class="surface-card">
              <div class="card-head">
                <div class="card-copy">
                  <strong>表达提示</strong>
                  <p>每行写一条常见习惯或固定表达</p>
                </div>
              </div>
              <div class="card-body">
                <div class="setting-row">
                  <div class="setting-copy">
                    <strong>启用</strong>
                    <p>只在需要时打开</p>
                  </div>
                  <div class="setting-control">
                    <div class="toggle-row">
                      <label class="switch"><input type="checkbox" id="hintsEnabled" />启用</label>
                    </div>
                  </div>
                </div>
                <div class="setting-row">
                  <div class="setting-copy">
                    <strong>提示内容</strong>
                    <p>每行一条，尽量短句</p>
                  </div>
                  <div class="setting-control">
                    <div class="field">
                      <label for="hintsInput">表达提示</label>
                      <textarea id="hintsInput"></textarea>
                    </div>
                  </div>
                </div>
              </div>
            </section>
          </div>
        </section>

        <section class="pane" id="paneRun">
          <section class="group">
            <h2>文件</h2>
            <div class="settings-items">
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>本地路径</strong>
                  <p>配置与日志</p>
                </div>
                <div class="setting-control">
                  <div class="path-stack">
                    <div class="path-item">
                      <span>配置文件</span>
                      <code id="configPath">-</code>
                    </div>
                    <div class="path-item">
                      <span>日志文件</span>
                      <code id="logPath">-</code>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section class="group">
            <h2>上下文快照</h2>
            <div class="settings-items">
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>采样</strong>
                  <p><span class="badge" id="contextMeta">未抓取</span></p>
                </div>
                <div class="setting-control">
                  <div class="field-stack" style="width:min(100%, 560px);">
                    <div class="inline-actions">
                      <button class="btn secondary" id="contextBtn" type="button">立即采集</button>
                      <button class="btn secondary" id="contextDelayedBtn" type="button">2 秒后</button>
                    </div>
                    <div class="context-box" id="contextPreview">当前没有上下文。</div>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section class="group">
            <h2>最近日志</h2>
            <div class="settings-items">
              <div class="setting-row">
                <div class="setting-copy">
                  <strong>会话日志</strong>
                  <p><span class="badge" id="logsMeta">最近 180 行</span></p>
                </div>
                <div class="setting-control">
                  <div class="field-stack" style="width:min(100%, 560px);">
                    <div class="inline-actions">
                      <button class="btn secondary" id="logsBtn" type="button">刷新</button>
                    </div>
                    <div class="log-box" id="logPreview">正在加载日志...</div>
                  </div>
                </div>
              </div>
            </div>
          </section>
        </section>
      </div>
    </section>
  </div>

  <datalist id="providerOptions">
    <option value="openai-compatible"></option>
    <option value="openrouter"></option>
    <option value="dashscope"></option>
    <option value="moonshot"></option>
    <option value="siliconflow"></option>
  </datalist>

  <script>
    const state = {
      data: null,
      activePane: 'rewrite',
    };

    const paneMeta = {
      rewrite: { title: '输入修订' },
      model: { title: '模型接入' },
      context: { title: '上下文增强' },
      lexicon: { title: '词库与习惯' },
      run: { title: '运行检查' },
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
      $('pageTitle').textContent = (paneMeta[name] || paneMeta.rewrite).title;
    }

    function capitalize(value) {
      return value.charAt(0).toUpperCase() + value.slice(1);
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

    function normalizeInline(value) {
      return String(value || '').replace(/\s+/g, ' ').trim();
    }

    function readNumber(id, fallback = 0) {
      const raw = String($(id).value || '').trim();
      if (!raw) return fallback;
      const value = Number(raw);
      return Number.isFinite(value) ? value : fallback;
    }

    function readOptionalInt(id) {
      const raw = String($(id).value || '').trim();
      if (!raw) return null;
      const value = Number(raw);
      if (!Number.isFinite(value) || value <= 0) return null;
      return Math.round(value);
    }

    function getPromptPresets() {
      return (state.data && state.data.prompt_presets) || [];
    }

    function findPromptPreset(key) {
      return getPromptPresets().find((preset) => preset.key === key) || getPromptPresets()[0] || null;
    }

    function applyPromptPreset(key) {
      const preset = findPromptPreset(key);
      if (!preset) return;
      $('systemPrompt').value = preset.system_prompt || '';
      $('userPromptTemplate').value = preset.user_prompt_template || '';
    }

    function syncPromptEditors() {
      const customEnabled = $('customPromptEnabled').checked;
      $('systemPrompt').disabled = !customEnabled;
      $('userPromptTemplate').disabled = !customEnabled;
      $('systemPromptPanel').classList.toggle('is-custom', customEnabled);
      $('userPromptPanel').classList.toggle('is-custom', customEnabled);
      $('systemPromptPanel').classList.toggle('is-readonly', !customEnabled);
      $('userPromptPanel').classList.toggle('is-readonly', !customEnabled);
      $('systemPromptMode').textContent = customEnabled ? '自定义' : '跟随预设';
      $('userPromptMode').textContent = customEnabled ? '自定义' : '跟随预设';
    }

    function hotwordRow(entry = { value: '', aliases: [] }) {
      const wrapper = document.createElement('div');
      wrapper.className = 'hotword-row';
      wrapper.innerHTML = `
        <input
          type="text"
          class="hotword-input hotword-value"
          aria-label="标准写法"
          placeholder="例如：潮汕"
          value="${escapeHtml(entry.value || '')}"
        />
        <input
          type="text"
          class="hotword-input hotword-aliases"
          aria-label="常见别名"
          placeholder="例如：潮上, 潮山"
          value="${escapeHtml((entry.aliases || []).join(', '))}"
        />
        <div class="hotword-remove">
          <button class="btn-danger" type="button" aria-label="删除这一行">x</button>
        </div>
      `;
      wrapper.querySelector('button').addEventListener('click', () => {
        wrapper.remove();
        if (!$('hotwordList').children.length) {
          $('hotwordList').appendChild(hotwordRow());
        }
        updateLiveSummary();
      });
      return wrapper;
    }

    function collectState() {
      const rows = Array.from(document.querySelectorAll('.hotword-row'));
      return {
        transforms: {
          fullwidth_to_halfwidth: $('fullwidthToHalfwidth').checked,
          space_around_punct: $('spaceAroundPunct').checked,
          space_between_cjk: $('spaceBetweenCjk').checked,
          strip_trailing_punctuation: $('stripTrailingPunctuation').checked,
        },
        llm: {
          enabled: $('llmEnabled').checked,
          provider: $('llmProvider').value.trim(),
          base_url: $('llmBaseUrl').value.trim(),
          model: $('llmModel').value.trim(),
          api_key_env: $('llmApiKeyEnv').value.trim(),
          timeout_sec: readNumber('llmTimeoutSec', 20),
          stream: $('llmStream').checked,
          temperature: readNumber('llmTemperature', 0),
          max_tokens: readOptionalInt('llmMaxTokens'),
          prompt_preset: $('promptPreset').value.trim() || 'default',
          custom_prompt_enabled: $('customPromptEnabled').checked,
          system_prompt: $('systemPrompt').value,
          user_prompt_template: $('userPromptTemplate').value,
          api_key_present: Boolean(state.data && state.data.state && state.data.state.llm && state.data.state.llm.api_key_present),
        },
        context: {
          enabled: $('contextEnabled').checked,
          max_chars: readNumber('contextMaxChars', 1200),
          capture_budget_ms: readNumber('contextCaptureBudgetMs', 1200),
        },
        hotwords: {
          enabled: $('hotwordsEnabled').checked,
          rewrite_aliases: $('rewriteAliases').checked,
          case_sensitive: $('caseSensitive').checked,
          entries: rows
            .map((row) => ({
              value: row.querySelector('.hotword-value').value.trim(),
              aliases: row.querySelector('.hotword-aliases').value.split(',').map((item) => item.trim()).filter(Boolean),
            }))
            .filter((entry) => entry.value),
        },
        hints: {
          enabled: $('hintsEnabled').checked,
          items: $('hintsInput').value.split('\n').map((item) => item.trim()).filter(Boolean),
        },
      };
    }

    function updateLiveSummary(current = state.data ? collectState() : null) {
      if (!current) return;

      const hotwordCount = (current.hotwords.entries || []).filter((entry) => entry.value).length;
      const hintCount = (current.hints.items || []).filter(Boolean).length;
      const modelLabel = normalizeInline(current.llm.model) || normalizeInline(current.llm.provider) || '未填写';
      const preset = findPromptPreset(current.llm.prompt_preset);
      const presetLabel = current.llm.custom_prompt_enabled ? '自定义' : (preset ? preset.label : current.llm.prompt_preset);

      $('rewriteSummary').textContent = current.llm.enabled ? presetLabel : '关闭';
      $('modelSummary').textContent = modelLabel;
      $('contextSummary').textContent = current.context.enabled ? `${current.context.max_chars} 字` : '关闭';
      $('lexiconSummary').textContent = `${hotwordCount} 热词 · ${hintCount} 提示`;
      $('runSummary').textContent = '日志';

      $('inlineKeyNotice').textContent = current.llm.api_key_present ? '已保留本地 key' : 'secret 不回显';
    }

    function renderState(payload) {
      state.data = payload;
      $('configPath').textContent = payload.config_path || '-';
      $('logPath').textContent = payload.log_path || '-';

      const current = payload.state;
      const hotwordEntries = current.hotwords.entries || [];
      const hintItems = (current.hints.items || []).filter(Boolean);

      $('fullwidthToHalfwidth').checked = !!current.transforms.fullwidth_to_halfwidth;
      $('spaceAroundPunct').checked = !!current.transforms.space_around_punct;
      $('spaceBetweenCjk').checked = !!current.transforms.space_between_cjk;
      $('stripTrailingPunctuation').checked = !!current.transforms.strip_trailing_punctuation;

      $('llmEnabled').checked = !!current.llm.enabled;
      $('llmProvider').value = current.llm.provider || 'openai-compatible';
      $('llmBaseUrl').value = current.llm.base_url || '';
      $('llmModel').value = current.llm.model || '';
      $('llmApiKeyEnv').value = current.llm.api_key_env || '';
      $('llmTimeoutSec').value = current.llm.timeout_sec ?? 20.0;
      $('llmStream').checked = !!current.llm.stream;
      $('llmTemperature').value = current.llm.temperature ?? 0.0;
      $('llmMaxTokens').value = current.llm.max_tokens ?? '';
      $('promptPreset').innerHTML = '';
      (payload.prompt_presets || []).forEach((preset) => {
        const option = document.createElement('option');
        option.value = preset.key;
        option.textContent = preset.label;
        $('promptPreset').appendChild(option);
      });
      $('promptPreset').value = current.llm.prompt_preset || 'default';
      $('customPromptEnabled').checked = !!current.llm.custom_prompt_enabled;
      $('systemPrompt').value = current.llm.system_prompt || '';
      $('userPromptTemplate').value = current.llm.user_prompt_template || '';
      syncPromptEditors();

      $('contextEnabled').checked = !!current.context.enabled;
      $('contextMaxChars').value = current.context.max_chars ?? 1200;
      $('contextCaptureBudgetMs').value = current.context.capture_budget_ms ?? 1200;

      $('hotwordsEnabled').checked = !!current.hotwords.enabled;
      $('rewriteAliases').checked = !!current.hotwords.rewrite_aliases;
      $('caseSensitive').checked = !!current.hotwords.case_sensitive;

      $('hintsEnabled').checked = !!current.hints.enabled;
      $('hintsInput').value = hintItems.join('\n');

      const list = $('hotwordList');
      list.innerHTML = '';
      if (hotwordEntries.length) {
        hotwordEntries.forEach((entry) => list.appendChild(hotwordRow(entry)));
      } else {
        list.appendChild(hotwordRow());
      }

      $('logPreview').textContent = payload.logs || '还没有日志。';
      updateLiveSummary(current);
    }

    function formatContextPreview(context) {
      if (!context) {
        $('contextMeta').textContent = '无数据';
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
      return [...meta, '', excerpt || '上下文为空。'].join('\n');
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
      if (!response.ok) throw new Error(payload.error || '保存失败');
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
        setStatus(`等待 ${Math.round(delayMs / 1000)} 秒…`);
        $('contextMeta').textContent = `等待 ${Math.round(delayMs / 1000)} 秒`;
      } else {
        setStatus('正在采集…');
      }
      const response = await fetch(`/api/context?delay_ms=${delayMs}`);
      const payload = await response.json();
      $('contextPreview').textContent = formatContextPreview(payload.context);
      setStatus('上下文已更新', true);
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
        setStatus(error.message || '采集失败');
      }
    });

    $('contextDelayedBtn').addEventListener('click', async () => {
      try {
        await refreshContext(2000);
      } catch (error) {
        setStatus(error.message || '采集失败');
      }
    });

    $('logsBtn').addEventListener('click', async () => {
      try {
        await refreshLogs();
        setStatus('日志已刷新', true);
      } catch (error) {
        setStatus(error.message || '刷新失败');
      }
    });

    $('addHotwordBtn').addEventListener('click', () => {
      const row = hotwordRow();
      $('hotwordList').appendChild(row);
      row.querySelector('.hotword-value').focus();
      updateLiveSummary();
    });

    $('promptPreset').addEventListener('change', () => {
      $('customPromptEnabled').checked = false;
      applyPromptPreset($('promptPreset').value);
      syncPromptEditors();
      updateLiveSummary();
    });

    $('customPromptEnabled').addEventListener('change', () => {
      if (!$('customPromptEnabled').checked) {
        applyPromptPreset($('promptPreset').value);
      }
      syncPromptEditors();
      updateLiveSummary();
    });

    document.querySelectorAll('.nav-item').forEach((item) => {
      item.addEventListener('click', () => switchPane(item.dataset.pane));
    });

    document.addEventListener('input', () => updateLiveSummary());
    document.addEventListener('change', () => updateLiveSummary());

    switchPane('rewrite');
    loadState().catch((error) => setStatus(error.message || '加载失败'));
    setInterval(() => {
      if (state.activePane === 'run') {
        refreshLogs().catch(() => {});
      }
    }, 5000);
  </script>
</body>
</html>
"""
