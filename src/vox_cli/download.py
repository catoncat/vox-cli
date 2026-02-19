from __future__ import annotations

from pathlib import Path
import os

from .types import EndpointResult


def download_with_fallback(
    repo_id: str,
    endpoints: list[str],
    hf_cache_dir: Path,
) -> EndpointResult:
    from huggingface_hub import snapshot_download

    errors: list[str] = []

    for endpoint in endpoints:
        previous_endpoint = os.getenv('HF_ENDPOINT')
        os.environ['HF_ENDPOINT'] = endpoint
        try:
            path = snapshot_download(
                repo_id=repo_id,
                endpoint=endpoint,
                cache_dir=str(hf_cache_dir),
                resume_download=True,
            )
            return EndpointResult(endpoint=endpoint, snapshot_path=Path(path))
        except Exception as e:
            errors.append(f'{endpoint}: {e}')
        finally:
            if previous_endpoint is None:
                os.environ.pop('HF_ENDPOINT', None)
            else:
                os.environ['HF_ENDPOINT'] = previous_endpoint

    detail = '\n'.join(errors)
    raise RuntimeError(f'All endpoints failed for {repo_id}:\n{detail}')
