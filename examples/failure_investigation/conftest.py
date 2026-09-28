pytest_plugins = ["signup031.selenium_plugin"]
"""Deliberate localhost failure: run with signup031.selenium_plugin.

This file is automation written outside the desktop app. The assertion must fail.
"""
import inspect
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from signup031.selenium_recording import recording_options

EXPECTED='검색 결과: 사과'
HTML='''<!doctype html><html lang="ko"><meta charset="utf-8"><title>검색 실패 조사 예제</title>
<style>body{font:20px sans-serif;margin:50px}input,button{font:inherit;padding:10px}</style>
<h1>검색 결과 확인</h1><label for="query">검색어</label> <input id="query">
<button id="search" onclick="document.getElementById('result').textContent='검색 오류: '+document.getElementById('query').value">검색</button>
<p id="result">검색어를 입력하세요</p></html>'''

@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            raw=HTML.encode('utf-8');self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/search'
    finally:server.shutdown();server.server_close();thread.join()

@pytest.fixture
def driver(request,selenium_record):
    browser=webdriver.Chrome(options=recording_options(headless=True))
    try:
        selenium_record(browser,test_context={
            'tc_id':'SEARCH-FAILURE-01','expected':EXPECTED,
            # Explicitly supply only this test function, never scan the repository.
            'automation_code':inspect.getsource(request.node.function)})
        yield browser
    finally:
        if browser.service.process is not None and browser.service.process.poll() is None:browser.quit()

