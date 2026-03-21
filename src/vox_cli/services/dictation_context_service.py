from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any

from ..config import VoxConfig

_CHROMIUM_APPS = {
    'google chrome',
    'google chrome beta',
    'chromium',
    'brave browser',
    'microsoft edge',
    'arc',
}

_TERMINAL_NOISE_PREFIXES = (
    'last login:',
    '❯',
    '$ ',
    '# ',
    '›',
    '•',
    '└',
    '│',
)
_TERMINAL_NOISE_PATTERNS = (
    re.compile(r'^[\s\-\u2500-\u257f]+$'),
    re.compile(r'^\w[\w./-]*\s+·\s+.+$'),
    re.compile(r'^(read|explored|recommending|improve documentation)\b', re.IGNORECASE),
)
_PAGE_NOISE_PATTERNS = (
    re.compile(r'^\[(?:session-server|vox-dictation|dictation)\]'),
    re.compile(r'^\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b'),
    re.compile(r'^\{"ts":\s*"'),
    re.compile(r'^\w+Error[:\s]'),
    re.compile(r'^\s*warnings?\.warn\('),
)


@dataclass
class DictationContext:
    source: str
    app_name: str | None = None
    window_title: str | None = None
    element_role: str | None = None
    element_title: str | None = None
    selected_text: str | None = None
    page_url: str | None = None
    context_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in (None, '')}


@dataclass
class DictationContextSnapshot:
    context: DictationContext | None
    capture_ms: int
    error: str | None = None


def capture_dictation_context(config: VoxConfig, *, force: bool = False) -> DictationContext | None:
    if not force and not config.dictation.context.enabled:
        return None

    max_chars = max(0, int(config.dictation.context.max_chars))
    if max_chars <= 0:
        return None

    app_name = _frontmost_app_name()
    if not app_name:
        return None

    app_key = app_name.casefold()
    if app_key == 'ghostty':
        return _capture_ghostty_context(app_name, max_chars)
    if app_key in _CHROMIUM_APPS:
        try:
            context = _capture_chromium_context(app_name, max_chars)
            if context is not None:
                return context
        except Exception:
            pass
    return _capture_generic_ax_context(app_name, max_chars)


def capture_dictation_context_snapshot(
    config: VoxConfig,
    *,
    force: bool = False,
) -> DictationContextSnapshot:
    started_at = time.perf_counter()
    try:
        context = capture_dictation_context(config, force=force)
        return DictationContextSnapshot(
            context=context,
            capture_ms=int((time.perf_counter() - started_at) * 1000),
        )
    except Exception as error:
        return DictationContextSnapshot(
            context=None,
            capture_ms=int((time.perf_counter() - started_at) * 1000),
            error=str(error),
        )


def _run_osascript(lines: list[str], *, language: str | None = None) -> str:
    command = ['osascript']
    if language:
        command.extend(['-l', language])
    for line in lines:
        command.extend(['-e', line])

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f'exit code {result.returncode}'
        raise RuntimeError(detail)
    return result.stdout.strip()


def _frontmost_app_name() -> str | None:
    output = _run_osascript(
        [
            'tell application "System Events"',
            'return name of first application process whose frontmost is true',
            'end tell',
        ]
    )
    return _clean_optional_text(output)


def _capture_ghostty_context(app_name: str, max_chars: int) -> DictationContext | None:
    context = _capture_generic_ax_context(app_name, max_chars, source='ghostty')
    if context is None:
        return None
    context.context_text = _sanitize_terminal_context(context.context_text, max_chars)
    return context if context.to_dict() else None


def _capture_generic_ax_context(
    app_name: str,
    max_chars: int,
    *,
    source: str = 'ax',
) -> DictationContext | None:
    window_title = _read_window_title(app_name)
    element_role = _clean_optional_text(_read_focused_attribute(app_name, 'AXRole'))
    element_title = _clean_optional_text(_read_focused_attribute(app_name, 'AXTitle'))
    selected_text = _clean_optional_text(_read_focused_attribute(app_name, 'AXSelectedText'))
    element_value = _clean_optional_text(_read_focused_attribute(app_name, 'AXValue'))
    context_text = _truncate_tail(element_value, max_chars)

    context = DictationContext(
        source=source,
        app_name=app_name,
        window_title=window_title,
        element_role=element_role,
        element_title=element_title,
        selected_text=_truncate_tail(selected_text, max_chars),
        context_text=context_text,
    )
    return context if context.to_dict() else None


