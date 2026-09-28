"""Controlled localhost Selenium cases for the two independent Actions workflows."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By

from signup031.selenium_recording import recording_options


@pytest.fixture
def local_site():
    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<!doctype html><html><body><h1>Synthetic CI page</h1></body></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def browser(selenium_record):
    driver = webdriver.Chrome(options=recording_options(headless=True))
    selenium_record(driver)
    try:
        yield driver
    finally:
        driver.quit()


def test_ci_success(local_site, browser):
    browser.get(local_site)
    assert browser.find_element(By.TAG_NAME, 'h1').text == 'Synthetic CI page'


def test_ci_intentional_failure(local_site, browser):
    browser.get(local_site)
    assert browser.find_element(By.TAG_NAME, 'h1').text == 'Expected repaired page'
