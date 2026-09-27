import pytest

from scripts import mock_analytics_dataset as producer
from scripts.mock_analytics_dataset import NoRedirects, SyntheticDataset, ingest_dataset


def test_redirects_are_not_followed_with_the_ingestion_credential():
    handler = NoRedirects()

    assert handler.redirect_request(None, None, 302, "Found", {}, "https://other.test") is None


def test_ingestion_summary_distinguishes_created_events_from_duplicates(monkeypatch):
    profile = {"uuid": "profile-1"}
    behavior = ({"uuid": "event-1"}, {"uuid": "event-2"})
    dataset = SyntheticDataset((profile,), behavior)

    def accepted(_base_url, _key, path, payload, _timeout):
        if path == "/api/v1/capture/":
            return {"status": "accepted", "duplicate": False}
        return {
            "results": [
                {"status": "accepted", "duplicate": False},
                {"status": "accepted", "duplicate": True},
            ]
        }

    monkeypatch.setattr(producer, "_request", accepted)

    assert ingest_dataset(dataset, "http://example.test", "secret", 1) == {
        "created": 2,
        "duplicates": 1,
        "rejected": 0,
    }


def test_ingestion_fails_when_a_valid_behavioral_event_is_rejected(monkeypatch):
    dataset = SyntheticDataset((), ({"uuid": "event-1"},))

    monkeypatch.setattr(
        producer,
        "_request",
        lambda *_args: {"results": [{"status": "rejected", "code": "INVALID_EVENT"}]},
    )

    with pytest.raises(RuntimeError, match="1 valid synthetic events were rejected"):
        ingest_dataset(dataset, "http://example.test", "secret", 1)


def test_ingestion_fails_when_bulk_response_omits_results(monkeypatch):
    dataset = SyntheticDataset((), ({"uuid": "event-1"}, {"uuid": "event-2"}))

    monkeypatch.setattr(
        producer,
        "_request",
        lambda *_args: {"results": [{"status": "accepted", "duplicate": False}]},
    )

    with pytest.raises(RuntimeError, match="expected 2 bulk results, received 1"):
        ingest_dataset(dataset, "http://example.test", "secret", 1)