def _capture_chromium_context(app_name: str, max_chars: int) -> DictationContext | None:
    tab_output = _run_osascript(
        [
            f'tell application "{_escape_applescript_string(app_name)}"',
            'set t to active tab of front window',
            'return (title of t) & linefeed & (URL of t)',
            'end tell',
        ]
    )
    tab_lines = tab_output.splitlines()
    window_title = _clean_optional_text(tab_lines[0] if tab_lines else '')
    page_url = _clean_optional_text('\n'.join(tab_lines[1:]) if len(tab_lines) > 1 else '')

    js = (
        "(function(){"
        "const active=document.activeElement;"
        "const selection=(window.getSelection&&window.getSelection().toString())||'';"
        "const ignoredSelector='[data-dictation-ignore=\"true\"]';"
        "const isEditable=!!active&&(active.isContentEditable||"
        "(active.tagName==='TEXTAREA')||"
        "(active.tagName==='INPUT'&&/^(?:text|search|email|url|tel|number|password)?$/i.test(active.type||'text')));"
        "const activeValue=active&&typeof active.value==='string'?active.value:"
        "(active&&typeof active.innerText==='string'?active.innerText:'');"
        "let nearbyNode=active;"
        "let nearbyText='';"
        "while(nearbyNode&&nearbyNode!==document.body){"
        "if(nearbyNode.matches&&nearbyNode.matches(ignoredSelector)){nearbyNode=nearbyNode.parentElement;continue;}"
        "const candidate=typeof nearbyNode.innerText==='string'?nearbyNode.innerText:'';"
        "if(candidate.trim().length>=120){nearbyText=candidate;break;}"
        "nearbyNode=nearbyNode.parentElement;"
        "}"
        "const bodyClone=document.body?document.body.cloneNode(true):null;"
        "if(bodyClone){bodyClone.querySelectorAll(ignoredSelector).forEach((node)=>node.remove());}"
        "const mainNode=bodyClone&&bodyClone.querySelector('main, article, [role=\"main\"], [data-dictation-main=\"true\"]');"
        "const mainText=mainNode&&typeof mainNode.innerText==='string'?mainNode.innerText:'';"
        "const bodyText=bodyClone&&bodyClone.innerText?bodyClone.innerText:'';"
        "return JSON.stringify({"
        "title:document.title||'',"
        "selection:selection,"
        "isEditable:isEditable,"
        "activeValue:activeValue,"
        "nearbyText:nearbyText.slice(-4000),"
        "mainText:mainText.slice(-4000),"
        "bodyText:bodyText.slice(-4000)"
        "});"
        "})()"
    )
    dom_output = _run_osascript(
        [
            f'tell application "{_escape_applescript_string(app_name)}"',
            'set t to active tab of front window',
            f'return execute t javascript "{_escape_applescript_string(js)}"',
            'end tell',
        ]
    )
    payload = json.loads(dom_output or '{}')
    selected_text = _clean_optional_text(str(payload.get('selection') or ''))
    is_editable = bool(payload.get('isEditable'))
    active_value = _clean_optional_text(str(payload.get('activeValue') or ''))
    nearby_text = _clean_optional_text(str(payload.get('nearbyText') or ''))
    main_text = _clean_optional_text(str(payload.get('mainText') or ''))
    body_text = _clean_optional_text(str(payload.get('bodyText') or ''))
    page_context = _select_browser_context_text(
        selected_text=selected_text,
        active_value=active_value,
        nearby_text=nearby_text,
        main_text=main_text,
        body_text=body_text,
        is_editable=is_editable,
        max_chars=max_chars,
    )

    context = DictationContext(
        source='chromium',
        app_name=app_name,
        window_title=window_title or _clean_optional_text(str(payload.get('title') or '')),
        page_url=page_url,
        selected_text=_truncate_tail(selected_text, max_chars),
        context_text=page_context,
    )
    return context if context.to_dict() else None


def _read_window_title(app_name: str) -> str | None:
    return _clean_optional_text(
        _run_osascript(
            [
                'tell application "System Events"',
                f'tell application process "{_escape_applescript_string(app_name)}"',
                'try',
                'set focusedWindow to value of attribute "AXFocusedWindow"',
                'return value of attribute "AXTitle" of focusedWindow',
                'on error',
                'return ""',
                'end try',
                'end tell',
                'end tell',
            ]
        )
    )


def _read_focused_attribute(app_name: str, attribute: str) -> str:
    return _run_osascript(
        [
            'tell application "System Events"',
            f'tell application process "{_escape_applescript_string(app_name)}"',
            'try',
            'set focusedElement to value of attribute "AXFocusedUIElement"',
            f'return value of attribute "{_escape_applescript_string(attribute)}" of focusedElement',
            'on error',
            'return ""',
            'end try',
            'end tell',
            'end tell',
        ]
    )


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not cleaned or cleaned == 'missing value':
        return None
    return cleaned


