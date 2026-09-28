from queue import Queue
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error

from signup031.replay import wait_for_manual_session


def test_user_closes_browser_during_event_wait():
    page = Mock()
    page.is_closed.side_effect = [False, True]
    page.wait_for_timeout.side_effect = Error('Target closed')
    wait_for_manual_session(page, Queue())
    page.wait_for_timeout.assert_called_once_with(100)


def test_open_page_errors_are_not_hidden():
    page = Mock()
    page.is_closed.return_value = False
    page.context.browser.is_connected.return_value = True
    page.wait_for_timeout.side_effect = Error('unexpected failure')
    with pytest.raises(Error, match='unexpected failure'):
        wait_for_manual_session(page, Queue())
