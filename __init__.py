"""HAI (https://hai.hcloud.ltd) 画像生成バックエンド。

HAI の OpenAI 互換 ``/v1/images/generations`` 表面で ``krea-2-medium-turbo``
(Krea 2 Medium Turbo, 自社DC) を利用します。出力は base64 (``b64_json``) で
同期返却され、``$HERMES_HOME/cache/images/`` に保存されます。

このプラグインは Hermes の公開プラグイン API (``agent.image_gen_provider``)
と ``openai`` SDK にのみ依存します。Hermes 内部の非公開モジュールには触れず、
バージョンアップに強くしています。

設定
----
``config.yaml``::

    image_gen:
      provider: hai
      model: krea-2-medium-turbo        # 任意 (既定)
      hai:
        base_url: https://hai-api.hcloud.ltd/v1   # 任意 (既定)

認証キーは **環境変数 ``HAI_API_KEY``** から取得します (有料キー ``hai_...``。
トライアルキーは画像生成エンドポイントで使えません)。``.env`` への自動書き込み
は行わず、環境変数から注入する想定です。

ドキュメント: https://hai.hcloud.ltd/docs
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from typing import Any, Dict, List, Optional, Tuple

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

# --- 定数 -------------------------------------------------------------------
DEFAULT_BASE_URL = "https://hai-api.hcloud.ltd/v1"
API_KEY_ENV = "HAI_API_KEY"
BASE_URL_ENV = "HAI_IMAGE_BASE_URL"
REQUEST_TIMEOUT_SECONDS = 180.0  # 生成は数十秒〜1分程度かかる

# HAI 画像モデルカタログ (GET /v1/images/models 参照)。ID は /v1/images/generations
# にそのまま送られる。1 リクエスト = 1 枚 (n > 1 は非対応)。
_MODELS: Dict[str, Dict[str, Any]] = {
    "krea-2-medium-turbo": {
        "display": "Krea 2 Medium Turbo (HAI)",
        "speed": "~30-60s",
        "strengths": "1K PNG 生成、1 枚の参照画像 (image-to-image) / seed 対応。1K 1 枚 約 ¥3",
        "price": "1K 1 枚 約 ¥3 (4,175 image tokens @ ¥720/M)",
    },
}
DEFAULT_MODEL = "krea-2-medium-turbo"

# Hermes のセマンティックアスペクト → HAI/OpenAI の ``size``。
# HAI は size (約分した比が対応表にあるもの) か aspect_ratio の両方を受理するが、
# OpenAI 標準の size を使う。3:2 / 1:1 / 2:3 はすべて HAI 対応。
_SIZE_BY_ASPECT: Dict[str, str] = {
    "landscape": "1536x1024",   # 3:2
    "square": "1024x1024",      # 1:1
    "portrait": "1024x1536",    # 2:3
}


def _size_for(aspect: str) -> str:
    return _SIZE_BY_ASPECT.get(aspect, _SIZE_BY_ASPECT["square"])


def _openai_importable() -> bool:
    try:
        import openai  # noqa: F401

        return True
    except ImportError:
        return False


def _resolve_api_key() -> Optional[str]:
    """``HAI_API_KEY`` を取得 (環境変数)。無ければ None。"""
    return get_secret(API_KEY_ENV)


def _resolve_base_url() -> str:
    """``image_gen.hai.base_url`` → 環境変数 ``HAI_IMAGE_BASE_URL`` → 既定 URL。"""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else {}
        hai = section.get("hai") if isinstance(section, dict) else {}
        if isinstance(hai, dict):
            val = hai.get("base_url")
            if isinstance(val, str) and val.strip():
                return val.strip().rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    env = os.environ.get(BASE_URL_ENV, "").strip()
    return env or DEFAULT_BASE_URL


def _resolve_model(caller_model: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """``model`` kwarg → 環境変数 ``HAI_IMAGE_MODEL`` → ``image_gen.hai.model`` →
    ``image_gen.model`` (うちの ID のとき) → 既定。未知の ID は既定にフォールバック。"""
    candidates: List[Optional[str]] = []
    if isinstance(caller_model, str) and caller_model.strip():
        candidates.append(caller_model.strip())
    env = os.environ.get("HAI_IMAGE_MODEL", "").strip()
    if env:
        candidates.append(env)
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else {}
        hai = section.get("hai") if isinstance(section, dict) else {}
        if isinstance(hai, dict):
            val = hai.get("model")
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
        if isinstance(section, dict):
            val = section.get("model")
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
    except Exception:  # noqa: BLE001
        pass
    for candidate in candidates:
        if candidate in _MODELS:
            return candidate, _MODELS[candidate]
    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _to_reference_url(ref: str) -> Tuple[Optional[str], Optional[str]]:
    """参照画像を HAI が受理する URL に変換 (https URL / data URL はそのまま、
    ローカルパスは data URL 化)。``(url, error)`` を返す。"""
    ref = (ref or "").strip()
    if not ref:
        return None, "参照画像が空です"
    if ref.startswith(("http://", "https://", "data:")):
        return ref, None
    try:
        with open(ref, "rb") as fh:
            data = fh.read()
    except Exception as exc:  # noqa: BLE001
        return None, f"参照画像 {ref} を読み込めませんでした: {exc}"
    ext = os.path.splitext(ref)[1].lstrip(".").lower()
    mime = (mimetypes.guess_type(f".{ext}")[0] if ext else None) or "image/png"
    if not mime.startswith("image/"):
        mime = "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}", None


class HaiImageGenProvider(ImageGenProvider):
    """HAI ``/v1/images/generations`` バックエンド (krea-2-medium-turbo)。"""

    @property
    def name(self) -> str:
        # 設定キー (image_gen.provider: hai) に一致する識別子。
        return "hai"

    def is_available(self) -> bool:
        # 呼び出し時はキー + SDK のみ確認 (ネットワーク接続しない)。
        return bool(_resolve_api_key()) and _openai_importable()

    def capabilities(self) -> Dict[str, Any]:
        # 参照画像 1 枚まで対応 (image-to-image)。
        return {"modalities": ["text", "image"], "max_reference_images": 1}

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": meta["price"],
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        # キーは環境変数注入が前提なので、プロンプト付きの env_vars は出さない。
        return {
            "name": "HAI",
            "badge": "paid",
            "tag": "Krea 2 Medium Turbo via HAI (hai-api.hcloud.ltd) — 1K PNG、image-to-image。HAI_API_KEY (環境変数)",
            "env_vars": [],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        model_id, _meta = _resolve_model(kwargs.get("model"))

        if not prompt:
            return error_response(
                error="プロンプトは必須で、空ではない文字列である必要があります。",
                error_type="invalid_argument", provider="hai", model=model_id,
                aspect_ratio=aspect,
            )

        api_key = _resolve_api_key()
        if not api_key:
            return error_response(
                error=f"{API_KEY_ENV} が設定されていません。環境変数 HAI_API_KEY に "
                      f"有料キー (hai_...) を設定してください (https://hai.hcloud.ltd)。",
                error_type="auth_required", provider="hai", model=model_id,
                prompt=prompt, aspect_ratio=aspect,
            )

        try:
            import openai
        except ImportError:
            return error_response(
                error="openai パッケージが必要です (pip install openai)。",
                error_type="missing_dependency", provider="hai", model=model_id,
                prompt=prompt, aspect_ratio=aspect,
            )

        # 参照画像: 主 ``image_url`` を最優先、次は reference_image_urls の先頭。
        # HAI は 1 枚まで。ローカルパス → data URL。
        ref_sources: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            ref_sources.append(image_url.strip())
        if reference_image_urls:
            for ref in reference_image_urls:
                if isinstance(ref, str) and ref.strip():
                    ref_sources.append(ref.strip())
        if len(ref_sources) > 1:
            ref_sources = ref_sources[:1]

        request: Dict[str, Any] = dict(
            model=model_id, prompt=prompt, size=_size_for(aspect), n=1,
        )
        if ref_sources:
            url, rerr = _to_reference_url(ref_sources[0])
            if rerr:
                return error_response(
                    error=f"参照画像の処理に失敗しました: {rerr}",
                    error_type="invalid_argument", provider="hai", model=model_id,
                    prompt=prompt, aspect_ratio=aspect,
                )
            # HAI 固有フィールド ``input_references`` を JSON body のトップレベルに
            # 足す (OpenAI SDK の extra_body がマージする)。
            request["extra_body"] = {
                "input_references": [
                    {"type": "image_url", "image_url": {"url": url}},
                ]
            }

        client = openai.OpenAI(
            api_key=api_key, base_url=_resolve_base_url(), timeout=REQUEST_TIMEOUT_SECONDS,
        )
        try:
            response = client.images.generate(**request)
        except Exception as exc:  # noqa: BLE001
            logger.debug("HAI image generation failed", exc_info=True)
            status = int(getattr(exc, "status_code", 0) or 0)
            detail = str(getattr(exc, "message", "") or "")
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                err = body.get("error")
                if isinstance(err, dict) and err.get("message"):
                    detail = str(err["message"])
                elif isinstance(err, str):
                    detail = err
            suffix = f" (HTTP {status})" if status else ""
            detail = f": {detail}" if detail else ""
            if status in (401, 403):
                return error_response(
                    error=f"HAI が API キーを拒否しました{suffix}{detail}。"
                          f"トライアルキーは画像生成で使えません。有料キー (hai_...) を "
                          f"HAI_API_KEY に設定してください。",
                    error_type="auth_required", provider="hai", model=model_id,
                    prompt=prompt, aspect_ratio=aspect,
                )
            return error_response(
                error=f"HAI 画像生成に失敗しました{suffix}{detail}",
                error_type="api_error", provider="hai", model=model_id,
                prompt=prompt, aspect_ratio=aspect,
            )

        data = getattr(response, "data", None) or []
        if not data:
            return error_response(
                error="HAI のレスポンスに画像データがありません。",
                error_type="empty_response", provider="hai", model=model_id,
                prompt=prompt, aspect_ratio=aspect,
            )
        first = data[0]
        b64 = getattr(first, "b64_json", None)
        url = getattr(first, "url", None)
        if not b64 and not url:
            return error_response(
                error="HAI のレスポンスに b64_json も URL もありません。",
                error_type="empty_response", provider="hai", model=model_id,
                prompt=prompt, aspect_ratio=aspect,
            )

        image_ref: Optional[str] = None
        try:
            if b64:
                image_ref = str(save_b64_image(b64, prefix="hai", extension="png"))
            elif url:
                image_ref = str(save_url_image(url, prefix="hai"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("HAI image cache save failed: %s", exc)
            if url:
                # キャッシュ失敗時は URL をそのまま返す (best effort)。
                image_ref = url
            else:
                return error_response(
                    error=f"画像のキャッシュ保存に失敗しました: {exc}",
                    error_type="io_error", provider="hai", model=model_id,
                    prompt=prompt, aspect_ratio=aspect,
                )
        if not image_ref:
            return error_response(
                error="画像の保存に失敗しました。",
                error_type="io_error", provider="hai", model=model_id,
                prompt=prompt, aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {"size": _size_for(aspect)}
        usage = getattr(response, "usage", None)
        if usage is not None and getattr(usage, "completion_tokens", None):
            try:
                extra["image_tokens"] = int(usage.completion_tokens)
            except Exception:  # noqa: BLE001
                pass
        if getattr(first, "revised_prompt", None):
            extra["revised_prompt"] = first.revised_prompt

        return success_response(
            image=image_ref, model=model_id, prompt=prompt, aspect_ratio=aspect,
            provider="hai", modality="image" if ref_sources else "text", extra=extra,
        )


def register(ctx) -> None:
    """プラグインエントリポイント — HAI バックエンドを image_gen レジストリに登録。"""
    ctx.register_image_gen_provider(HaiImageGenProvider())