def _truncate_tail(value: str | None, max_chars: int) -> str | None:
    cleaned = _clean_optional_text(value)
    if cleaned is None:
        return None
    if max_chars <= 0 or len(cleaned) <= max_chars:
        return cleaned
    return cleaned[-max_chars:]


def _sanitize_terminal_context(value: str | None, max_chars: int) -> str | None:
    cleaned = _clean_optional_text(value)
    if cleaned is None:
        return None

    lines = [_normalize_terminal_line(line) for line in cleaned.split('\n')]
    useful_lines = [line for line in lines if line and not _looks_like_terminal_noise(line)]
    if not useful_lines:
        return None

    cjk_lines = [line for line in useful_lines if _contains_cjk(line)]
    candidate_lines = cjk_lines or useful_lines

    deduped_lines: list[str] = []
    for line in candidate_lines:
        if deduped_lines and deduped_lines[-1] == line:
            continue
        deduped_lines.append(line)

    selected: list[str] = []
    budget = max_chars
    for line in reversed(deduped_lines[-8:]):
        line_cost = len(line) + (1 if selected else 0)
        if selected and line_cost > budget and len(selected) >= 2:
            break
        selected.append(line)
        budget -= line_cost
        if budget <= 0:
            break

    result = '\n'.join(reversed(selected)).strip()
    return _truncate_tail(result, max_chars)


def _select_browser_context_text(
    *,
    selected_text: str | None,
    active_value: str | None,
    nearby_text: str | None,
    main_text: str | None,
    body_text: str | None,
    is_editable: bool,
    max_chars: int,
) -> str | None:
    if selected_text:
        return _truncate_tail(selected_text, max_chars)

    candidates: list[str | None] = []
    if is_editable:
        candidates.extend([nearby_text, main_text, body_text])
    else:
        candidates.extend([nearby_text, main_text, body_text, active_value])
    if is_editable:
        candidates.append(active_value)

    for candidate in candidates:
        sanitized = _sanitize_page_context(candidate, max_chars)
        if sanitized:
            return sanitized
    return None


def _sanitize_page_context(value: str | None, max_chars: int) -> str | None:
    cleaned = _clean_optional_text(value)
    if cleaned is None:
        return None

    lines = [re.sub(r'\s+', ' ', line).strip() for line in cleaned.split('\n')]
    useful_lines = [line for line in lines if line and len(line) >= 2 and not _looks_like_page_noise(line)]
    if not useful_lines:
        return None

    deduped_lines: list[str] = []
    for line in useful_lines:
        if deduped_lines and deduped_lines[-1] == line:
            continue
        deduped_lines.append(line)

    head_lines = deduped_lines[:6]
    tail_lines = deduped_lines[-8:] if len(deduped_lines) > 6 else []
    candidate_lines: list[str] = []
    for line in [*head_lines, *tail_lines]:
        if line not in candidate_lines:
            candidate_lines.append(line)

    selected: list[str] = []
    budget = max_chars
    for line in candidate_lines:
        line_cost = len(line) + (1 if selected else 0)
        if selected and line_cost > budget and len(selected) >= 2:
            break
        selected.append(line)
        budget -= line_cost
        if budget <= 0:
            break

    result = '\n'.join(selected).strip()
    return _truncate_tail(result, max_chars)


def _looks_like_page_noise(line: str) -> bool:
    line_key = line.casefold()
    if any(pattern.match(line) for pattern in _PAGE_NOISE_PATTERNS):
        return True
    if line_key.startswith('http://127.0.0.1:') or line_key.startswith('https://127.0.0.1:'):
        return True
    if line.count('|') >= 4 and len(line) > 40:
        return True
    return False


def _normalize_terminal_line(line: str) -> str:
    collapsed = re.sub(r'\s+', ' ', line).strip()
    return collapsed


def _looks_like_terminal_noise(line: str) -> bool:
    line_key = line.casefold()
    if any(line_key.startswith(prefix) for prefix in _TERMINAL_NOISE_PREFIXES):
        return True
    if any(pattern.match(line) for pattern in _TERMINAL_NOISE_PATTERNS):
        return True
    if '/' in line and not _contains_cjk(line) and len(line) > 32:
        return True
    return False


def _contains_cjk(value: str) -> bool:
    return any('\u3400' <= char <= '\u9fff' for char in value)


def _escape_applescript_string(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"')
