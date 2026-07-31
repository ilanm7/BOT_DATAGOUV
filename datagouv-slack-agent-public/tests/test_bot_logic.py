"""Tests unitaires sur la logique pure du bot Slack."""


from app.slack.bot import (
    _clean_answer,
    _escape_slack_mrkdwn,
    _extract_dataset_ids,
    _sanitize_user_input,
)


class TestExtractDatasetIds:
    def test_simple_slug(self):
        text = "https://www.data.gouv.fr/fr/datasets/velos-a-assistance-electrique-en-libre-service-levelo-sur-marseille"
        assert _extract_dataset_ids(text) == [
            "velos-a-assistance-electrique-en-libre-service-levelo-sur-marseille"
        ]

    def test_with_resource(self):
        text = "https://www.data.gouv.fr/fr/datasets/r/619e3ea5f653cb96ca6a4d4c/liste-autoecoles.csv"
        assert _extract_dataset_ids(text) == ["619e3ea5f653cb96ca6a4d4c"]

    def test_multiple(self):
        text = (
            "https://www.data.gouv.fr/fr/datasets/dataset-a "
            "et aussi https://www.data.gouv.fr/fr/datasets/dataset-b"
        )
        assert _extract_dataset_ids(text) == ["dataset-a", "dataset-b"]

    def test_no_match(self):
        assert _extract_dataset_ids("rien ici") == []


class TestSanitizeUserInput:
    def test_normal_text(self):
        assert _sanitize_user_input("Bonjour") == "Bonjour"

    def test_null_bytes(self):
        assert _sanitize_user_input("a\x00b") == "ab"

    def test_control_chars(self):
        assert _sanitize_user_input("a\x01b\x02c") == "abc"

    def test_preserved_length(self):
        # _sanitize ne tronque pas, il nettoie
        long_text = "a" * 15_000
        result = _sanitize_user_input(long_text)
        assert len(result) == 15_000


class TestCleanAnswer:
    def test_bold_conversion(self):
        text = "Ce fichier **ne permet pas** l'analyse"
        assert _clean_answer(text) == "Ce fichier *ne permet pas* l'analyse"

    def test_list_conversion(self):
        text = "* item 1\n* item 2"
        assert _clean_answer(text) == "• item 1\n• item 2"

    def test_code_block_preserved(self):
        text = "```python\nprint(1)\n```"
        assert "```python" in _clean_answer(text)

    def test_refs_line_preserved(self):
        text = "Réponse\n_refs: dataset_1=abc123"
        assert "_refs: dataset_1=abc123" in _clean_answer(text)


class TestEscapeSlackMrkdwn:
    def test_no_special_chars(self):
        assert _escape_slack_mrkdwn("normal") == "normal"
