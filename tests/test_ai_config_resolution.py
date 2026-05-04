from api.services.config_service import ConfigService


def test_text_settings_infer_provider_and_default_model_from_base_url():
    service = ConfigService()

    settings = service.resolve_text_ai_settings({
        "TEXT_AI_BASE_URL": "https://api.deepseek.com/v1",
        "TEXT_AI_API_KEY": "text-key",
        "TEXT_AI_TIMEOUT": "45",
        "TEXT_AI_MAX_TOKENS": "1024",
    })

    assert settings == {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "text-key",
        "timeout": "45",
        "max_tokens": "1024",
    }


def test_text_settings_fall_back_to_legacy_provider_model_and_key():
    service = ConfigService()

    settings = service.resolve_text_ai_settings({
        "AI_PROVIDER": "anthropic",
        "AI_MODEL": "claude-custom",
        "ANTHROPIC_API_KEY": "legacy-key",
    })

    assert settings["provider"] == "anthropic"
    assert settings["model"] == "claude-custom"
    assert settings["api_key"] == "legacy-key"
    assert settings["base_url"] == ""


def test_image_settings_infer_provider_from_own_base_url_and_use_default_model():
    service = ConfigService()

    settings = service.resolve_image_ai_settings({
        "TEXT_AI_BASE_URL": "https://api.openai.com/v1",
        "IMAGE_UNDERSTANDING_BASE_URL": "https://api.deepseek.com/v1",
        "IMAGE_UNDERSTANDING_API_KEY": "image-key",
        "IMAGE_UNDERSTANDING_ENABLED": "true",
        "IMAGE_UNDERSTANDING_MAX_IMAGES": "4",
        "IMAGE_UNDERSTANDING_TIMEOUT": "90",
        "IMAGE_UNDERSTANDING_USE_LOCAL_FIRST": "false",
    })

    assert settings == {
        "enabled": "true",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "image-key",
        "max_images": "4",
        "timeout": "90",
        "use_local_first": "false",
    }


def test_image_settings_fall_back_to_legacy_model_when_provider_matches():
    service = ConfigService()

    settings = service.resolve_image_ai_settings({
        "AI_PROVIDER": "anthropic",
        "AI_MODEL": "claude-vision-custom",
        "ANTHROPIC_API_KEY": "legacy-key",
    })

    assert settings["provider"] == "anthropic"
    assert settings["model"] == "claude-vision-custom"
    assert settings["api_key"] == "legacy-key"


def test_image_settings_use_dashscope_ocr_default_model():
    service = ConfigService()

    settings = service.resolve_image_ai_settings({
        "IMAGE_UNDERSTANDING_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "IMAGE_UNDERSTANDING_API_KEY": "image-key",
        "IMAGE_UNDERSTANDING_ENABLED": "true",
    })

    assert settings["provider"] == "openai"
    assert settings["model"] == "qwen-vl-ocr-latest"
    assert settings["api_key"] == "image-key"
