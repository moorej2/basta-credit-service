import signal
from threading import Event
from unittest.mock import Mock

from credit_service import dispatch
from credit_service.config import Settings


def test_dispatcher_survives_publish_error_then_stops_on_signal(monkeypatch):
    repo = Mock()
    stopped = Event()
    handlers = {}
    monkeypatch.setattr(dispatch, "CreditRepository", Mock(return_value=repo))
    monkeypatch.setattr(dispatch, "get_settings", lambda: Settings())
    monkeypatch.setattr(dispatch, "Event", lambda: stopped)
    monkeypatch.setattr(
        dispatch.signal, "signal", lambda sig, handler: handlers.update({sig: handler})
    )
    attempts = []

    def one(_):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("broker down")
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return True

    monkeypatch.setattr(dispatch, "dispatch_one", one)
    monkeypatch.setattr(stopped, "wait", Mock(return_value=False))
    dispatch.main()
    assert len(attempts) == 2
    repo.close.assert_called_once()


def test_idle_dispatcher_waits_instead_of_busy_spinning(monkeypatch):
    repo = Mock()
    stopped = Event()
    monkeypatch.setattr(dispatch, "CreditRepository", lambda _: repo)
    monkeypatch.setattr(dispatch, "Event", lambda: stopped)
    monkeypatch.setattr(dispatch.signal, "signal", Mock())
    one = Mock(return_value=False)
    monkeypatch.setattr(dispatch, "dispatch_one", one)
    wait = Mock(side_effect=lambda _: stopped.set())
    monkeypatch.setattr(stopped, "wait", wait)
    dispatch.main()
    one.assert_called_once_with(repo)
    wait.assert_called_once()
    repo.close.assert_called_once()


def test_dispatcher_closes_client_when_interrupted(monkeypatch):
    import pytest

    repo = Mock()
    monkeypatch.setattr(dispatch, "CreditRepository", lambda _: repo)
    monkeypatch.setattr(dispatch.signal, "signal", Mock())
    monkeypatch.setattr(dispatch, "dispatch_one", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        dispatch.main()
    repo.close.assert_called_once()
