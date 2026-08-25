from unittest.mock import patch

import pytest

from faffmonkey.runtime.redaction import redact


class TestTokenPatterns:
    """Each token shape is redacted and leaves a [REDACTED] marker."""

    @pytest.mark.parametrize("text,forbidden", [
        ("my key is sk-abcdefghijklmnopqrstuvwxyz1234", "sk-"),
        ("token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl", "ghp_"),
        ("use xoxb-123-456-abc for slack", "xoxb-"),
        ("key=AIzaSyB_abcdefghijklmnopqrstuvwxyz12345", "AIza"),
        ("venice key: vn_abcdefghijklmnopqrstuv", "vn_"),
        ("auth: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123def456_ghi", "eyJ"),
        ("key=sk-proj-abc123def456ghi789jkl012", "sk-proj"),
        ("key=sk-ant-api03-abcdefghijklmnopqrst", "sk-ant"),
        ("aws_key=AKIAIOSFODNN7EXAMPLE", "AKIA"),
        ("bot=123456789:AAGlR9b_T4pKQXV5D3Xj3GzPi4fWm_abcde", ":AA"),
        ("bot=123456789:FnR9b_T4pKQXV5D3Xj3GzPi4fWm_abcdefgh", "FnR9b"),
        ("gho_" + "A" * 36, "gho_"),
        ("ghu_" + "B" * 36, "ghu_"),
        ("ghs_" + "C" * 36, "ghs_"),
        ("ghr_" + "D" * 36, "ghr_"),
        ("xoxp-123-456-789-abcdef", "xoxp-"),
        ("xoxb-111-222-abc", "xoxb-"),
        ("xoxa-111-222-abc", "xoxa-"),
        ("xoxr-111-222-abc", "xoxr-"),
        ("sk_live_" + "a" * 24, "sk_live_"),
        ("sk_test_" + "b" * 24, "sk_test_"),
        ("pk_live_" + "c" * 24, "pk_live_"),
        ("rk_test_" + "d" * 24, "rk_test_"),
        ("npm_" + "A" * 36, "npm_"),
        ("ASIAIOSTEMPKEY12EXAM", "ASIA"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIBog...\n-----END RSA PRIVATE KEY-----", "PRIVATE KEY"),
        ("-----BEGIN EC PRIVATE KEY-----\ndata\n-----END EC PRIVATE KEY-----", "PRIVATE KEY"),
        ("-----BEGIN PRIVATE KEY-----\ndata\n-----END PRIVATE KEY-----", "PRIVATE KEY"),
        ("s​k-abcdefghijklmnopqrstuvwxyz1234", "sk-"),
        ("key=sk-proj-" + "A" * 10 + "\n" + "B" * 20, "sk-proj"),
        ("sk-" + "a" * 10 + "\t" + "b" * 15, "sk-"),
        ("sk-proj-" + "A" * 8 + " " + "B" * 8 + " " + "C" * 8, "sk-proj"),
        ("glpat-" + "A" * 20, "glpat-"),
        ("glrt-" + "B" * 20, "glrt-"),
        ("glptt-" + "C" * 20, "glptt-"),
        ("glcbt-" + "D" * 20, "glcbt-"),
        ("hf_" + "A" * 30, "hf_"),
        ("xoxs-" + "1" * 40, "xoxs-"),
        ("xoxe-" + "2" * 40, "xoxe-"),
        ("sid=AC" + "a" * 32, "ACa"),
        ("auth=SK" + "0" * 32, "SK0"),
        ("SG." + "A" * 22 + "." + "B" * 43, "SG."),
        ("key-" + "a" * 32, "key-"),
        ("dop_v1_" + "a" * 64, "dop_v1_"),
        ("HRKU-" + "A" * 40, "HRKU-"),
        ("token=sbp_" + "a" * 30, "sbp_"),
        ("key=sb_secret_" + "A" * 25, "sb_secret_"),
    ])
    def test_token_redacted(self, text, forbidden):
        result = redact(text)
        assert forbidden not in result
        assert "[REDACTED]" in result


class TestRedactionPatterns:






    def test_bearer_token_with_authorization_header(self):
        token = "a" * 40
        text = f"Authorization: Bearer {token}"
        assert "Bearer " not in redact(text)
        assert "[REDACTED]" in redact(text)

    def test_bearer_token_long(self):
        token = "abcdefghijklmnopqrstuvwxyz01234567890ABCDE"
        text = f"header: Bearer {token}"
        assert "Bearer " not in redact(text)
        assert "[REDACTED]" in redact(text)

    def test_bearer_short_not_matched(self):
        text = "Bearer abcdefghijklmnopqrstuvwxyz"
        assert redact(text) == text

    def test_clean_passthrough(self):
        text = "Hello, this is a normal message with no secrets."
        assert redact(text) == text

    def test_multiple_secrets_in_one_string(self):
        text = "key1=sk-aaaabbbbccccddddeeeefffff key2=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl"
        result = redact(text)
        assert "sk-" not in result
        assert "ghp_" not in result
        assert result.count("[REDACTED]") == 2






    def test_short_sk_not_matched(self):
        text = "sk-short"
        assert redact(text) == text

    def test_preserves_surrounding_text(self):
        text = "before sk-abcdefghijklmnopqrstuvwxyz1234 after"
        result = redact(text)
        assert result.startswith("before ")
        assert result.endswith(" after")


class TestGitHubPatterns:
    def test_github_fine_grained_pat(self):
        token = "github_pat_" + "A" * 22 + "_" + "B" * 59
        text = f"token={token}"
        assert "github_pat_" not in redact(text)
        assert "[REDACTED]" in redact(text)

    def test_github_fine_grained_pat_full_token_with_underscore(self):
        token = "github_pat_" + "A" * 22 + "_" + "B" * 59
        result = redact(token)
        assert result == "[REDACTED]"


class TestNpmPattern:

    def test_npm_short_not_matched(self):
        text = "npm_abc"
        assert redact(text) == text


class TestInvisibleCharEvasion:

    def test_newline_wrapped_key_redacted_without_reflowing_text(self):
        """Both halves go, the line break stays.

        This asserted "\\n" not in result, which passed only because redact()
        collapsed every whitespace run in the whole message. That flattened
        code, logs and tables in every outbound reply for a pass that usually
        matched nothing. The secret is what must disappear, not the layout.
        """
        text = "sk-\nabcdefghijklmnopqrstuvwxyz1234"
        result = redact(text)
        assert "abcdefghijklmnopqrstuvwxyz1234" not in result
        assert "[REDACTED]" in result
        assert "\n" in result

    def test_formatting_survives_redaction(self):
        text = "line one\n\n    indented\nline three"
        assert redact(text) == text

    def test_soft_hyphen_in_key(self):
        text = "sk­-abcdefghijklmnopqrstuvwxyz1234"
        result = redact(text)
        assert "[REDACTED]" in result

    def test_clean_text_unchanged_after_strip(self):
        text = "Hello, this is a normal message."
        assert redact(text) == text


class TestWhitespaceWrappedEvasion:



    def test_non_secret_not_false_positive(self):
        text = "this is a normal sentence with spaces"
        assert redact(text) == text


class TestGitLabPatterns:




    def test_gitlab_short_not_matched(self):
        text = "glpat-short"
        assert redact(text) == text


class TestHuggingFacePattern:

    def test_huggingface_short_not_matched(self):
        text = "hf_short"
        assert redact(text) == text


class TestUnicodeBypass:
    def test_sk_with_unicode_hyphen(self):
        text = "sk‐ant‐abcdefghijklmnopqrstuvwxyz1234"
        result = redact(text)
        assert "[REDACTED]" in result

    def test_ghp_with_fullwidth_underscore(self):
        text = "ghp＿" + "A" * 36
        result = redact(text)
        assert "[REDACTED]" in result

    def test_en_dash_in_gitlab_pat(self):
        text = "glpat–" + "A" * 20
        result = redact(text)
        assert "[REDACTED]" in result

    def test_fullwidth_hyphen_in_slack(self):
        text = "xoxb－123-456-abc"
        result = redact(text)
        assert "[REDACTED]" in result

    def test_clean_text_unaffected(self):
        text = "Hello, this is a normal message."
        assert redact(text) == text

    def test_whitespace_wrapped_with_unicode_hyphen(self):
        text = "sk‐proj‐" + "A" * 10 + "\n" + "B" * 15
        result = redact(text)
        assert "[REDACTED]" in result


class TestNewProviderPatterns:






    def test_twilio_sid_short_not_matched(self):
        text = "AC" + "a" * 31
        assert redact(text) == text

    def test_mailgun_uppercase_not_matched(self):
        text = "key-" + "A" * 32
        assert redact(text) == text


class TestValueBasedRedaction:
    def _redact_with_env(self, env: dict[str, str], text: str) -> str:
        with patch.dict("os.environ", env, clear=True):
            return redact(text)

    def test_bespoke_api_key_redacted_by_value(self):
        secret = "aqicn_bespoke_token_xyz987"
        result = self._redact_with_env(
            {"AQICN_API_KEY": secret},
            f"Air quality data fetched with {secret} from API",
        )
        assert secret not in result
        assert "[REDACTED]" in result

    def test_short_env_value_not_redacted(self):
        result = self._redact_with_env(
            {"TZ": "Europe/London"},
            "Timezone is Europe/London",
        )
        assert result == "Timezone is Europe/London"

    def test_long_non_secret_name_not_redacted(self):
        long_val = "a" * 50
        result = self._redact_with_env(
            {"MY_CUSTOM_SETTING": long_val},
            f"Value is {long_val} here",
        )
        assert long_val in result

    def test_pattern_redaction_still_works(self):
        result = self._redact_with_env(
            {},
            "my key is sk-abcdefghijklmnopqrstuvwxyz1234",
        )
        assert "sk-" not in result
        assert "[REDACTED]" in result

    def test_token_suffix_matched(self):
        secret = "mygateway12345678"
        result = self._redact_with_env(
            {"GATEWAY_TOKEN": secret},
            f"using {secret} for auth",
        )
        assert secret not in result

    def test_password_suffix_matched(self):
        secret = "supersecretpass99"
        result = self._redact_with_env(
            {"DB_PASSWORD": secret},
            f"connecting with {secret}",
        )
        assert secret not in result

    def test_key_suffix_matched(self):
        secret = "nvidia_custom_key_value"
        result = self._redact_with_env(
            {"NVIDIA_KEY": secret},
            f"header: {secret}",
        )
        assert secret not in result

    def test_database_url_password_extracted(self):
        result = self._redact_with_env(
            {"DATABASE_URL": "postgres://user:SuperSecretPw123@db.host/app"},
            "password is SuperSecretPw123 in the logs",
        )
        assert "SuperSecretPw123" not in result
        assert "[REDACTED]" in result

    def test_redis_url_password_extracted(self):
        result = self._redact_with_env(
            {"REDIS_URL": "redis://default:longredispassword@redis.host:6379/0"},
            "connecting with longredispassword",
        )
        assert "longredispassword" not in result
        assert "[REDACTED]" in result

    def test_url_short_password_not_extracted(self):
        result = self._redact_with_env(
            {"DATABASE_URL": "postgres://user:short@db.host/app"},
            "password is short in the logs",
        )
        assert "short" in result

    def test_values_fresh_after_env_change(self):
        with patch.dict("os.environ", {"MY_API_KEY": "first_secret_value"}, clear=True):
            result1 = redact("first_secret_value appears")
            assert "first_secret_value" not in result1
        with patch.dict("os.environ", {"MY_API_KEY": "second_secret_val"}, clear=True):
            result2 = redact("second_secret_val appears")
            assert "second_secret_val" not in result2
            assert "first_secret_value" in redact("first_secret_value appears differently")


class TestUrlCredentialRedaction:
    def test_postgres_url_password_redacted(self):
        text = "DATABASE_URL=postgres://user:SuperSecretPw123@db.host/app"
        result = redact(text)
        assert "SuperSecretPw123" not in result
        assert "://user:" in result
        assert "@db.host/app" in result
        assert "[REDACTED]" in result

    def test_redis_url_password_redacted(self):
        text = "redis://default:r3d1s_p4ss@redis.host:6379/0"
        result = redact(text)
        assert "r3d1s_p4ss" not in result
        assert "://default:" in result
        assert "@redis.host" in result

    def test_mysql_url_password_redacted(self):
        text = "mysql://root:hunter2@localhost:3306/mydb"
        result = redact(text)
        assert "hunter2" not in result
        assert "://root:" in result
        assert "@localhost" in result

    def test_url_without_credentials_unchanged(self):
        text = "https://example.com/path?query=1"
        assert redact(text) == text

    def test_url_with_user_only_unchanged(self):
        text = "ftp://anonymous@files.example.com/pub"
        assert redact(text) == text


class TestSupabasePatterns:


    def test_sbp_short_not_matched(self):
        text = "sbp_short"
        assert redact(text) == text

    def test_sb_secret_short_not_matched(self):
        text = "sb_secret_short"
        assert redact(text) == text
